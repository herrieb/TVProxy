"""Versioned JSON management API."""

from __future__ import annotations

import base64
import os
import shutil
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from .security import validate_upstream_url
from .recorder import storage_status

_AUTH_FAILURES: dict[str, list[float]] = {}


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return "[configured]" if value else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def _public_viewer(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("token", "allowed_channels")}


def _public_channel(row: dict) -> dict:
    out = dict(row)
    if str(out.get("upstream_path", "")).startswith(("http://", "https://")):
        # Imported provider URLs may embed credentials in path segments.
        out["upstream_path"] = "[configured]"
        out["upstream_configured"] = True
    return out


def _public_connection(row: dict) -> dict:
    out = dict(row)
    out.pop("auth_header", None)
    out["base_url"] = _safe_url(row.get("base_url", ""))
    out["auth_configured"] = bool(row.get("auth_header"))
    return out


def _page(request: web.Request):
    try:
        page = max(1, int(request.query.get("page", "1")))
        per_page = min(200, max(1, int(request.query.get("per_page", "50"))))
    except ValueError:
        raise web.HTTPBadRequest(text="invalid pagination")
    return page, per_page


def _paged(items: list, page: int, per_page: int) -> dict:
    total = len(items)
    return {"items": items[(page - 1) * per_page: page * per_page],
            "pagination": {"page": page, "per_page": per_page, "total": total,
                            "pages": (total + per_page - 1) // per_page}}


async def api_auth_middleware(request: web.Request, handler):
    allowed_origins = {x.strip() for x in os.environ.get("API_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        if origin not in allowed_origins:
            return _error("CORS_DENIED", "Origin is not allowed", 403)
        return web.Response(status=204, headers={
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
        })
    if request.path in ("/api/v1", "/api/v1/"):
        response = await handler(request)
        if origin in allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response
    authorization = request.headers.get("Authorization", "")
    if not authorization.lower().startswith("bearer "):
        return _error("AUTH_REQUIRED", "Authorization: Bearer <API_TOKEN> is required", 401)
    token = authorization[7:].strip()
    if not token or "?" in token:
        return _error("INVALID_TOKEN", "Invalid management API token", 401)
    ip = request.remote or "unknown"
    if len([stamp for stamp in _AUTH_FAILURES.get(ip, []) if time.time() - stamp < 60]) >= 20:
        return _error("RATE_LIMITED", "Too many failed authentication attempts", 429)
    api_user = await request.app["api_tokens"].authenticate(token)
    if not api_user:
        now = time.time()
        ip = request.remote or "unknown"
        recent = [stamp for stamp in _AUTH_FAILURES.get(ip, []) if now - stamp < 60]
        recent.append(now)
        _AUTH_FAILURES[ip] = recent[-20:]
        return _error("INVALID_TOKEN", "Invalid management API token", 401)
    request["api_token"] = api_user
    response = await handler(request)
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


async def api_info(request):
    return web.json_response({"name": "TVProxy Management API", "version": "1",
                              "documentation": "/api/docs", "openapi": "/api/openapi.json"})


async def api_health(request):
    return web.json_response({"status": "ok"})


async def api_options(request):
    return web.Response(status=204)


async def api_tokens(request):
    if request.method == "GET":
        return web.json_response({"items": await request.app["api_tokens"].list_all()})
    try:
        data = await request.json()
        name = str(data["name"]).strip()
        if not name:
            raise ValueError
        token, row = await request.app["api_tokens"].create(name, str(data.get("description", "")), data.get("expires_at"))
    except (KeyError, TypeError, ValueError):
        return _error("VALIDATION_ERROR", "name is required", 422)
    return web.json_response({"token": token, "token_info": row}, status=201)


async def api_token_delete(request):
    try:
        token_id = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid API token id", 400)
    if not await request.app["api_tokens"].delete(token_id):
        return _error("TOKEN_NOT_FOUND", "API token not found", 404)
    return web.Response(status=204)


async def api_users(request):
    store = request.app["viewers"]
    page, per_page = _page(request)
    items = [_public_viewer(v) for v in await store.list_all()]
    search = request.query.get("search", "").casefold()
    if search:
        items = [v for v in items if search in v.get("name", "").casefold()]
    return web.json_response(_paged(items, page, per_page))


async def api_user(request):
    store = request.app["viewers"]
    try:
        uid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid user id", 400)
    user = await store.get(uid)
    if not user:
        return _error("USER_NOT_FOUND", "User not found", 404)
    if request.method == "GET":
        return web.json_response(_public_viewer(user))
    if request.method == "DELETE":
        await store.delete(uid)
        return web.Response(status=204)
    try:
        data = await request.json()
    except Exception:
        return _error("INVALID_JSON", "Request body must be JSON", 400)
    fields = {"name": data.get("name", user["name"]),
              "description": data.get("description", user.get("description", "")),
              "allowed_channel_ids": data.get("allowed_channel_ids", user.get("allowed_channel_ids", [])),
              "max_connections": data.get("max_connections", user.get("max_connections", 1)),
              "expires_at": data.get("expires_at", user.get("expires_at")),
              "disabled": data.get("disabled", bool(user.get("disabled")))}
    if not isinstance(fields["name"], str) or not isinstance(fields["allowed_channel_ids"], list):
        return _error("VALIDATION_ERROR", "Invalid user fields", 422)
    try:
        fields["max_connections"] = max(1, int(fields["max_connections"]))
    except (TypeError, ValueError):
        return _error("VALIDATION_ERROR", "max_connections must be an integer", 422)
    ok, reason = await store.update(uid, fields["name"], fields["description"],
                                    fields["allowed_channel_ids"], fields["max_connections"],
                                    fields["expires_at"], bool(fields["disabled"]))
    if not ok:
        return _error("UPDATE_FAILED", reason, 422)
    return web.json_response(_public_viewer(await store.get(uid)))


async def api_create_user(request):
    try:
        data = await request.json()
        name = str(data["name"]).strip()
    except (Exception, KeyError):
        return _error("VALIDATION_ERROR", "name is required", 422)
    store = request.app["viewers"]
    ok, reason, user = await store.create(name, str(data.get("description", "")),
                                          data.get("allowed_channel_ids", []),
                                          int(data.get("max_connections", 1)), data.get("expires_at"))
    if not ok:
        return _error("CREATE_FAILED", reason, 422)
    response = _public_viewer(user)
    response["viewer_token"] = user["token"]
    return web.json_response(response, status=201)


async def api_regenerate_user_token(request):
    try:
        uid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid user id", 400)
    token = await request.app["viewers"].regenerate_token(uid)
    if not token:
        return _error("USER_NOT_FOUND", "User not found", 404)
    return web.json_response({"viewer_token": token})


async def api_regenerate_short_code(request):
    try:
        uid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid user id", 400)
    code = await request.app["viewers"].regenerate_short_code(uid)
    if not code:
        return _error("USER_NOT_FOUND", "User not found", 404)
    return web.json_response({"short_code": code, "playlist_path": f"/tv/{code}"})


async def api_short_access(request):
    try:
        uid = int(request.match_info["id"])
        data = await request.json()
        enabled = bool(data["enabled"])
    except (ValueError, KeyError, TypeError):
        return _error("VALIDATION_ERROR", "enabled boolean is required", 422)
    if not await request.app["viewers"].set_short_access(uid, enabled):
        return _error("USER_NOT_FOUND", "User not found", 404)
    return web.json_response({"enabled": enabled})


async def api_channels(request):
    page, per_page = _page(request)
    channels = [_public_channel(c) for c in await request.app["channels"].list_all()]
    conns = {c["id"]: c for c in await request.app["connections"].list_all()}
    for c in channels:
        c["connection_name"] = conns.get(c["connection_id"], {}).get("name")
    if request.method == "GET":
        return web.json_response(_paged(channels, page, per_page))
    try:
        data = await request.json()
        required = ["slug", "display_name", "connection_id", "upstream_path"]
        if any(k not in data for k in required):
            raise ValueError
        ok, reason = await request.app["channels"].create(
            str(data["slug"]), str(data["display_name"]), str(data.get("description", "")),
            str(data.get("logo_url", "")), int(data["connection_id"]), str(data["upstream_path"]))
    except (ValueError, TypeError, KeyError):
        return _error("VALIDATION_ERROR", "Invalid channel fields", 422)
    if not ok:
        return _error("CREATE_FAILED", reason, 409)
    return web.json_response(_public_channel(await request.app["channels"].get_by_slug(str(data["slug"]))), status=201)


async def api_channel(request):
    try:
        cid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid channel id", 400)
    channel = await request.app["channels"].get(cid)
    if not channel:
        return _error("CHANNEL_NOT_FOUND", "Channel not found", 404)
    if request.method == "GET":
        return web.json_response(_public_channel(channel))
    if request.method == "DELETE":
        await request.app["channels"].delete(cid)
        return web.Response(status=204)
    try:
        data = await request.json()
        values = {k: data.get(k, channel.get(k, "")) for k in ("slug", "display_name", "description", "logo_url", "connection_id", "upstream_path", "enabled")}
        values["connection_id"] = int(values["connection_id"])
        ok, reason = await request.app["channels"].update(cid, values["slug"], values["display_name"], values["description"], values["logo_url"], values["connection_id"], values["upstream_path"], bool(values["enabled"]))
    except (ValueError, TypeError):
        return _error("VALIDATION_ERROR", "Invalid channel fields", 422)
    if not ok:
        return _error("UPDATE_FAILED", reason, 422)
    return web.json_response(_public_channel(await request.app["channels"].get(cid)))


async def api_channel_test(request):
    try:
        cid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid channel id", 400)
    channel = await request.app["channels"].get(cid)
    if not channel:
        return _error("CHANNEL_NOT_FOUND", "Channel not found", 404)
    connection = await request.app["connections"].get(channel["connection_id"])
    if not connection:
        return _error("CONNECTION_NOT_FOUND", "Connection not found", 404)
    target = channel["upstream_path"]
    if not target.startswith(("http://", "https://")):
        target = urljoin(connection["base_url"].rstrip("/") + "/", target.lstrip("/"))
    started = time.time()
    try:
        headers = {"Authorization": connection["auth_header"]} if connection.get("auth_header") else {}
        timeout = aiohttp.ClientTimeout(sock_connect=5, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(target, headers=headers, allow_redirects=True) as response:
                return web.json_response({
                    "status": "online" if response.status < 400 else "offline",
                    "http_status": response.status,
                    "response_time_ms": int((time.time() - started) * 1000),
                    "playlist_type": "hls" if "mpegurl" in response.headers.get("Content-Type", "").lower() or str(response.url).lower().endswith(".m3u8") else "mpegts",
                })
    except Exception as exc:
        return web.json_response({"status": "offline", "response_time_ms": int((time.time() - started) * 1000), "error": type(exc).__name__}, status=502)


async def api_connection_test(request):
    try:
        cid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid connection id", 400)
    connection = await request.app["connections"].get(cid)
    if not connection:
        return _error("CONNECTION_NOT_FOUND", "Connection not found", 404)
    started = time.time()
    try:
        headers = {"Authorization": connection["auth_header"]} if connection.get("auth_header") else {}
        timeout = aiohttp.ClientTimeout(sock_connect=5, sock_read=10)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(connection["base_url"], headers=headers, allow_redirects=False) as response:
                return web.json_response({"status": "online" if response.status < 400 else "offline", "http_status": response.status, "response_time_ms": int((time.time() - started) * 1000)})
    except Exception as exc:
        return web.json_response({"status": "offline", "response_time_ms": int((time.time() - started) * 1000), "error": type(exc).__name__}, status=502)


async def api_connections(request):
    page, per_page = _page(request)
    if request.method == "GET":
        return web.json_response(_paged([_public_connection(c) for c in await request.app["connections"].list_all()], page, per_page))
    try:
        data = await request.json()
        base_url = str(data["base_url"])
        host = str(data.get("allowed_host") or urlparse(base_url).hostname or "")
        valid, reason = validate_upstream_url(base_url, host)
        if not valid:
            return _error("INVALID_UPSTREAM", reason, 422)
        ok, reason = await request.app["connections"].create(str(data["name"]), base_url, str(data.get("auth_header", "")), host, int(data.get("timeout_connect", 5)), int(data.get("timeout_read", 30)))
    except (KeyError, ValueError, TypeError):
        return _error("VALIDATION_ERROR", "Invalid connection fields", 422)
    if not ok:
        return _error("CREATE_FAILED", reason, 409)
    rows = await request.app["connections"].list_all()
    return web.json_response(_public_connection(next(c for c in rows if c["name"] == data["name"])), status=201)


async def api_connection(request):
    try:
        cid = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid connection id", 400)
    conn = await request.app["connections"].get(cid)
    if not conn:
        return _error("CONNECTION_NOT_FOUND", "Connection not found", 404)
    if request.method == "GET":
        return web.json_response(_public_connection(conn))
    if request.method == "DELETE":
        if not await request.app["connections"].delete(cid):
            return _error("DELETE_FAILED", "Connection may still have channels", 409)
        return web.Response(status=204)
    try:
        data = await request.json()
        base_url = str(data.get("base_url", conn["base_url"]))
        host = str(data.get("allowed_host", conn["allowed_host"]))
        valid, reason = validate_upstream_url(base_url, host)
        if not valid:
            return _error("INVALID_UPSTREAM", reason, 422)
        values = {"name": str(data.get("name", conn["name"])), "auth_header": str(data.get("auth_header", conn.get("auth_header", ""))), "timeout_connect": int(data.get("timeout_connect", conn["timeout_connect"])), "timeout_read": int(data.get("timeout_read", conn["timeout_read"])), "enabled": bool(data.get("enabled", conn["enabled"]))}
        ok, reason = await request.app["connections"].update(cid, values["name"], base_url, values["auth_header"], host, values["timeout_connect"], values["timeout_read"], values["enabled"])
    except (ValueError, TypeError):
        return _error("VALIDATION_ERROR", "Invalid connection fields", 422)
    if not ok:
        return _error("UPDATE_FAILED", reason, 422)
    return web.json_response(_public_connection(await request.app["connections"].get(cid)))


async def api_sessions(request):
    sessions = await request.app["sessions"].list()
    return web.json_response(_paged(sessions, *_page(request)))


async def api_session_delete(request):
    if not await request.app["sessions"].terminate(request.match_info["id"]):
        return _error("SESSION_NOT_FOUND", "Session not found", 404)
    return web.Response(status=204)


async def api_logs(request):
    page, per_page = _page(request)
    rows = await request.app["audit"].list_recent(limit=200)
    return web.json_response(_paged(rows, page, per_page))


async def api_settings(request):
    store = request.app["settings_store"]
    if request.method == "GET":
        return web.json_response(await store.get_all())
    try:
        data = await request.json()
    except Exception:
        return _error("INVALID_JSON", "Request body must be JSON", 400)
    allowed = {"public_url", "default_max_connections", "default_expires_days", "session_timeout_seconds", "log_retention_days"}
    if not set(data).issubset(allowed):
        return _error("VALIDATION_ERROR", "Unknown or sensitive setting", 422)
    await store.set_many({k: str(v) for k, v in data.items()})
    return web.json_response(await store.get_all())


async def api_system_status(request):
    disk = shutil.disk_usage("/")
    uptime = float(open("/proc/uptime").read().split()[0])
    return web.json_response({"status": "ok", "uptime_seconds": uptime,
                              "disk": {"total": disk.total, "free": disk.free, "used": disk.used}})


def _recording_time(value, tz_name):
    if not isinstance(value, str):
        raise ValueError("time must be ISO-8601")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(timezone.utc).timestamp()


async def api_recordings(request):
    store = request.app["recordings"]
    page, per_page = _page(request)
    viewer_id = request.query.get("viewer_id")
    try:
        viewer_id = int(viewer_id) if viewer_id is not None else None
    except ValueError:
        return _error("INVALID_ID", "Invalid viewer_id", 400)
    if request.method == "GET":
        return web.json_response(_paged(await store.list_all(viewer_id), page, per_page))
    try:
        data = await request.json()
        viewer_id = int(data["viewer_id"])
        channel_id = int(data["channel_id"])
        tz_name = str(data.get("timezone", "UTC"))
        ZoneInfo(tz_name)
        start_at = _recording_time(data["start_at"], tz_name)
        end_at = _recording_time(data["end_at"], tz_name)
        if end_at <= start_at or end_at - start_at > 4 * 3600:
            raise ValueError("recording must be 1 second to 4 hours")
    except (KeyError, TypeError, ValueError):
        return _error("VALIDATION_ERROR", "Invalid recording times or ids", 422)
    viewer = await request.app["viewers"].get(viewer_id)
    channel = await request.app["channels"].get(channel_id)
    if not viewer or not channel:
        return _error("RESOURCE_NOT_FOUND", "Viewer or channel not found", 404)
    allowed = viewer.get("allowed_channel_ids", [])
    if allowed and channel_id not in allowed:
        return _error("CHANNEL_NOT_ALLOWED", "Viewer is not assigned to this channel", 403)
    row = await store.create(viewer_id, channel_id, str(data.get("title", "")), start_at, end_at, tz_name)
    return web.json_response(row, status=201)


async def api_recording(request):
    try:
        recording_id = int(request.match_info["id"])
    except ValueError:
        return _error("INVALID_ID", "Invalid recording id", 400)
    store = request.app["recordings"]
    row = await store.get(recording_id)
    if not row:
        return _error("RECORDING_NOT_FOUND", "Recording not found", 404)
    if request.method == "GET":
        return web.json_response(row)
    if request.method == "DELETE":
        if row["status"] in ("recording", "uploading"):
            return _error("RECORDING_ACTIVE", "Active recordings cannot be deleted", 409)
        await store.delete(recording_id)
        return web.Response(status=204)
    if request.method == "POST":
        if row["status"] != "upload_failed":
            return _error("INVALID_STATE", "Only failed uploads can be retried", 409)
        await store.update(recording_id, status="scheduled", error="")
        return web.json_response(await store.get(recording_id))
    try:
        data = await request.json()
        fields = {}
        if "title" in data:
            fields["title"] = str(data["title"])
        if "start_at" in data or "end_at" in data:
            tz_name = str(data.get("timezone", row["timezone"]))
            fields["timezone"] = tz_name
            fields["start_at"] = _recording_time(data.get("start_at", datetime.fromtimestamp(row["start_at"], timezone.utc).isoformat()), tz_name)
            fields["end_at"] = _recording_time(data.get("end_at", datetime.fromtimestamp(row["end_at"], timezone.utc).isoformat()), tz_name)
            if fields["end_at"] <= fields["start_at"] or fields["end_at"] - fields["start_at"] > 4 * 3600:
                raise ValueError
        if row["status"] not in ("scheduled", "failed", "disk_rejected"):
            return _error("INVALID_STATE", "Only scheduled recordings can be updated", 409)
        await store.update(recording_id, **fields)
        return web.json_response(await store.get(recording_id))
    except (ValueError, TypeError):
        return _error("VALIDATION_ERROR", "Invalid recording update", 422)


async def api_storage(request):
    return web.json_response(storage_status())


OPENAPI = {"openapi": "3.0.3", "info": {"title": "TVProxy Management API", "version": "1.0.0"},
           "servers": [{"url": "/api/v1"}], "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}, "schemas": {
               "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}}, "required": ["code", "message"]}}},
               "Pagination": {"type": "object", "properties": {"page": {"type": "integer"}, "per_page": {"type": "integer"}, "total": {"type": "integer"}, "pages": {"type": "integer"}}},
               "UserCreate": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "allowed_channel_ids": {"type": "array", "items": {"type": "integer"}}, "max_connections": {"type": "integer", "minimum": 1}, "expires_at": {"type": ["number", "null"]}}},
               "UserPatch": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "allowed_channel_ids": {"type": "array", "items": {"type": "integer"}}, "max_connections": {"type": "integer", "minimum": 1}, "expires_at": {"type": ["number", "null"]}, "disabled": {"type": "boolean"}}},
               "ApiTokenCreate": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "expires_at": {"type": ["number", "null"]}}},
               "ConnectionWrite": {"type": "object", "required": ["name", "base_url"], "properties": {"name": {"type": "string"}, "base_url": {"type": "string", "format": "uri"}, "allowed_host": {"type": "string"}, "auth_header": {"type": "string", "writeOnly": True}, "timeout_connect": {"type": "integer"}, "timeout_read": {"type": "integer"}, "enabled": {"type": "boolean"}}},
               "ChannelWrite": {"type": "object", "required": ["slug", "display_name", "connection_id", "upstream_path"], "properties": {"slug": {"type": "string"}, "display_name": {"type": "string"}, "description": {"type": "string"}, "logo_url": {"type": "string"}, "connection_id": {"type": "integer"}, "upstream_path": {"type": "string"}, "enabled": {"type": "boolean"}}}
           }},
           "security": [{"bearerAuth": []}], "paths": {}}

def _doc(summary: str, description: str, body: str | None = None, delete: bool = False) -> dict:
    operation = {"summary": summary, "description": description,
                 "responses": {"200": {"description": "Successful JSON response"},
                                "401": {"description": "Missing or invalid management API token"},
                                "404": {"description": "Resource not found"},
                                "422": {"description": "Validation error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}}}
    if body:
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": body}}}}
    if delete:
        operation["responses"]["204"] = {"description": "Deleted successfully"}
    return operation


OPENAPI["paths"] = {
    "/health": {"get": _doc("Health check", "Requires a management API token. Returns {status: ok} when the API process is responding.")},
    "/api-tokens": {"get": _doc("List management tokens", "Returns token metadata only. Hashes and complete token values are never returned."),
                    "post": _doc("Create management token", "Creates an enabled token. Required JSON: name. Optional: description and expires_at (Unix timestamp). The complete token is returned once in token; save it immediately.", "#/components/schemas/ApiTokenCreate")},
    "/api-tokens/{id}": {"delete": _doc("Revoke management token", "Permanently deletes the token identified by the integer id. The token stops authenticating immediately.", delete=True)},
    "/users": {"get": _doc("List viewers", "Returns viewer IPTV accounts without their secret token. Query parameters: page (default 1), per_page (default 50, maximum 200), and search (case-insensitive name filter)."),
                "post": _doc("Create viewer", "Creates a viewer account. Required JSON: name. Optional: description, allowed_channel_ids (array of channel ids; empty means all), max_connections, and expires_at. Returns the viewer token once as viewer_token.", "#/components/schemas/UserCreate")},
    "/users/{id}": {"get": _doc("Get viewer", "Returns one viewer by integer id, including assignments and usage metadata but never the viewer token."),
                      "patch": _doc("Update viewer", "Partially updates a viewer. Any omitted field keeps its current value. Supported fields: name, description, allowed_channel_ids, max_connections, expires_at, and disabled.", "#/components/schemas/UserPatch"),
                      "delete": _doc("Delete viewer", "Permanently deletes a viewer and its access token.", delete=True)},
    "/users/{id}/regenerate-token": {"post": _doc("Regenerate viewer token", "Invalidates the current viewer IPTV token and returns a newly generated viewer_token. The new token is returned only in this response.")},
    "/channels": {"get": _doc("List channels", "Returns channels with ids, slugs, display metadata, connection ids, enabled state, and health-check fields. Supports page and per_page."),
                   "post": _doc("Create channel", "Creates a channel mapping. Required JSON: slug, display_name, connection_id, and upstream_path. upstream_path may be relative or absolute; upstream allowlist validation still applies.", "#/components/schemas/ChannelWrite")},
    "/channels/{id}": {"get": _doc("Get channel", "Returns one channel by integer id. Absolute upstream paths are replaced with [configured] to prevent credential leakage."),
                        "patch": _doc("Update channel", "Partially updates channel metadata and routing. Supported fields: slug, display_name, description, logo_url, connection_id, upstream_path, enabled.", "#/components/schemas/ChannelWrite"),
                        "delete": _doc("Delete channel", "Permanently deletes a channel mapping.", delete=True)},
    "/channels/{id}/test": {"post": _doc("Test channel", "Fetches the configured channel through its upstream connection and returns status, HTTP status, response_time_ms, and detected playlist_type (hls or mpegts).")},
    "/connections": {"get": _doc("List upstream connections", "Returns upstream connection metadata. auth_header is never returned; auth_configured indicates whether one exists. Supports page and per_page."),
                      "post": _doc("Create upstream connection", "Creates an upstream connection. Required JSON: name and base_url. Optional: allowed_host, auth_header (write-only), timeout_connect, timeout_read, and enabled. The URL is validated against the host allowlist.", "#/components/schemas/ConnectionWrite")},
    "/connections/{id}": {"get": _doc("Get upstream connection", "Returns one connection without its authorization header or URL query credentials."),
                            "patch": _doc("Update upstream connection", "Partially updates connection settings. Send auth_header only to replace it; the current value is never returned.", "#/components/schemas/ConnectionWrite"),
                            "delete": _doc("Delete upstream connection", "Deletes a connection when no channels reference it.", delete=True)},
    "/connections/{id}/test": {"post": _doc("Test upstream connection", "Requests the configured base_url and returns status, HTTP status, and response_time_ms. Credentials are not included in the response.")},
    "/sessions": {"get": _doc("List active sessions", "Returns currently tracked viewer sessions: session id, viewer/channel ids, source IP, user agent, started_at, and last_seen. Stale sessions are reaped automatically.")},
    "/sessions/{id}": {"delete": _doc("Terminate active session", "Terminates the in-memory viewer session identified by its session id.", delete=True)},
    "/logs": {"get": _doc("List audit logs", "Read-only audit log endpoint. Supports page and per_page. Sensitive tokens, authorization headers, and upstream credentials are not returned.")},
    "/settings": {"get": _doc("Read settings", "Returns known application settings. Secrets and environment variables are not exposed."),
                  "patch": _doc("Update settings", "Updates only known non-secret settings. Accepted JSON keys: public_url, default_max_connections, default_expires_days, session_timeout_seconds, and log_retention_days.")},
    "/system/status": {"get": _doc("Read system status", "Returns safe host status including API status, process uptime, and root filesystem total/used/free bytes. It does not expose environment variables or filesystem paths.")},
    "/storage": {"get": _doc("Read recording storage", "Returns total, used, and free bytes plus the mandatory 10 GB reserve and space available for recordings.")},
    "/recordings": {"get": _doc("List recordings", "Returns scheduled, active, uploading, completed, and failed recordings. Supports page, per_page, and optional viewer_id."),
                    "post": _doc("Schedule recording", "Creates a recording schedule. Required JSON: viewer_id, channel_id, start_at, and end_at. Times may include an offset or be local ISO-8601 values with timezone naming. Optional: title and timezone. Recordings are limited to 24 hours.")},
    "/recordings/{id}": {"get": _doc("Get recording", "Returns one recording with schedule, status, local file, remote path, and safe error fields."),
                           "patch": _doc("Update scheduled recording", "Updates title, start_at, end_at, and timezone. Only scheduled recordings can be changed."),
                           "delete": _doc("Delete recording", "Deletes a non-active recording schedule. Active recordings cannot be deleted.", delete=True)},
    "/recordings/{id}/retry": {"post": _doc("Retry failed upload", "Moves an upload_failed recording back to scheduled so the worker can record/upload it again.")},
}

for _path, _operations in OPENAPI["paths"].items():
    for _operation in _operations.values():
        _operation.setdefault("tags", [_path.split("/")[1].replace("-", " ").title()])


def make_api_subapp(stores: dict) -> web.Application:
    app = web.Application(middlewares=[api_auth_middleware])
    for key, value in stores.items():
        app[key] = value
    app["settings_store"] = stores["settings"]
    app.router.add_get("", api_info)
    app.router.add_get("/", api_info)
    app.router.add_get("/health", api_health)
    app.router.add_route("OPTIONS", "/{tail:.*}", api_options)
    app.router.add_route("GET", "/api-tokens", api_tokens)
    app.router.add_post("/api-tokens", api_tokens)
    app.router.add_delete("/api-tokens/{id}", api_token_delete)
    app.router.add_get("/users", api_users)
    app.router.add_post("/users", api_create_user)
    app.router.add_route("GET", "/users/{id}", api_user)
    app.router.add_route("PATCH", "/users/{id}", api_user)
    app.router.add_delete("/users/{id}", api_user)
    app.router.add_post("/users/{id}/regenerate-token", api_regenerate_user_token)
    app.router.add_post("/users/{id}/short-code", api_regenerate_short_code)
    app.router.add_patch("/users/{id}/short", api_short_access)
    app.router.add_route("GET", "/channels", api_channels)
    app.router.add_post("/channels", api_channels)
    app.router.add_route("GET", "/channels/{id}", api_channel)
    app.router.add_route("PATCH", "/channels/{id}", api_channel)
    app.router.add_delete("/channels/{id}", api_channel)
    app.router.add_post("/channels/{id}/test", api_channel_test)
    app.router.add_route("GET", "/connections", api_connections)
    app.router.add_post("/connections", api_connections)
    app.router.add_route("GET", "/connections/{id}", api_connection)
    app.router.add_route("PATCH", "/connections/{id}", api_connection)
    app.router.add_delete("/connections/{id}", api_connection)
    app.router.add_post("/connections/{id}/test", api_connection_test)
    app.router.add_get("/sessions", api_sessions)
    app.router.add_delete("/sessions/{id}", api_session_delete)
    app.router.add_get("/logs", api_logs)
    app.router.add_route("GET", "/settings", api_settings)
    app.router.add_patch("/settings", api_settings)
    app.router.add_get("/system/status", api_system_status)
    app.router.add_get("/storage", api_storage)
    app.router.add_get("/recordings", api_recordings)
    app.router.add_post("/recordings", api_recordings)
    app.router.add_get("/recordings/{id}", api_recording)
    app.router.add_patch("/recordings/{id}", api_recording)
    app.router.add_delete("/recordings/{id}", api_recording)
    app.router.add_post("/recordings/{id}/retry", api_recording)
    return app


async def api_openapi(request):
    return web.json_response(OPENAPI)


async def api_docs(request):
    rows = "".join(
        f"<tr><td><code>{method.upper()}</code></td><td><code>{path}</code></td>"
        f"<td>{OPENAPI['paths'][path][method].get('summary', '')}</td></tr>"
        for path in sorted(OPENAPI["paths"])
        for method in sorted(OPENAPI["paths"][path])
    )
    return web.Response(
        text='''<!doctype html>
<html><head><meta charset="utf-8"><title>TVProxy Management API</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"><h1>TVProxy Management API</h1>
<p>Loading interactive documentation...</p>
<h2>Endpoint inventory</h2><table border="1" cellpadding="6"><tr><th>Method</th><th>Path</th><th>Description</th></tr>'''
        + rows + '''</table></div>
<script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>window.ui = SwaggerUIBundle({url: '/api/openapi.json', dom_id: '#swagger-ui'});</script>
</body></html>''',
        content_type="text/html",
    )
