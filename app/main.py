"""TVProxy IPTV/HLS reverse proxy.

Lightweight async HTTP proxy intended to run on a Raspberry Pi.
Listens on 127.0.0.1:8081, expects an nginx front-end on 127.0.0.1:8080
which in turn is reached from the public Internet only via a Cloudflare
Tunnel.

No transcoding, no decoding, no FFmpeg.  Pure HTTP/HLS proxying.

The app is split into modules:
  app.security   - pure security helpers (SSRF, tokens, IP classification)
  app.streaming  - pure HLS / M3U helpers
  app.stores     - SQLite-backed CRUD (viewers, channels, connections, ...)
  app.admin      - admin HTML handlers + auth/session middleware
  app.main       - this file: aiohttp app + public streaming routes

Currently in this build:
  - Per-viewer tokens (HLS auth)  -- NEW
  - Channels CRUD with /admin/channels (test-stream coming next)
  - Connections CRUD with /admin/connections
  - Admin accounts (owner/admin roles) with /admin/admins
  - Settings page /admin/settings
  - Audit log writes for admin actions
  - In-memory session tracking + terminate via /admin/sessions
  - M3U generator + /playlist.m3u route
  - Bootstrap: first user can be created without auth via /admin/users
                (legacy path) or /admin/admins/bootstrap

Deferred to Phase B (see docs/PHASE_B.md):
  - Geo-IP country lookups
  - 2FA TOTP
  - Statistics graphs / per-user traffic charts
  - QR codes
  - Per-session bandwidth counters
  - IP/country restrictions
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import html
import json
import logging
import os
import socket
import sys
import time
from typing import Optional
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

# Local imports
from .security import (
    ip_is_public,
    redact_token,
    token_is_valid,
    validate_upstream_url_runtime,
)
from .streaming import (
    looks_like_playlist,
    rewrite_playlist,
    safe_host,
)
from .epg import EPG_ROOT, EpgStore, run_daily


APP_VERSION = "1.2.0"

LISTEN_HOST = os.environ.get("TVPROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("TVPROXY_LISTEN_PORT", "8081"))

UPSTREAM_CONNECT_TIMEOUT = float(os.environ.get("UPSTREAM_CONNECT_TIMEOUT", "5"))
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "30"))

CHUNK_SIZE = 16 * 1024

DB_PATH = os.environ.get("TVPROXY_DB_PATH", "/var/lib/tv-proxy/mappings.db")

FORWARD_HEADERS = (
    "user-agent",
    "accept",
    "accept-language",
    "accept-encoding",
    "range",
    "if-range",
)

logger = logging.getLogger("tvproxy")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _AccessFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        message = f"{ts} {record.getMessage()}"
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return message


def _configure_logging() -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_AccessFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Public streaming routes
# ---------------------------------------------------------------------------


async def handle_root(request: web.Request) -> web.Response:
    return web.Response(
        text="TVProxy IPTV Proxy\nStatus: running\n",
        content_type="text/plain",
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK\n", content_type="text/plain")


async def handle_guide_data(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    def viewer_ids(value, fallback):
        if isinstance(value, list):
            return {int(item) for item in value}
        try:
            return {int(item) for item in json.loads(value or "[]")}
        except (TypeError, ValueError, json.JSONDecodeError):
            return set(fallback or [])

    allowed = viewer_ids(viewer.get("allowed_channels"), viewer.get("allowed_channel_ids"))
    favorites = viewer_ids(viewer.get("favorite_channels"), viewer.get("favorite_channel_ids"))
    mappings = await request.app["epg"].mappings()
    channel_ids = {int(row["channel_id"]) for row in mappings}
    if favorites:
        channel_ids &= favorites
    elif allowed:
        channel_ids &= allowed
    channels = {c["id"]: c for c in await request.app["channels"].list_all()
                if c["id"] in channel_ids}
    start = time.time() - 3600
    end = time.time() + 120 * 3600
    items = []
    for channel_id, channel in channels.items():
        programs = await request.app["epg"].programs(channel_id, start, end)
        seen = set()
        for program in programs:
            key = (channel_id, program["start_at"], program["title"])
            if key in seen or program["end_at"] <= program["start_at"]:
                continue
            seen.add(key)
            items.append({"channel_id": channel["id"], "channel": channel["display_name"],
                          "title": program["title"], "description": program["description"],
                          "start": program["start_at"], "end": program["end_at"]})
    return web.json_response({"generated_at": time.time(), "items": sorted(items, key=lambda x: (x["start"], x["channel"]))})


async def handle_guide(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    viewer = await request.app["viewers"].get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="A valid viewer token is required")
    page = """<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>
<title>TVProxy Guide</title><style>body{font:15px system-ui;background:#071426;color:#eaf7ff;max-width:1000px;margin:auto;padding:1rem}article{background:#102943;border:1px solid #4385ad;border-radius:8px;padding:.8rem;margin:.6rem 0}button{padding:.45rem .7rem;background:#168bd0;color:white;border:0;border-radius:5px;cursor:pointer}.muted{color:#9fc0d4}</style>
<h1>TV Guide</h1><p class=muted>Recordings include 5 minutes before and after the program.</p><main id=guide>Loading...</main>
<script>const token=%r,box=document.getElementById('guide'),esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);fetch('/guide/data?token='+encodeURIComponent(token)).then(r=>r.json()).then(rows=>{box.innerHTML=rows.map((p,i)=>`<article><b>${esc(p.channel)}</b><br><strong>${esc(p.title)}</strong><br><span class=muted>${new Date(p.start*1000).toLocaleString()} - ${new Date(p.end*1000).toLocaleTimeString()}</span>${p.description?'<p>'+esc(p.description)+'</p>':''}<br><button data-i="${i}">Record with padding</button></article>`).join('')||'No guide data available';box.querySelectorAll('button').forEach((b)=>b.onclick=()=>fetch('/guide/record?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(rows[b.dataset.i])}).then(r=>r.json()).then(x=>{b.textContent=x.error||'Recording scheduled';b.disabled=true}))}).catch(()=>box.textContent='Unable to load guide')</script>""" % token
    return web.Response(text=page, content_type="text/html")


async def handle_guide_slick(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    viewer = await request.app["viewers"].get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="A valid viewer token is required")
    safe_token = json.dumps(token)
    page = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TVProxy | Guide</title>
<style>
:root{color-scheme:dark;--bg:#06101e;--panel:#10243a;--line:#6fcdf344;--text:#edf8ff;--muted:#91b6ca;--cyan:#78e6ff;--blue:#249be4;--green:#b6f36d;--red:#ff9b8e}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 75% -10%,#1d6688,#0b1e34 42%,var(--bg) 80%);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1440px;margin:auto;padding:24px}.top{display:flex;gap:20px;align-items:end;justify-content:space-between;margin-bottom:20px}.eyebrow{color:var(--green);font-size:11px;letter-spacing:.18em;text-transform:uppercase}.top h1{font-size:32px;font-weight:300;letter-spacing:.04em;margin:4px 0}.muted{color:var(--muted)}.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.tools input,.tools select,.tools button{border:1px solid var(--line);border-radius:8px;background:#07192a;color:var(--text);padding:10px 12px;font:inherit}.tools input{min-width:240px;flex:1}.tools button{cursor:pointer}.tools button.active,.tools button:hover{background:var(--blue);border-color:#8ee9ff}.days{display:flex;gap:8px;overflow:auto;padding-bottom:8px}.days button{min-width:110px}.notice{min-height:24px;color:var(--muted);margin:4px 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px}.channel{background:#0b1b2de6;border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:0 10px 30px #0004}.channel-head{display:flex;justify-content:space-between;align-items:center;padding:13px 15px;background:linear-gradient(100deg,#17476b,#102b45);border-bottom:1px solid var(--line)}.channel-name{font-weight:600;color:var(--cyan)}.program{position:relative;padding:13px 15px;border-bottom:1px solid #78cfff1c}.program:last-child{border:0}.program.current{background:#16472d77;border-left:3px solid var(--green);padding-left:12px}.time{font-size:12px;color:var(--muted)}.title{font-size:16px;font-weight:600;margin:3px 0}.desc{font-size:13px;color:#b9d3e1;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.progress{height:3px;background:#ffffff18;margin:8px 0}.progress i{display:block;height:100%;background:var(--green)}.record{margin-top:9px;border:1px solid #57b7eb88;border-radius:7px;padding:7px 10px;background:#0c3150;color:white;cursor:pointer}.record:hover{background:var(--blue)}.record:disabled{cursor:default;background:#254836;color:#c9f4bd;border-color:#75bd6c}.empty{padding:35px;text-align:center;background:#0b1b2d;border:1px dashed var(--line);border-radius:12px;color:var(--muted)}@media(max-width:650px){.wrap{padding:15px}.top{display:block}.top h1{font-size:27px}.tools input{min-width:100%}.grid{grid-template-columns:1fr}}
</style></head><body><main class="wrap"><header class="top"><div><div class="eyebrow">TVProxy / private relay</div><h1>TV Guide</h1><div class="muted">The next 48 hours, with five minutes of recording padding.</div></div><a class="muted" href="/player?token=''' + html.escape(token, quote=True) + '''">Back to player</a></header>
<nav class="tools"><input id="search" placeholder="Search programmes or channels" autocomplete="off"><select id="channel"><option value="">All channels</option></select><button id="refresh">Refresh</button></nav><div class="days" id="days"></div><p class="notice" id="notice">Loading guide...</p><section class="grid" id="grid"></section></main>
<script>const token=''' + safe_token + r''',grid=document.getElementById('grid'),notice=document.getElementById('notice'),days=document.getElementById('days'),search=document.getElementById('search'),channel=document.getElementById('channel');let rows=[],selectedDay='all',scheduled=new Set();const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);const key=p=>p.channel_id+':'+p.start+':'+p.title;const day=p=>new Date(p.start*1000).toLocaleDateString([], {year:'numeric',month:'2-digit',day:'2-digit'});const time=t=>new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});function renderDays(){const keys=[...new Set(rows.map(day))];days.innerHTML='<button data-day="all">All</button>'+keys.map((k,i)=>'<button data-day="'+k+'">'+(i?' '+new Date(rows.find(p=>day(p)===k).start*1000).toLocaleDateString([], {weekday:'short',day:'numeric',month:'short'}):'Today')+'</button>').join('');days.querySelectorAll('button').forEach(b=>{b.className=b.dataset.day===selectedDay?'active':'';b.onclick=()=>{selectedDay=b.dataset.day;renderDays();render()}})}function render(){const q=search.value.trim().toLowerCase(),chosen=channel.value,now=Date.now()/1000,filtered=rows.filter(p=>(selectedDay==='all'||day(p)===selectedDay)&&(!chosen||String(p.channel_id)===chosen)&&(!q||(p.title+' '+p.channel+' '+p.description).toLowerCase().includes(q)));const groups=new Map();filtered.forEach(p=>{if(!groups.has(p.channel_id))groups.set(p.channel_id,{name:p.channel,items:[]});groups.get(p.channel_id).items.push(p)});grid.innerHTML='';if(!filtered.length){grid.innerHTML='<div class="empty">No programmes match this view.</div>';return}for(const [id,g] of groups){const card=document.createElement('article');card.className='channel';card.innerHTML='<header class="channel-head"><span class="channel-name">'+esc(g.name)+'</span><span class="muted">'+g.items.length+' programmes</span></header>'+g.items.map(p=>{const current=p.start<=now&&p.end>now,pct=current?Math.min(100,Math.max(0,(now-p.start)/(p.end-p.start)*100)):0,k=key(p),past=p.end<=now;return '<div class="program '+(current?'current':'')+'"><div class="time">'+time(p.start)+' - '+time(p.end)+'</div><div class="title">'+esc(p.title)+'</div>'+(current?'<div class="progress"><i style="width:'+pct+'%"></i></div>':'')+(p.description?'<div class="desc">'+esc(p.description)+'</div>':'')+'<button class="record" data-key="'+esc(k)+'" data-id="'+rows.indexOf(p)+'" '+(scheduled.has(k)||past?'disabled':'')+'>'+(scheduled.has(k)?'Scheduled':past?'Finished':'Record with padding')+'</button></div>'}).join('');grid.appendChild(card)}grid.querySelectorAll('.record:not(:disabled)').forEach(b=>b.onclick=()=>record(rows[Number(b.dataset.id)],b))}async function record(p,b){b.disabled=true;b.textContent='Scheduling...';try{const r=await fetch('/guide/record?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),x=await r.json();if(!r.ok)throw Error(x.error||'Unable to schedule');scheduled.add(b.dataset.key);b.textContent='Scheduled';notice.textContent='Recording scheduled successfully.'}catch(e){b.disabled=false;b.textContent='Record with padding';notice.textContent=e.message;notice.style.color='var(--red)'}}async function load(){notice.textContent='Refreshing guide...';try{const r=await fetch('/guide/data?token='+encodeURIComponent(token));const x=await r.json();if(!r.ok)throw Error(x.error||'Unable to load guide');rows=x.items||[];channel.innerHTML='<option value="">All channels</option>'+[...new Map(rows.map(p=>[p.channel_id,p.channel])).entries()].sort((a,b)=>a[1].localeCompare(b[1])).map(([id,n])=>'<option value="'+id+'">'+esc(n)+'</option>').join('');renderDays();render();notice.textContent=rows.length+' programmes available';notice.style.color='var(--muted)'}catch(e){grid.innerHTML='<div class="empty">Unable to load the guide. Please try again.</div>';notice.textContent=e.message;notice.style.color='var(--red)'}}search.oninput=render;channel.onchange=render;document.getElementById('refresh').onclick=load;load();setInterval(load,300000);setInterval(render,60000)</script></body></html>'''
    return web.Response(text=page, content_type="text/html")


async def handle_guide_calendar(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    viewer = await request.app["viewers"].get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="A valid viewer token is required")
    safe_token = json.dumps(token)
    page = r'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>TVProxy | Guide</title>
<style>
:root{color-scheme:dark;--bg:#06101e;--panel:#10243a;--line:#6fcdf344;--text:#edf8ff;--muted:#91b6ca;--cyan:#78e6ff;--blue:#249be4;--green:#b6f36d;--red:#ff9b8e}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 75% -10%,#1d6688,#0b1e34 42%,var(--bg) 80%);color:var(--text);font:14px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}.wrap{max-width:1600px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:18px}.eyebrow{color:var(--green);font-size:11px;letter-spacing:.18em;text-transform:uppercase}.top h1{font-size:31px;font-weight:300;margin:4px 0}.muted{color:var(--muted)}.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.tools input,.tools button{border:1px solid var(--line);border-radius:8px;background:#07192a;color:var(--text);padding:10px 12px;font:inherit}.tools input{min-width:260px;flex:1}.tools button{cursor:pointer}.tools button:hover{background:var(--blue)}.notice{color:var(--muted);min-height:22px;margin:5px 0 12px}.calendar{display:grid;grid-template-columns:245px minmax(760px,1fr);gap:12px;align-items:start}.rail,.calendar-main{background:#0b1b2de8;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 36px #0004}.rail{overflow:hidden}.rail-title{padding:15px;background:linear-gradient(100deg,#17476b,#102b45);color:var(--cyan);font-weight:600}.channel-list{max-height:calc(100vh - 240px);overflow:auto;padding:7px}.channel{display:block;width:100%;text-align:left;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--text);padding:10px;margin:3px 0;cursor:pointer}.channel:hover{background:#164365}.channel.active{background:linear-gradient(100deg,#14679e,#164466);border-color:#8ee9ff88}.channel small{display:block;color:var(--muted);margin-top:2px}.calendar-main{overflow:hidden}.day-heads{display:grid;grid-template-columns:repeat(3,minmax(250px,1fr));background:linear-gradient(100deg,#17476b,#102b45);border-bottom:1px solid var(--line)}.day-head{padding:13px 15px;border-right:1px solid var(--line);font-weight:600;color:var(--cyan)}.day-head small{display:block;color:var(--muted);font-weight:400}.days{display:grid;grid-template-columns:repeat(3,minmax(250px,1fr));overflow:auto}.day{position:relative;height:960px;border-right:1px solid #6fcdf322;background:repeating-linear-gradient(to bottom,transparent 0,transparent 39px,#6fcdf31c 40px)}.hour{position:absolute;left:4px;right:0;border-top:1px solid #6fcdf333;color:#648aa0;font-size:10px;padding-left:3px;pointer-events:none}.program{position:absolute;left:30px;right:7px;min-height:24px;padding:6px 8px;border:1px solid #71d6ff66;border-radius:7px;background:linear-gradient(135deg,#145785ee,#12385be8);overflow:hidden;box-shadow:0 3px 10px #0005}.program.current{border-color:var(--green);background:linear-gradient(135deg,#176443ee,#174b3be8)}.program-title{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.program-time{font-size:11px;color:#b8d4e0}.record{margin-top:5px;border:1px solid #8de5ff77;border-radius:5px;padding:4px 7px;background:#0a2942;color:white;cursor:pointer;font-size:11px}.record:hover{background:var(--blue)}.record:disabled{background:#254836;color:#c9f4bd;border-color:#75bd6c;cursor:default}.empty{padding:45px;text-align:center;color:var(--muted)}@media(max-width:850px){.wrap{padding:14px}.top{display:block}.calendar{grid-template-columns:1fr}.rail{order:0}.channel-list{display:flex;overflow:auto;max-height:none;gap:5px}.channel{min-width:180px}.calendar-main{overflow-x:auto}.day-heads,.days{min-width:810px}.tools input{min-width:100%}}
</style></head><body><main class="wrap"><header class="top"><div><div class="eyebrow">TVProxy / private relay</div><h1>TV Guide</h1><div class="muted">Select a channel, then browse three days like a calendar. Block height follows programme duration.</div></div><a class="muted" href="/player?token=''' + html.escape(token, quote=True) + '''">Back to player</a></header>
<nav class="tools"><input id="search" placeholder="Filter channels or programmes" autocomplete="off"><button id="refresh">Refresh</button></nav><p class="notice" id="notice">Loading guide...</p><section class="calendar"><aside class="rail"><div class="rail-title">Channels</div><div class="channel-list" id="channels"></div></aside><section class="calendar-main"><div class="day-heads" id="heads"></div><div class="days" id="days"></div></section></section></main>
<script>const token=''' + safe_token + r''',channels=document.getElementById('channels'),heads=document.getElementById('heads'),days=document.getElementById('days'),search=document.getElementById('search'),notice=document.getElementById('notice');let rows=[],selected=null,scheduled=new Set();const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const fmt=t=>new Date(t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});const startOfDay=d=>{const x=new Date(d);x.setHours(0,0,0,0);return x};const key=p=>p.channel_id+':'+p.start+':'+p.title;function channelRows(){const q=search.value.trim().toLowerCase();return [...new Map(rows.filter(p=>!q||(p.channel+' '+p.title+' '+p.description).toLowerCase().includes(q)).map(p=>[p.channel_id,p.channel])).entries()].sort((a,b)=>a[1].localeCompare(b[1]))}function renderRail(){const list=channelRows();if(!selected||!list.some(x=>x[0]===selected))selected=list[0]?.[0]||null;channels.innerHTML=list.map(([id,name])=>'<button class="channel '+(id===selected?'active':'')+'" data-id="'+id+'">'+esc(name)+'<small>'+rows.filter(p=>p.channel_id===id).length+' programmes</small></button>').join('')||'<div class="empty">No channels found.</div>';channels.querySelectorAll('button').forEach(b=>b.onclick=()=>{selected=Number(b.dataset.id);renderRail();renderCalendar()})}function renderCalendar(){const now=Date.now()/1000,base=startOfDay(new Date()),dates=[0,1,2].map(i=>new Date(base.getTime()+i*86400000));heads.innerHTML=dates.map(d=>'<div class="day-head">'+d.toLocaleDateString([], {weekday:'long',day:'numeric',month:'short'})+'<small>'+d.toLocaleDateString([], {year:'numeric'})+'</small></div>').join('');days.innerHTML=dates.map((d,index)=>{const dayStart=d.getTime()/1000,dayEnd=dayStart+86400,items=rows.filter(p=>p.channel_id===selected&&p.end>dayStart&&p.start<dayEnd);let html='';for(let h=1;h<24;h++)html+='<span class="hour" style="top:'+(h/24*100)+'%">'+String(h).padStart(2,'0')+':00</span>';for(const p of items){const start=Math.max(p.start,dayStart),end=Math.min(p.end,dayEnd),top=(start-dayStart)/86400*100,height=Math.max(2.6,(end-start)/86400*100),current=p.start<=now&&p.end>now,k=key(p),past=p.end<=now;html+='<article class="program '+(current?'current':'')+'" style="top:'+top+'%;height:'+height+'%" title="'+esc(p.title)+'"><div class="program-time">'+fmt(p.start)+' - '+fmt(p.end)+'</div><div class="program-title">'+esc(p.title)+'</div><button class="record" data-key="'+esc(k)+'" data-id="'+rows.indexOf(p)+'" '+(scheduled.has(k)||past?'disabled':'')+'>'+(scheduled.has(k)?'Scheduled':past?'Finished':'Record')+'</button></article>'}return '<div class="day">'+html+'</div>'}).join('');days.querySelectorAll('.record:not(:disabled)').forEach(b=>b.onclick=()=>record(rows[Number(b.dataset.id)],b));if(!selected)days.innerHTML='<div class="empty">Select a channel to view its schedule.</div>'}async function record(p,b){b.disabled=true;b.textContent='Scheduling...';try{const r=await fetch('/guide/record?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),x=await r.json();if(!r.ok)throw Error(x.error||'Unable to schedule');scheduled.add(b.dataset.key);b.textContent='Scheduled';notice.textContent='Recording scheduled successfully.'}catch(e){b.disabled=false;b.textContent='Record';notice.textContent=e.message;notice.style.color='var(--red)'}}async function load(){notice.textContent='Refreshing guide...';try{const r=await fetch('/guide/data?token='+encodeURIComponent(token));const x=await r.json();if(!r.ok)throw Error(x.error||'Unable to load guide');rows=x.items||[];renderRail();renderCalendar();notice.textContent=rows.length+' programmes across '+channelRows().length+' channels';notice.style.color='var(--muted)'}catch(e){notice.textContent=e.message;notice.style.color='var(--red)';channels.innerHTML='<div class="empty">Unable to load guide.</div>'}}search.oninput=()=>{renderRail();renderCalendar()};document.getElementById('refresh').onclick=load;load();setInterval(load,300000);setInterval(renderCalendar,60000)</script></body></html>'''
    return web.Response(text=page, content_type="text/html")


async def handle_guide_calendar5(request: web.Request) -> web.Response:
    if request.query.get("view") == "all":
        return await handle_guide_all(request)
    response = await handle_guide_calendar(request)
    if response.status != 200:
        return response
    page = response.text
    page = page.replace(
        "</style>",
        ".view-toggle{display:inline-block;color:#b7f36b;border:1px solid #75cfff66;border-radius:8px;padding:8px 11px;text-decoration:none}.view-toggle:hover{background:#218bd0}</style>",
        1,
    )
    page = page.replace(
        "</nav><p class=\"notice\"",
        "<a class=\"view-toggle\" href=\"/guide?token=" + html.escape(request.query.get("token", ""), quote=True) + "&view=all\">All channels</a></nav><p class=\"notice\"",
        1,
    )
    page = page.replace("repeat(3,minmax(250px,1fr))", "repeat(5,minmax(250px,1fr))")
    page = page.replace("dates=[0,1,2].map", "dates=[0,1,2,3,4].map")
    page = page.replace("browse three days like a calendar", "browse five days like a calendar")
    page = page.replace("grid-template-columns:repeat(5,minmax(250px,1fr));", "grid-template-columns:repeat(5,minmax(250px,1fr));min-width:1250px;")
    page = page.replace(
        "<script>const token=",
        "<script>const token=",
        1,
    )
    page = page.replace(
        "document.getElementById('refresh').onclick=load;load();",
        "days.addEventListener('scroll',()=>{heads.style.transform='translateX(-'+days.scrollLeft+'px)'});document.getElementById('refresh').onclick=load;load();",
        1,
    )
    page = page.replace("b.textContent=x.error||'Recording scheduled'", "b.textContent=x.message||x.error||'Recording scheduled'")
    page = page.replace(
        "if(!r.ok)throw Error(x.error||'Unable to schedule');",
        "if(!r.ok){if(x.can_split&&confirm(x.message+' Split into parts?')){const y=await fetch('/guide/record?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...p,split:true})});const z=await y.json();if(!y.ok)throw Error(z.message||z.error||'Unable to split recording');scheduled.add(b.dataset.key);b.textContent='Scheduled in parts';notice.textContent=z.message||'Recording scheduled in parts.';return}throw Error(x.message||x.error||'Unable to schedule');}"
    )
    return web.Response(text=page, content_type="text/html")


async def handle_guide_all(request: web.Request) -> web.Response:
    response = await handle_guide_slick(request)
    if response.status != 200:
        return response
    page = response.text.replace(
        "</header>",
        "<a class=\"view-toggle\" href=\"/guide?token=" + html.escape(request.query.get("token", ""), quote=True) + "\">Calendar view</a></header>",
        1,
    )
    page = page.replace(
        "</style>",
        ".view-toggle{display:inline-block;color:#b7f36b;border:1px solid #75cfff66;border-radius:8px;padding:8px 11px;text-decoration:none}.view-toggle:hover{background:#218bd0}</style>",
        1,
    )
    return web.Response(text=page, content_type="text/html")


async def handle_guide_record(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        data = await request.json()
        channel_id = int(data["channel_id"])
        raw_start = float(data["start"])
        raw_end = float(data["end"])
        start = raw_start - 300
        end = raw_end + 300
        channel = await request.app["channels"].get(channel_id)
        allowed = viewer.get("allowed_channel_ids") or []
        if not channel or not channel.get("enabled") or (allowed and channel_id not in allowed) or end <= start:
            raise ValueError
        if start < time.time() - 60:
            raise ValueError
        matches = await request.app["epg"].programs(channel_id, raw_start - 2, raw_end + 2)
        if not any(abs(p["start_at"] - float(data["start"])) < 2 and
                   p["title"] == str(data.get("title") or "") for p in matches):
            return web.json_response({"error": "programme-no-longer-available"}, status=409)
        if end - start > 4 * 3600:
            if not data.get("split"):
                return web.json_response({"error": "recording-too-long", "message": "This recording exceeds the 4-hour limit.", "can_split": True}, status=422)
            parts = []
            cursor = raw_start
            while cursor < raw_end:
                part_end = min(cursor + 4 * 3600 - 600, raw_end)
                part_start = cursor - 300 if cursor == raw_start else cursor
                part_stop = part_end + 300 if part_end == raw_end else part_end
                row = await request.app["recordings"].create(viewer["id"], channel_id,
                    f"{data.get('title') or 'Recording'} (part {len(parts) + 1})", part_start, part_stop, "Europe/Amsterdam")
                parts.append(row["id"])
                cursor = part_end
            return web.json_response({"parts": parts, "message": f"Scheduled {len(parts)} recording parts."}, status=201)
        for existing in await request.app["recordings"].list_all(viewer["id"]):
            if existing["channel_id"] == channel_id and existing["status"] in ("scheduled", "recording") and start < existing["end_at"] and end > existing["start_at"]:
                return web.json_response({"error": "recording-overlaps-existing"}, status=409)
        row = await request.app["recordings"].create(viewer["id"], channel_id,
            str(data.get("title") or "")[:200], start, end, "Europe/Amsterdam")
        return web.json_response({"id": row["id"], "start": start, "end": end}, status=201)
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "invalid-program"}, status=422)


async def handle_player(request: web.Request) -> web.Response:
    """Serve the browser player without exposing provider URLs."""
    token = request.query.get("token", "")
    viewer = await request.app["viewers"].get_by_token(token) if token else None
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="A valid viewer token is required",
                            content_type="text/plain")
    page_html = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TVProxy | Live Lounge</title>
<style>
:root{color-scheme:dark;--ink:#07111f;--panel:#101d31e8;--blue:#2e9cff;--cyan:#7de7ff;--green:#b7f36b;--line:#75cfff38}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:#eaf7ff;font:15px/1.4 "Segoe UI",Arial,sans-serif;background:radial-gradient(circle at 70% -10%,#1b668d,#0a1c34 42%,#020711 100%);overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.08;background:repeating-linear-gradient(0deg,transparent 0 3px,#fff 4px)}
.shell{position:relative;max-width:1500px;margin:auto;padding:24px}.brand{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}.brand h1{margin:0;font-size:28px;font-weight:300;letter-spacing:.08em;text-shadow:0 0 24px #4ecbff}.brand small{color:#9cc9e5;letter-spacing:.14em;text-transform:uppercase}.layout{display:grid;grid-template-columns:330px minmax(0,1fr);gap:18px;align-items:start}.rail,.stage{border:1px solid var(--line);background:linear-gradient(145deg,#173758d9,#071426ee);box-shadow:0 18px 60px #0008;border-radius:12px}.rail{padding:16px;max-height:calc(100vh - 116px);overflow:auto}.rail-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}.rail h2{font-weight:300;margin:0;color:var(--cyan)}.count{color:var(--green);font-size:12px}.search{width:100%;padding:11px 12px;color:white;background:#020a16aa;border:1px solid var(--line);border-radius:7px;outline:0;margin-bottom:12px}.search:focus{border-color:var(--blue);box-shadow:0 0 15px #2e9cff55}.channel{display:block;width:100%;border:1px solid transparent;border-radius:8px;padding:11px 12px;margin:5px 0;text-align:left;background:#0b2239aa;color:#dff4ff;cursor:pointer}.channel:hover,.channel.active{border-color:#63caff99;background:linear-gradient(100deg,#106ab2,#16446c);box-shadow:0 0 18px #148ce044}.channel strong{display:block;font-size:14px;font-weight:500}.channel span{display:block;color:#93bad5;font-size:12px;margin-top:2px}.stage{padding:14px}.screen{position:relative;background:#000;border-radius:9px;overflow:hidden;aspect-ratio:16/9;box-shadow:0 0 0 1px #83dfff22,0 16px 45px #000b}.screen video{width:100%;height:100%;display:block}.welcome{position:absolute;inset:0;display:grid;place-content:center;text-align:center;background:radial-gradient(circle,#16466b,#030912 70%);color:#bfeaff}.welcome b{font-size:24px;font-weight:300}.welcome p{color:#8fb8d3}.controls{display:flex;align-items:center;gap:10px;padding:14px 4px 3px}.now{flex:1;min-width:0}.now b{display:block;font-size:18px;font-weight:400;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.now span{color:#90b7d1;font-size:12px}.action{border:1px solid #68cfff77;background:linear-gradient(#176da0,#0a355e);color:#fff;border-radius:7px;padding:9px 13px;cursor:pointer}.action:hover{filter:brightness(1.3)}.hint{color:#789bb6;font-size:12px;margin:10px 4px 0}@media(max-width:800px){.shell{padding:14px}.layout{grid-template-columns:1fr}.rail{order:2;max-height:43vh}.stage{order:1}.brand h1{font-size:21px}}
</style><style>
 .groups{display:block;padding:2px 0 10px}.groups details{margin:6px 0;border:1px solid var(--line);border-radius:8px;background:#06182aaa}.groups summary{padding:9px 11px;color:var(--cyan);cursor:pointer;list-style-position:inside}.groups details>div{padding:0 6px 5px}.group{white-space:nowrap;border:0;border-radius:20px;padding:6px 10px;background:#09223a;color:#9fc7df;cursor:pointer}.group.active,.group:hover{background:#218bd0;color:white}.channel-row{display:flex;align-items:center;gap:4px}.channel{display:flex;align-items:center;gap:10px;flex:1}.favorite{border:0;background:transparent;color:#7fa1b8;font-size:20px;padding:5px;cursor:pointer}.favorite.on{color:#ffd35a}.thumb{width:28px;height:28px;border-radius:50%;display:grid;place-items:center;background:linear-gradient(135deg,#37b6e8,#164c86);color:#dff8ff;font-size:11px;font-weight:600;flex:none}.channel strong{flex:1}.stage:before{content:"LIVE RELAY";display:block;color:#b7f36b;font-size:11px;letter-spacing:.18em;margin:2px 4px 10px}.screen:after{content:"TVProxy";position:absolute;right:12px;top:10px;color:#ffffff99;font-size:11px;letter-spacing:.12em}.screen{isolation:isolate}.screen video{position:relative;z-index:0}.welcome{z-index:1}
</style></head><body><main class="shell"><header class="brand"><div><h1>TVProxy</h1><small>Live Lounge</small></div><small>Private relay</small></header>
<section class="layout"><aside class="rail"><div class="rail-head"><h2>Channels</h2><span class="count" id="count"></span></div><input class="search" id="search" placeholder="Find a channel..." autocomplete="off"><div class="groups" id="groups"></div><div id="channels"></div></aside>
 <section class="stage"><div class="screen"><video id="video" controls playsinline></video><div class="welcome" id="welcome"><div><b>Choose a channel</b><p>Every stream is delivered through TVProxy.</p></div></div></div><div class="controls"><div class="now"><b id="now">Ready to watch</b><span id="meta">Select a channel from the lounge</span></div><button class="action" id="fullscreen" type="button">Fullscreen</button><button class="action" id="sync-favorites" type="button">Sync local favorites</button><a class="action" href="/guide?token=''' + html.escape(token, quote=True) + '''">TV Guide</a></div><p class="hint">Tip: press F for fullscreen. Your player never connects directly to the provider.</p></section></section></main>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script><script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/dist/mpegts.min.js"></script>
<script>
  const video=document.getElementById('video'), list=document.getElementById('channels'), welcome=document.getElementById('welcome');let hls=null,ts=null,items=[],activeGroup='All';const viewerToken=new URLSearchParams(location.search).get('token')||'',favKey='tvproxy-favorites-'+viewerToken;let favorites=new Set();function favoriteSlug(item){let marker='/live/',start=item.url.indexOf(marker);if(start<0)return item.url;start+=marker.length;let end=item.url.indexOf('.',start);if(end<0)end=item.url.indexOf('?',start);if(end<0)end=item.url.length;return decodeURIComponent(item.url.slice(start,end))}
function stop(){if(hls){hls.destroy();hls=null}if(ts){ts.destroy();ts=null}video.removeAttribute('src');video.load()}
function play(item,button){stop();document.querySelectorAll('.channel').forEach(x=>x.classList.remove('active'));button.classList.add('active');welcome.style.display='none';document.getElementById('now').textContent=item.name;document.getElementById('meta').textContent=item.group||'TVProxy';
 if(window.mpegts&&mpegts.isSupported()){playTs(item,()=>playHls(item))}
 else {playHls(item)} }
 function playHls(item){if(window.Hls&&Hls.isSupported()){hls=new Hls({enableWorker:true});hls.loadSource(item.url);hls.attachMedia(video);hls.on(Hls.Events.MANIFEST_PARSED,()=>video.play().catch(()=>{}));hls.on(Hls.Events.ERROR,(e,d)=>{if(d.fatal)showError('HLS playback failed')})}
 else if(video.canPlayType('application/vnd.apple.mpegurl')){video.src=item.url;video.play().catch(()=>showError('Playback was blocked by the browser'))}
 else {showError('This browser cannot play this stream')} }
 function playTs(item,onfail){if(!window.mpegts||!mpegts.isSupported()){onfail();return}ts=mpegts.createPlayer({type:'mpegts',url:item.url,isLive:true});ts.attachMediaElement(video);ts.on(mpegts.Events.ERROR,()=>{if(ts){ts.destroy();ts=null}onfail()});ts.load();ts.play().catch(()=>{})}
 function showError(message){document.getElementById('meta').textContent=message;welcome.style.display='grid';welcome.querySelector('b').textContent='Playback unavailable';welcome.querySelector('p').textContent='The stream could not be played in this browser.'}
 function renderGroups(){let groups=['Favorites',...new Set(items.map(x=>x.group||'TVProxy'))].filter((g,i,a)=>a.indexOf(g)===i).sort((a,b)=>a==='Favorites'?-1:b==='Favorites'?1:a.localeCompare(b));document.getElementById('groups').innerHTML='';groups.forEach(g=>{let d=document.createElement('details');let s=document.createElement('summary');s.textContent=g;d.appendChild(s);let target=document.createElement('div');target.dataset.group=g;d.appendChild(target);document.getElementById('groups').appendChild(d)})}
  function render(){let q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('#groups details').forEach(d=>d.querySelector('div').innerHTML='');items.filter(x=>x.name.toLowerCase().includes(q)||(x.group||'').toLowerCase().includes(q)).forEach(item=>{let groups=[item.group||'TVProxy'];if(favorites.has(favoriteSlug(item)))groups.unshift('Favorites');groups.forEach(g=>{let target=Array.from(document.querySelectorAll('#groups details div')).find(x=>x.dataset.group===g);if(!target)return;let row=document.createElement('div');row.className='channel-row';let b=document.createElement('button');b.className='channel';b.innerHTML='<span class="thumb"></span><strong></strong><span></span>';b.querySelector('.thumb').textContent=item.name.slice(0,1).toUpperCase();b.querySelector('strong').textContent=item.name;b.querySelectorAll('span')[1].textContent=item.group||'TVProxy';b.onclick=()=>play(item,b);let star=document.createElement('button');star.className='favorite'+(favorites.has(favoriteSlug(item))?' on':'');star.textContent=favorites.has(favoriteSlug(item))?'★':'☆';star.title='Add or remove favorite';star.onclick=()=>{let slug=favoriteSlug(item);if(favorites.has(slug))favorites.delete(slug);else favorites.add(slug);try{localStorage.setItem(favKey,JSON.stringify([...favorites]))}catch(e){}fetch('/favorites?token='+encodeURIComponent(viewerToken),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slugs:[...favorites]})});render()};row.append(b,star);target.appendChild(row)})});if(q)document.querySelectorAll('#groups details').forEach(d=>{if(d.querySelector('.channel'))d.open=true})}
 function attr(text,name){let marker=name+'="',start=text.indexOf(marker);if(start<0)return'';start+=marker.length;let end=text.indexOf('"',start);return end<0?'':text.slice(start,end)}
 function parse(text){let lines=text.split(String.fromCharCode(10)),out=[],pending=null;for(let line of lines){line=line.trim();if(line.indexOf('#EXTINF')===0){let p=line.indexOf(','),attrs=line.slice(0,p),name=line.slice(p+1)||'Channel';pending={name:name,group:attr(attrs,'group-title')||'TVProxy',logo:attr(attrs,'tvg-logo')}}else if(line&&line.charAt(0)!=='#'&&pending){pending.url=line;out.push(pending);pending=null}}return out}
   const playlistKey='tvproxy-playlist-'+viewerToken;try{let cached=localStorage.getItem(playlistKey);if(cached){items=parse(cached);document.getElementById('count').textContent=items.length+' available';renderGroups();render()}}catch(e){}fetch('/playlist.m3u?token='+encodeURIComponent(viewerToken)).then(r=>{if(!r.ok)throw Error('playlist '+r.status);return r.text()}).then(t=>{try{localStorage.setItem(playlistKey,t)}catch(e){}items=parse(t);document.getElementById('count').textContent=items.length+' available';renderGroups();render();return fetch('/favorites?token='+encodeURIComponent(viewerToken)).then(r=>r.json())}).then(data=>{favorites=new Set(data.slugs||[]);let old=[];try{old=JSON.parse(localStorage.getItem(favKey)||'[]')}catch(e){}if(old.length){favorites=new Set([...favorites,...old.map(u=>favoriteSlug({url:u}))]);fetch('/favorites?token='+encodeURIComponent(viewerToken),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slugs:[...favorites]})})}render()}).catch(()=>{if(!items.length)list.textContent='Unable to load playlist'});document.getElementById('sync-favorites').onclick=()=>{let old=[];try{old=JSON.parse(localStorage.getItem(favKey)||'[]')}catch(e){}let slugs=[...new Set([...favorites,...old.map(u=>favoriteSlug({url:u}))])];fetch('/favorites?token='+encodeURIComponent(viewerToken),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({slugs})}).then(r=>r.json()).then(data=>{favorites=new Set(data.slugs||[]);render();document.getElementById('sync-favorites').textContent='Favorites synced';setTimeout(()=>document.getElementById('sync-favorites').textContent='Sync local favorites',1800)});};
document.getElementById('search').oninput=render;document.getElementById('fullscreen').onclick=()=>{let s=document.querySelector('.screen');(document.fullscreenElement?document.exitFullscreen():s.requestFullscreen())};document.onkeydown=e=>{if(e.key.toLowerCase()==='f'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName))document.getElementById('fullscreen').click()};
</script></body></html>'''
    return web.Response(text=page_html, content_type="text/html")


async def handle_record_page(request: web.Request) -> web.Response:
    token = request.query.get("token", "") or request.match_info.get("token", "")
    viewer = await request.app["viewers"].get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="A valid viewer token is required.")
    zone_name = "Europe/Amsterdam"
    next_hour = (datetime.now(ZoneInfo(zone_name)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    message = request.query.get("message", "")
    page_html = f"""<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Plan recording</title>
<style>body{{margin:0;padding:1rem;background:#071426;color:#eaf7ff;font:16px system-ui}}main{{max-width:520px;margin:auto}}section{{padding:1rem;background:#102943;border:1px solid #4385ad;border-radius:12px}}label{{display:block;margin:.8rem 0 .3rem}}input,select,button{{width:100%;padding:.75rem;border-radius:7px;border:1px solid #5798bd;background:#06182b;color:inherit;font:inherit}}button{{margin-top:1rem;background:#168bd0;cursor:pointer}}.msg{{padding:.7rem;background:#3d8a3d66;border-radius:7px}}</style>
<main><h1>Plan recording</h1><p>Viewer: {html.escape(str(viewer.get("name", "Viewer")))}</p><section>{f'<p class="msg">{html.escape(message)}</p>' if message else ''}<form method="post" action="/record"><input type="hidden" name="token" value="{html.escape(token, quote=True)}">
<label>Channel</label><select id="channel" name="channel_id" required><option>Loading channels...</option></select>
<label>Title (optional)</label><input name="title" placeholder="Recording title">
<label>Start</label><input name="start_at" type="datetime-local" value="{next_hour.strftime('%Y-%m-%dT%H:%M')}" required>
<label>End</label><input name="end_at" type="datetime-local" value="{(next_hour + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')}" required>
<label>Timezone</label><input name="timezone" value="{zone_name}" required><button type="submit">Schedule recording</button></form></section><section><h2>Available recordings</h2><div id="recordings">Loading recordings...</div></section></main><script src="https://cdn.jsdelivr.net/npm/mpegts.js@1.8.0/dist/mpegts.min.js"></script>
<script>const token={token!r},channel=document.getElementById('channel');document.querySelector('[name=timezone]').value=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';const now=new Date();now.setMinutes(0,0,0);now.setHours(now.getHours()+1);const pad=n=>String(n).padStart(2,'0'),fmt=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes());document.querySelector('[name=start_at]').value=fmt(now);now.setHours(now.getHours()+1);document.querySelector('[name=end_at]').value=fmt(now);fetch('/favorites?token='+encodeURIComponent(token)).then(r=>r.json()).then(account=>fetch('/record/channels?token='+encodeURIComponent(token)).then(r=>r.json()).then(rows=>{{const fav=new Set(account.slugs||[]),filtered=rows.filter(c=>fav.has(c.slug));channel.innerHTML=filtered.map(c=>'<option value="'+c.id+'">'+c.name.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</option>').join('')||'<option value="">No favorites saved</option>'}}));fetch('/recordings?token='+encodeURIComponent(token)).then(r=>r.json()).then(rows=>{{const box=document.getElementById('recordings');box.innerHTML=rows.map(r=>'<div><strong>'+r.title.replace(/</g,'&lt;')+'</strong><br><video id="recording-'+r.id+'" controls preload="metadata" style="width:100%"></video><a href="/recording/'+r.id+'.ts?token='+encodeURIComponent(token)+'" download>Download</a></div>').join('')||'<p>No completed recordings yet.</p>';rows.forEach(r=>{{const p=mpegts.createPlayer({{type:'mpegts',url:'/recording/'+r.id+'.ts?token='+encodeURIComponent(token),isLive:false}});p.attachMediaElement(document.getElementById('recording-'+r.id));p.load()}})}}).catch(()=>document.getElementById('recordings').textContent='Unable to load recordings')</script>"""
    page_html = page_html.replace("<h2>Available recordings</h2>", "<h2>Available recordings</h2><p><label><input type='checkbox' id='select-all-recordings'> Select all</label> <button type='button' id='delete-selected-recordings'>Delete selected/all</button></p>", 1)
    page_html = page_html.replace("</body>", "<script>const recordingBox=document.getElementById('recordings'),selectAll=document.getElementById('select-all-recordings'),deleteSelected=document.getElementById('delete-selected-recordings');const addRecordingSelectors=()=>recordingBox.querySelectorAll(':scope > div').forEach(card=>{if(card.querySelector('.recording-select'))return;const video=card.querySelector('video');if(!video)return;const id=video.id.replace('recording-','');const label=document.createElement('label');label.innerHTML='<input type=checkbox class=recording-select value='+id+'> Select';card.prepend(label,document.createElement('br'))});new MutationObserver(addRecordingSelectors).observe(recordingBox,{childList:true});selectAll.onchange=()=>recordingBox.querySelectorAll('.recording-select').forEach(x=>x.checked=selectAll.checked);deleteSelected.onclick=()=>{const ids=[...recordingBox.querySelectorAll('.recording-select:checked')].map(x=>Number(x.value));if(!confirm(ids.length?'Delete selected recordings?':'Delete all recordings?'))return;fetch('/recordings/delete-all?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})}).then(r=>r.json()).then(()=>location.reload())};addRecordingSelectors();</script></body>", 1)
    return web.Response(text=page_html, content_type="text/html")


async def handle_record_channels(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    allowed = set(viewer.get("allowed_channel_ids") or [])
    channels = await request.app["channels"].list_all()
    if allowed:
        channels = [c for c in channels if c["id"] in allowed]
    return web.json_response([{"id": c["id"], "slug": c["slug"], "name": c.get("display_name") or c.get("slug")} for c in channels])


async def handle_favorites(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    channels = await request.app["channels"].list_all()
    if request.method == "GET":
        favorites = set(viewer.get("favorite_channel_ids") or [])
        return web.json_response({"slugs": [c["slug"] for c in channels if c["id"] in favorites]})
    data = await request.json()
    slugs = set(str(s) for s in data.get("slugs", []) if isinstance(s, str))
    allowed = set(viewer.get("allowed_channel_ids") or [])
    ids = [c["id"] for c in channels if c["slug"] in slugs and (not allowed or c["id"] in allowed)]
    await request.app["viewers"].set_favorites(viewer["id"], ids)
    return web.json_response({"slugs": [c["slug"] for c in channels if c["id"] in ids]})


async def handle_viewer_recordings(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    rows = await request.app["recordings"].list_all(viewer["id"])
    try:
        requested_ids = {int(value) for value in (await request.json()).get("ids", [])}
    except (TypeError, ValueError, json.JSONDecodeError):
        requested_ids = set()
    return web.json_response([{"id": r["id"], "title": r.get("title") or f"Recording {r['id']}"}
                             for r in rows if r.get("status") == "completed" and r.get("local_path") and os.path.isfile(r["local_path"])])


async def handle_viewer_recordings_delete_all(request: web.Request) -> web.Response:
    viewer = await request.app["viewers"].get_by_token(request.query.get("token", ""))
    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)
    rows = await request.app["recordings"].list_all(viewer["id"])
    deleted = 0
    for row in rows:
        if requested_ids and row["id"] not in requested_ids:
            continue
        if row.get("status") in ("recording", "uploading"):
            continue
        if await request.app["recordings"].delete(row["id"]):
            if row.get("local_path"):
                try:
                    os.remove(row["local_path"])
                except FileNotFoundError:
                    pass
            deleted += 1
    return web.json_response({"deleted": deleted})


async def handle_record_create(request: web.Request) -> web.Response:
    data = await request.post()
    token = str(data.get("token") or "")
    viewer = await request.app["viewers"].get_by_token(token)
    try:
        channel_id = int(data.get("channel_id"))
        zone_name = str(data.get("timezone") or "UTC")
        zone = ZoneInfo(zone_name)
        start = datetime.fromisoformat(str(data.get("start_at"))).replace(tzinfo=zone).timestamp()
        end = datetime.fromisoformat(str(data.get("end_at"))).replace(tzinfo=zone).timestamp()
        channel = await request.app["channels"].get(channel_id)
        allowed = viewer.get("allowed_channel_ids") or []
        if not viewer or viewer.get("disabled") or not channel or (allowed and channel_id not in allowed):
            raise ValueError("channel is not available to this viewer")
        if start < time.time():
            raise ValueError("start time cannot be in the past")
        if end <= start or end - start > 4 * 3600:
            raise ValueError("end time must be after start and within 4 hours")
        await request.app["recordings"].create(viewer["id"], channel_id, str(data.get("title") or ""), start, end, zone_name)
        return web.HTTPFound("/record?token=" + quote(token, safe="") + "&message=" + quote("Recording scheduled.", safe=""))
    except (TypeError, ValueError, KeyError) as exc:
        return web.HTTPFound("/record?token=" + quote(token, safe="") + "&message=" + quote("Could not schedule: " + str(exc), safe=""))


def _extract_token(request: web.Request) -> Optional[str]:
    token = request.query.get("token")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def handle_playlist(request: web.Request, session: aiohttp.ClientSession):
    """Serve an HLS playlist by looking up the viewer's allowed channels."""
    viewers = request.app["viewers"]
    viewer = None
    token = request.query.get("token") or ""
    short_code = request.match_info.get("code") or ""
    if token:
        viewer = await viewers.get_by_token(token)
    elif short_code:
        viewer = await viewers.get_by_short_code(short_code)
        token = viewer.get("token", "") if viewer else ""

    if not viewer:
        return web.json_response({"error": "forbidden"}, status=403)
    if viewer.get("disabled"):
        return web.json_response({"error": "disabled"}, status=403)
    expires_at = viewer.get("expires_at")
    if expires_at and expires_at < asyncio.get_event_loop().time():
        return web.json_response({"error": "expired"}, status=403)

    slug = request.match_info.get("name", "").rsplit(".", 1)[0]
    channel = await request.app["channels"].get_by_slug(slug)
    if not channel:
        return web.json_response({"error": "no-such-channel"}, status=404)

    allowed = viewer.get("allowed_channel_ids") or []
    if allowed and channel["id"] not in allowed:
        return web.json_response({"error": "channel-not-allowed"}, status=403)

    # Build the upstream URL from the channel's connection
    conn = await request.app["connections"].get(channel["connection_id"])
    if not conn or not conn.get("enabled"):
        return web.json_response({"error": "upstream-disabled"}, status=502)
    upstream_url = channel["upstream_path"]
    if not upstream_url.startswith(("http://", "https://")):
        upstream_url = urljoin(conn["base_url"].rstrip("/") + "/", upstream_url.lstrip("/"))

    ok, reason = validate_upstream_url_runtime(upstream_url, conn["allowed_host"])
    if not ok:
        logger.warning("upstream-reject reason=%s slug=%s", reason, slug)
        return web.json_response({"error": "upstream-blocked"}, status=502)

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS
    }
    if conn.get("auth_header"):
        headers["Authorization"] = conn["auth_header"]

    timeout = aiohttp.ClientTimeout(
        sock_connect=UPSTREAM_CONNECT_TIMEOUT,
        sock_read=UPSTREAM_READ_TIMEOUT,
    )
    try:
        resp = await session.get(
            upstream_url, headers=headers, allow_redirects=True,
            timeout=timeout, auto_decompress=False,
        )
    except aiohttp.ClientError as exc:
        logger.warning("upstream-connect-fail slug=%s err=%s", slug, type(exc).__name__)
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status != 200:
            logger.warning("upstream-fetch-fail slug=%s code=%s", slug, resp.status)
            return web.json_response({"error": "upstream-non-200"}, status=502)
        final_url = str(resp.url)
        final_host = safe_host(final_url)
        ok, reason = validate_upstream_url_runtime(final_url, final_host)
        if not ok:
            logger.warning("upstream-redirect-reject reason=%s slug=%s", reason, slug)
            return web.json_response({"error": "upstream-redirect-blocked"}, status=502)
        ctype = resp.headers.get("Content-Type", "")
        if not looks_like_playlist(upstream_url, ctype):
            out = web.StreamResponse(status=resp.status)
            for key in ("content-type", "content-length", "content-range",
                        "accept-ranges", "last-modified", "etag", "cache-control"):
                if key in resp.headers:
                    out.headers[key] = resp.headers[key]
            await out.prepare(request)
            try:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    await out.write(chunk)
            except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                logger.info("viewer-disconnected slug=%s", slug)
            return out
        body = await resp.text()
    finally:
        await resp.release()

    proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
    rewritten = rewrite_playlist(body, str(resp.url), proxy_base, token)

    # Record session: mark this viewer as currently active.
    sessions = request.app["sessions"]
    sessions.touch(viewer["id"], channel["id"], request, started_at=None)

    return web.Response(
        text=rewritten,
        content_type="application/vnd.apple.mpegurl",
        charset="utf-8",
    )


async def handle_segment(request: web.Request, session: aiohttp.ClientSession):
    viewers = request.app["viewers"]
    token = request.query.get("token") or ""
    viewer = None
    if token:
        viewer = await viewers.get_by_token(token)

    if not viewer or viewer.get("disabled"):
        return web.json_response({"error": "forbidden"}, status=403)

    target = request.query.get("upstream", "")
    if not target:
        return web.json_response({"error": "missing-upstream"}, status=400)

    # Verify target's host is in one of the connection allowlists.
    target_host = safe_host(target)
    connections = await request.app["connections"].list_all()
    conn = None
    for c in connections:
        if c.get("enabled") and c["allowed_host"].lower() == target_host.lower():
            conn = c
            break
    if conn is None:
        return web.json_response({"error": "upstream-blocked"}, status=403)

    ok, reason = validate_upstream_url_runtime(target, target_host)
    if not ok:
        return web.json_response({"error": "upstream-blocked"}, status=403)

    headers = {
        k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS
    }
    if conn.get("auth_header"):
        headers["Authorization"] = conn["auth_header"]

    timeout = aiohttp.ClientTimeout(
        sock_connect=UPSTREAM_CONNECT_TIMEOUT,
        sock_read=UPSTREAM_READ_TIMEOUT,
    )
    try:
        resp = await session.get(
            target, headers=headers, allow_redirects=True,
            timeout=timeout, auto_decompress=False,
        )
    except aiohttp.ClientError as exc:
        logger.warning(
            "upstream-connect-fail url=%s err=%s", target_host, type(exc).__name__
        )
        return web.json_response({"error": "upstream-fetch-fail"}, status=502)

    try:
        if resp.status >= 400:
            return web.json_response({"error": "upstream-non-2xx"}, status=502)
        final_url = str(resp.url)
        final_host = safe_host(final_url)
        ok, reason = validate_upstream_url_runtime(final_url, final_host)
        if not ok:
            return web.json_response({"error": "upstream-redirect-blocked"}, status=502)

        ctype = resp.headers.get("Content-Type", "")
        if looks_like_playlist(target, ctype):
            body = await resp.text()
            proxy_base = f"{str(request.scheme)}://{request.host}/proxy"
            rewritten = rewrite_playlist(body, str(resp.url), proxy_base, token)
            return web.Response(
                text=rewritten,
                content_type="application/vnd.apple.mpegurl",
                charset="utf-8",
            )

        out = web.StreamResponse(status=resp.status)
        passthrough = (
            "content-type", "content-length", "content-range",
            "accept-ranges", "last-modified", "etag", "cache-control",
        )
        for k, v in resp.headers.items():
            if k.lower() in passthrough:
                out.headers[k] = v
        await out.prepare(request)
        async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
            await out.write(chunk)
        return out
    finally:
        if not resp.closed:
            await resp.release()


async def handle_playlist_m3u(request: web.Request):
    """Render an M3U listing all channels the viewer can watch."""
    viewers = request.app["viewers"]
    settings_store = request.app["settings_store"]
    token = request.query.get("token") or ""
    short_code = request.match_info.get("code") or ""
    viewer = await viewers.get_by_token(token) if token else await viewers.get_by_short_code(short_code)
    if not viewer or viewer.get("disabled"):
        return web.Response(text="#EXTM3U\n", content_type="audio/x-mpegurl")
    if viewer.get("expires_at") and viewer["expires_at"] < asyncio.get_event_loop().time():
        return web.Response(text="#EXTM3U\n", content_type="audio/x-mpegurl")

    allowed = viewer.get("allowed_channel_ids") or []
    all_channels = await request.app["channels"].list_all()
    if allowed:
        channels = [c for c in all_channels if c["id"] in allowed]
    else:
        channels = all_channels
    recordings = [r for r in await request.app["recordings"].list_all(viewer["id"])
                  if r.get("status") == "completed" and r.get("local_path") and os.path.isfile(r["local_path"])]
    for recording in recordings:
        recording["external_filename"] = os.path.basename(recording["local_path"])

    public_url = (await settings_store.get("public_url")) or "https://tv.berrie.uk"
    from .streaming import generate_user_m3u
    m3u = generate_user_m3u(
        public_url, channels, viewer["token"], viewer.get("short_code") if short_code else None, recordings,
        os.environ.get("TVPROXY_LOCAL_RECORDING_URL", "http://192.168.178.217:8081"),
        os.environ.get("TVPROXY_EXTERNAL_RECORDING_URL", "https://filesharez.berrie.uk/iptv"),
        os.environ.get("TVPROXY_EXTERNAL_RECORDING_TOKEN", ""),
    )
    return web.Response(text=m3u, content_type="audio/x-mpegurl")


async def handle_recording_file(request: web.Request):
    token = request.query.get("token") or ""
    viewer = await request.app["viewers"].get_by_token(token)
    if not viewer or viewer.get("disabled"):
        return web.Response(status=403, text="Forbidden")
    try:
        recording_id = int(request.match_info["id"])
    except ValueError:
        return web.Response(status=404, text="Not found")
    recording = await request.app["recordings"].get(recording_id)
    path = recording.get("local_path", "") if recording else ""
    if not recording or recording["viewer_id"] != viewer["id"] or recording.get("status") != "completed":
        return web.Response(status=404, text="Not found")
    root = os.path.realpath(os.environ.get("TVPROXY_RECORDING_DIR", "/var/lib/tv-proxy/recordings"))
    if not path or os.path.commonpath((root, os.path.realpath(path))) != root or not os.path.isfile(path):
        return web.Response(status=404, text="Not found")
    size = os.path.getsize(path)
    start, end = 0, size - 1
    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes="):
        first, _, last = range_header[6:].split(",", 1)[0].partition("-")
        try:
            start = int(first) if first else max(0, size - int(last))
            end = int(last) if first and last else size - 1
            if start < 0 or start > end or end >= size:
                raise ValueError
        except ValueError:
            return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    response = web.StreamResponse(status=206 if range_header else 200, headers={
        "Content-Type": "video/mp2t", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
        **({"Content-Range": f"bytes {start}-{end}/{size}"} if range_header else {}),
    })
    await response.prepare(request)
    try:
        with open(path, "rb") as recording_file:
            recording_file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = recording_file.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                await response.write(chunk)
                remaining -= len(chunk)
    except ConnectionResetError:
        return response
    await response.write_eof()
    return response


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@web.middleware
async def access_log_middleware(request: web.Request, handler):
    resp = await handler(request)
    logger.info(
        "access method=%s path=%s status=%s token=%s ua=%s",
        request.method,
        request.path,
        resp.status,
        redact_token(_extract_token(request)),
        request.headers.get("User-Agent", "-"),
    )
    return resp


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception:  # pragma: no cover - last-resort guard
        logger.exception("unhandled-error path=%s", request.path)
        return web.json_response({"error": "internal-error"}, status=500)


def _make_app(stores: dict, session: aiohttp.ClientSession) -> web.Application:
    app = web.Application(
        middlewares=[access_log_middleware, error_middleware],
    )
    app["viewers"] = stores["viewers"]
    app["channels"] = stores["channels"]
    app["connections"] = stores["connections"]
    app["admin_users"] = stores["admin_users"]
    app["audit"] = stores["audit"]
    app["settings_store"] = stores["settings"]
    app["recordings"] = stores["recordings"]
    app["epg"] = stores["epg"]
    app["session"] = session

    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/guide", handle_guide_calendar5)
    app.router.add_get("/guide/data", handle_guide_data)
    app.router.add_post("/guide/record", handle_guide_record)
    app.router.add_get("/epg/guide.xml", lambda r: web.FileResponse(str(EPG_ROOT / "guide.xml"), headers={"Content-Type": "application/xml"}))
    app.router.add_get("/player", handle_player)
    app.router.add_get("/record", handle_record_page)
    app.router.add_get("/record/channels", handle_record_channels)
    app.router.add_post("/record", handle_record_create)
    app.router.add_get("/favorites", handle_favorites)
    app.router.add_post("/favorites", handle_favorites)
    app.router.add_get("/recordings", handle_viewer_recordings)
    app.router.add_post("/recordings/delete-all", handle_viewer_recordings_delete_all)
    app.router.add_get("/rec/{token}", handle_record_page)
    app.router.add_post("/rec/{token}", handle_record_create)
    app.router.add_get("/playlist.m3u", handle_playlist_m3u)
    app.router.add_get("/tv/{code}", handle_playlist_m3u)
    app.router.add_get("/recording/{id}.ts", handle_recording_file)
    app.router.add_get(
        "/live/{name:.+\\.m3u8}",
        lambda r: handle_playlist(r, session),
    )
    app.router.add_get(
        "/tv/{code}/{name:.+\\.m3u8}",
        lambda r: handle_playlist(r, session),
    )
    app.router.add_get("/proxy", lambda r: handle_segment(r, session))

    # Admin subapp
    from . import admin as admin_mod
    admin_app = admin_mod.make_admin_subapp(stores)
    app["sessions"] = admin_app["sessions_registry"]
    app.add_subapp("/admin", admin_app)

    # Versioned management API.  Authentication is separate from viewer tokens.
    from . import api as api_mod
    api_app = api_mod.make_api_subapp(stores)
    api_app["sessions"] = app["sessions"]
    app.add_subapp("/api/v1", api_app)
    app.router.add_get("/api/openapi.json", api_mod.api_openapi)
    app.router.add_get("/api/docs", api_mod.api_docs)

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _run() -> None:
    _configure_logging()

    from .stores import (
        AdminUserStore,
        ApiTokenStore,
        RecordingStore,
        AuditStore,
        ChannelStore,
        ConnectionStore,
        SettingsStore,
        ViewerStore,
    )
    from .admin import SessionRegistry

    stores = {
        "viewers":     ViewerStore(DB_PATH),
        "channels":    ChannelStore(DB_PATH),
        "connections": ConnectionStore(DB_PATH),
        "admin_users": AdminUserStore(DB_PATH),
        "api_tokens":  ApiTokenStore(DB_PATH),
        "recordings":  RecordingStore(DB_PATH),
        "audit":       AuditStore(DB_PATH),
        "settings":    SettingsStore(DB_PATH),
        "epg":         EpgStore(DB_PATH),
    }
    for s in stores.values():
        if hasattr(s, "init_schema"):
            s.init_schema()

    logger.info("starting listen=%s:%s version=%s", LISTEN_HOST, LISTEN_PORT, APP_VERSION)

    connector = aiohttp.TCPConnector(limit=20, limit_per_host=10, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, connect=UPSTREAM_CONNECT_TIMEOUT)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    epg_task = asyncio.create_task(run_daily(DB_PATH))
    app = _make_app(stores, session)
    runner = web.AppRunner(app, access_log=None, handle_signals=False)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_HOST, LISTEN_PORT))
    site = web.SockSite(runner, sock)
    await site.start()

    logger.info("listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        epg_task.cancel()
        await asyncio.gather(epg_task, return_exceptions=True)
        await runner.cleanup()
        await session.close()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
