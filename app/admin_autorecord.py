"""Admin page /admin/autorecord: automatic recording rules and what is coming up.

Rules can be switched on and off here (adding/editing stays on the command line,
`python -m app.autorecord`). Switching off also removes the rule's scheduled
recordings; switching on plans them right away. "Plan now" runs the planner
immediately instead of waiting for the 30-minute timer.
"""

from __future__ import annotations

import asyncio
import html
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web

from . import autorecord as ar

DISPLAY_TZ = ZoneInfo(os.environ.get("TVPROXY_DISPLAY_TZ", "Europe/Istanbul"))
STATUS = {  # display_status: (background, text colour, label)
    "scheduled":     ("#1f6f43", "#d7ffe8", "Scheduled"),
    "recording now": ("#b71c1c", "#ffe5e5", "Recording now"),
    "partial":       ("#8a5a00", "#fff1cc", "Partial"),
    "rerun":         ("#14507a", "#dcefff", "Rerun"),
    "conflict":      ("#5a2330", "#ffd9df", "Not recorded"),
    "skipped":       ("#3a3f47", "#c9d1d9", "Skipped"),
}
KIND = {"f1": "F1 calendar", "team": "Team fixtures", "epg": "TV guide"}


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%a %d-%m %H:%M")


def _badge(status: str) -> str:
    bg, fg, label = STATUS.get(status, ("#3a3f47", "#c9d1d9", status))
    return (f'<span style="background:{bg};color:{fg};padding:2px 9px;border-radius:999px;'
            f'font-size:.85em;white-space:nowrap">{_e(label)}</span>')


def render_body(db, csrf_field: str, flash: str = "", now: float | None = None) -> str:
    rules = [dict(r) for r in db.execute("SELECT * FROM autorecord_rules ORDER BY priority, id")]
    items = ar.upcoming(db, now=now)
    per_rule: dict[int, int] = {}
    for it in items:
        if it["display_status"] in ("scheduled", "partial", "rerun", "recording now"):
            per_rule[it["rule_id"]] = per_rule.get(it["rule_id"], 0) + 1

    rows = []
    for r in rules:
        on = bool(r["enabled"])
        state = ('<span style="color:#7ee2a8;font-weight:600">On</span>' if on
                 else '<span style="color:#ff9b8e;font-weight:600">Off</span>')
        danger = ' class="danger"' if on else ""
        dim = "" if on else ' style="opacity:.55"'
        new_value, label = ("0", "Turn off") if on else ("1", "Turn on")
        button = (f'<form class="inline" method="post" action="/admin/autorecord/{r["id"]}/toggle">'
                  f'{csrf_field}<input type="hidden" name="enabled" value="{new_value}">'
                  f'<button type="submit"{danger}>{label}</button></form>')
        rows.append(
            f'<tr{dim}>'
            f'<td>{r["priority"]}</td><td><strong>{_e(r["name"])}</strong><br>'
            f'<span class="muted">{_e(ar.describe_rule(db, r))}</span></td>'
            f'<td>{_e(KIND.get(r["kind"], r["kind"]))}</td>'
            f'<td>-{r["before_min"]} / +{r["after_min"]} min</td>'
            f'<td>{per_rule.get(r["id"], 0)}</td><td>{state}</td><td>{button}</td></tr>')
    rules_table = (
        '<table><thead><tr><th>Priority</th><th>Rule</th><th>Source</th><th>Margin</th>'
        '<th>Planned</th><th>Status</th><th></th></tr></thead><tbody>'
        + ("".join(rows) or '<tr><td colspan="7" class="muted">No rules yet.</td></tr>')
        + '</tbody></table>')

    up_rows = []
    for it in items:
        when = _when(it["when"])
        if it["start_at"] and it["end_at"]:
            when += "&ndash;" + datetime.fromtimestamp(it["end_at"], DISPLAY_TZ).strftime("%H:%M")
        note = it["note"] if it["display_status"] in ("conflict", "skipped") else ""
        dim = "" if it["enabled"] else ' style="opacity:.55"'
        extra = f'<br><span class="muted">{_e(note)}</span>' if note else ""
        up_rows.append(
            f'<tr{dim}><td style="white-space:nowrap">{when}</td>'
            f'<td>{_e(it["title"])}{extra}</td>'
            f'<td>{_e(it["channel"] or "")}</td><td>{_e(it["rule"])}</td>'
            f'<td>{_badge(it["display_status"])}</td></tr>')
    upcoming_table = (
        '<table><thead><tr><th>When (Turkish time)</th><th>Programme</th><th>Channel</th>'
        '<th>Rule</th><th>Status</th></tr></thead><tbody>'
        + ("".join(up_rows) or '<tr><td colspan="5" class="muted">Nothing planned yet.</td></tr>')
        + '</tbody></table>')

    plan_now = (f'<form class="inline" method="post" action="/admin/autorecord/run">{csrf_field}'
                '<button type="submit">Plan now</button></form>')
    return (
        flash
        + '<h2>Automatic recording rules</h2>'
        '<p class="muted">Lower priority number wins when recordings overlap (only one stream at a time); '
        'the other one is trimmed around it, or its rerun is recorded. The planner runs every 30 minutes. '
        f'Adding or changing rules: <code>python -m app.autorecord</code> on the Pi. {plan_now}</p>'
        + rules_table
        + '<h2>Coming up (next 30 days)</h2>'
        '<p class="muted">Matches appear about 5 days ahead (when they are in the TV guide); '
        'F1 is planned for the whole season. <em>Partial</em> and <em>Rerun</em> are labelled in the '
        'recording title as well.</p>'
        + upcoming_table)


async def autorecord_get(request: web.Request) -> web.Response:
    from .admin import _emit_csrf_field, _flash_consume, _render
    db = await asyncio.to_thread(ar.connect, ar.DB_PATH)
    try:
        body = await asyncio.to_thread(render_body, db, await _emit_csrf_field(request),
                                       _flash_consume(request.app))
    finally:
        db.close()
    return _render(request, "Auto-record", "/admin/autorecord", body)


def _summary(lines: list[str]) -> str:
    n = {k: sum(1 for x in lines if x.startswith(k)) for k in ("scheduled", "moved", "cancelled", "CONFLICT")}
    parts = [f"{v} {k.lower()}" for k, v in n.items() if v]
    return ", ".join(parts) if parts else "nothing changed"


async def autorecord_toggle(request: web.Request) -> web.Response:
    from .admin import _audit, _flash_set
    rule_id = int(request.match_info["id"])
    enabled = (await request.post()).get("enabled") == "1"

    def work():
        db = ar.connect(ar.DB_PATH)
        try:
            name = db.execute("SELECT name FROM autorecord_rules WHERE id=?", (rule_id,)).fetchone()
            if not name:
                return None, []
            return name[0], ar.set_rule_enabled(db, rule_id, enabled)
        finally:
            db.close()

    name, report = await asyncio.to_thread(work)
    if name is None:
        _flash_set(request.app, "err", "Rule not found.")
        raise web.HTTPFound("/admin/autorecord")
    if enabled:                                   # plan its recordings right away
        try:
            report += await asyncio.wait_for(asyncio.to_thread(ar.run), timeout=60)
        except Exception as exc:                  # the 30-minute timer will catch up
            report.append(f"planning postponed: {type(exc).__name__}")
    await _audit(request, "autorecord.enable" if enabled else "autorecord.disable",
                 "autorecord_rule", rule_id, {"name": name})
    _flash_set(request.app, "ok", f"'{name}' turned {'on' if enabled else 'off'}: {_summary(report)}.")
    raise web.HTTPFound("/admin/autorecord")


async def autorecord_run(request: web.Request) -> web.Response:
    from .admin import _flash_set
    try:
        report = await asyncio.wait_for(asyncio.to_thread(ar.run), timeout=90)
        _flash_set(request.app, "ok", f"Planner ran: {_summary(report)}.")
    except Exception as exc:
        _flash_set(request.app, "err", f"Planner failed: {type(exc).__name__}: {exc}")
    raise web.HTTPFound("/admin/autorecord")


def add_routes(sub: web.Application) -> None:
    sub.router.add_get("/autorecord", autorecord_get)
    sub.router.add_post("/autorecord/run", autorecord_run)
    sub.router.add_post("/autorecord/{id}/toggle", autorecord_toggle)
