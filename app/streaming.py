"""HLS/M3U streaming helpers.

Pure (no I/O) so they are easy to unit test.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlencode, quote


EXTINF_RE = re.compile(r"#EXTINF:([0-9.\-]+),")


def parse_m3u_entries(body: str, base_url: str = "") -> list[dict[str, str]]:
    """Parse a provider M3U into display metadata and absolute stream URLs."""
    entries = []
    pending = None
    for raw in body.splitlines():
        line = raw.strip()
        if line.upper().startswith("#EXTINF"):
            name = line.rsplit(",", 1)[-1].strip() or "Channel"
            attrs = dict(re.findall(r'(?:^|\s)([\w-]+)="([^"]*)"', line))
            pending = {
                "display_name": name,
                "group": attrs.get("group-title", "TVProxy"),
                "logo_url": attrs.get("tvg-logo", ""),
            }
        elif line and not line.startswith("#") and pending is not None:
            pending["url"] = urljoin(base_url, line)
            entries.append(pending)
            pending = None
    return entries


def parse_m3u_attributes(line: str) -> dict[str, str]:
    """Parse an #EXT-X line's KEY=VALUE,VALUE2 list into a dict.

    Quoted values are unquoted. Unquoted values are taken verbatim
    until the next comma.
    """
    if not line.startswith("#EXT"):
        return {}
    body = line.split(":", 1)[1] if ":" in line else ""
    attrs: dict[str, str] = {}
    i = 0
    n = len(body)
    while i < n:
        # find KEY=
        eq = body.find("=", i)
        if eq < 0:
            break
        key = body[i:eq].strip()
        i = eq + 1
        if i >= n:
            attrs[key] = ""
            break
        if body[i] == '"':
            # quoted value
            j = body.find('"', i + 1)
            if j < 0:
                attrs[key] = body[i + 1:]
                break
            attrs[key] = body[i + 1:j]
            i = j + 1
            # skip comma separator
            if i < n and body[i] == ",":
                i += 1
        else:
            # unquoted value: up to next comma
            j = body.find(",", i)
            if j < 0:
                attrs[key] = body[i:].strip()
                break
            attrs[key] = body[i:j].strip()
            i = j + 1
    return attrs


def classify_hls(body: str) -> dict:
    """Inspect an HLS playlist and return simple statistics.

    Returns dict with keys: kind ('master' | 'media' | 'unknown'),
    variants (int), extinf_count (int), is_vod (bool).
    """
    out: dict = {
        "kind": "unknown",
        "variants": 0,
        "extinf_count": 0,
        "is_vod": False,
    }
    if not body:
        return out
    lines = body.splitlines()
    is_master = False
    is_media = False
    extinf = 0
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            is_master = True
            out["variants"] += 1
        elif line.startswith("#EXTINF"):
            is_media = True
            extinf += 1
        elif line.startswith("#EXT-X-ENDLIST"):
            out["is_vod"] = True
    if is_master and not is_media:
        out["kind"] = "master"
    elif is_media and not is_master:
        out["kind"] = "media"
    elif is_master and is_media:
        out["kind"] = "master"
    else:
        out["kind"] = "media" if extinf > 0 else "unknown"
    out["extinf_count"] = extinf
    return out


def rewrite_playlist(
    body: str,
    base_url: str,
    proxy_base_url: str,
    token: Optional[str],
) -> str:
    """Rewrite an HLS playlist so every URI passes through the proxy.

    Handles:
      - relative segment URLs
      - absolute segment URLs
      - child/variant playlists (.m3u8)
      - .ts / .m4s segments
      - EXT-X-KEY URI="..."  (and other URI= attributes)
      - EXT-X-MAP URI="..."
    """
    out_lines: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#EXT"):
            out_lines.append(
                _rewrite_attribute_line(stripped, base_url, proxy_base_url, token)
            )
            continue
        out_lines.append(_rewrite_uri_line(stripped, base_url, proxy_base_url, token))
    return "\n".join(out_lines) + ("\n" if body.endswith("\n") else "")


def _rewrite_uri_line(
    uri: str, base_url: str, proxy_base_url: str, token: Optional[str]
) -> str:
    absolute = urljoin(base_url, uri)
    proxy_qs = urlencode({"upstream": absolute, "token": token or ""})
    return f"{proxy_base_url}?{proxy_qs}"


def _rewrite_attribute_line(
    line: str, base_url: str, proxy_base_url: str, token: Optional[str]
) -> str:
    if "URI=\"" not in line:
        return line
    prefix, rest = line.split("URI=\"", 1)
    uri, after = rest.split("\"", 1)
    absolute = urljoin(base_url, uri)
    proxy_qs = urlencode({"upstream": absolute, "token": token or ""})
    new_uri = f"{proxy_base_url}?{proxy_qs}"
    return f"{prefix}URI=\"{new_uri}\"{after}"


def looks_like_playlist(url: str, content_type: str) -> bool:
    if url.lower().endswith(".m3u8"):
        return True
    if "mpegurl" in content_type.lower():
        return True
    return False


def safe_host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


def generate_user_m3u(
    public_url: str,
    channels: list[dict],
    token: str,
    short_code: Optional[str] = None,
    recordings: Optional[list[dict]] = None,
    local_recording_url: str = "",
    external_recording_url: str = "",
    external_recording_token: str = "",
    movies: Optional[list[dict]] = None,
    original_urls: bool = False,
) -> str:
    """Render a per-viewer M3U pointing at the proxy's /playlist URLs.

    Each entry is an #EXTINF followed by a URL of the form:
      <public_url>/live/<slug>.m3u8?token=<token>

    When original_urls is True, live entries use the channel's upstream
    URL instead of the proxy URL, so viewers fetch directly from the
    provider.  Movies (if given) use the proxy's /movie/<id> route.
    """
    out = ["#EXTM3U"]
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        attrs = []
        logo = ch.get("logo_url", "")
        if logo and urlparse(logo).scheme in ("http", "https") and urlparse(logo).netloc:
            attrs.append(f'tvg-logo="{_escape_attr(logo)}"')
        attrs.append(f'tvg-id="{_escape_attr(ch.get("slug", ""))}"')
        group = ch.get("group") or ch.get("description") or "TVProxy"
        attrs.append(f'group-title="{_escape_attr(group)}"')
        attr_str = " ".join(attrs)
        name = (ch.get("display_name") or ch.get("slug") or "Channel").replace("\r", " ").replace("\n", " ")
        slug = ch.get("slug") or ""
        upstream = str(ch.get("upstream_path") or "")
        if original_urls and upstream.startswith(("http://", "https://")):
            url = upstream
        elif short_code:
            url = f"{public_url.rstrip('/')}/tv/{quote(short_code, safe='')}/{quote(slug, safe='')}.m3u8"
        else:
            url = f"{public_url.rstrip('/')}/live/{quote(slug, safe='')}.m3u8?token={quote(token, safe='')}"
        out.append(f"#EXTINF:-1 {attr_str},{name}")
        out.append(url)
    for movie in movies or []:
        name = (movie.get("display_name") or "Movie").replace("\r", " ").replace("\n", " ")
        out.append(f'#EXTINF:-1 group-title="Movies",{_escape_attr(name)}')
        out.append(movie["url"])
    for recording in recordings or []:
        name = (recording.get("title") or f"Recording {recording.get('id', '')}").replace("\r", " ").replace("\n", " ")
        filename = recording.get("external_filename") or ""
        local_base = local_recording_url.rstrip('/')
        if local_base.endswith('/iptv') and filename:
            local_url = f"{local_base}/{quote(filename, safe='')}?token={quote(external_recording_token, safe='')}"
        else:
            local_url = f"{local_base}/recording/{recording['id']}.ts?token={quote(token, safe='')}"
        out.append(f'#EXTINF:-1 group-title="Local",{_escape_attr(name)}')
        out.append(local_url)
        if external_recording_url and external_recording_token and filename:
            out.append(f'#EXTINF:-1 group-title="External hosted",{_escape_attr(name)}')
            out.append(f"{external_recording_url.rstrip('/')}/{quote(filename, safe='')}?token={quote(external_recording_token, safe='')}")
    return "\n".join(out) + "\n"


def _escape_attr(value: str) -> str:
    return value.replace('"', '\\"')
