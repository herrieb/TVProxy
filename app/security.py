"""Pure security helpers for TVProxy.

No I/O. All functions are deterministic and safe to import from
tests without any side effects.
"""

from __future__ import annotations

import hmac
import ipaddress
import socket
from typing import Tuple
from urllib.parse import urlparse


def constant_time_eq(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def token_is_valid(provided: str | None, expected: str) -> bool:
    """True iff provided matches expected with constant-time compare."""
    if not expected:
        return False
    if not provided:
        return False
    if len(provided) != len(expected):
        return False
    return constant_time_eq(provided, expected)


def redact_token(value: str | None) -> str:
    """Redact a token for logging. Never reveals more than a few chars."""
    if not value:
        return "-"
    if len(value) <= 6:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


def ip_is_public(addr: str) -> bool:
    """True iff addr parses and is a routable public address."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    return True


def resolve_and_check(host: str) -> Tuple[bool, str]:
    """Resolve host and ensure every returned address is public."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"dns-fail:{exc}"
    if not infos:
        return False, "dns-empty"
    for info in infos:
        addr = info[4][0]
        if not ip_is_public(addr):
            return False, f"non-public-ip:{addr}"
    return True, "ok"


def validate_upstream_url(url: str, allowed_host: str) -> Tuple[bool, str]:
    """Static URL validation: scheme, host, allowlist match.

    Does NOT do DNS lookups (use validate_upstream_url_runtime for that).
    """
    if not url:
        return False, "no-upstream-configured"
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return False, f"parse-error:{exc}"
    if parsed.scheme not in ("http", "https"):
        return False, f"bad-scheme:{parsed.scheme}"
    host = parsed.hostname or ""
    if not host:
        return False, "no-host"
    if not allowed_host:
        return False, "no-allowed-host-configured"
    if host.lower() != allowed_host.lower():
        return False, f"host-not-allowed:{host}"
    try:
        ipaddress.ip_address(host)
        if not ip_is_public(host):
            return False, f"non-public-upstream-ip:{host}"
    except ValueError:
        # DNS name; defer to runtime check.
        pass
    return True, "ok"


def validate_upstream_url_runtime(url: str, allowed_host: str) -> Tuple[bool, str]:
    """Same as validate_upstream_url but also DNS-resolves and verifies
    every returned address is public."""
    ok, reason = validate_upstream_url(url, allowed_host)
    if not ok:
        return ok, reason
    try:
        ipaddress.ip_address(urlparse(url).hostname or "")
        return True, "ok"
    except ValueError:
        host = urlparse(url).hostname or ""
        ok, msg = resolve_and_check(host)
        if not ok:
            return False, f"dns-block:{msg}"
    return True, "ok"