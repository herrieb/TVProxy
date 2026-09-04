"""Unit tests for app.security."""

from app.security import (
    constant_time_eq,
    ip_is_public,
    redact_token,
    resolve_and_check,
    token_is_valid,
    validate_upstream_url,
    validate_upstream_url_runtime,
)


class TestConstantTimeEq:
    def test_equal(self):
        assert constant_time_eq("abc", "abc")

    def test_unequal(self):
        assert not constant_time_eq("abc", "abd")

    def test_empty(self):
        assert constant_time_eq("", "")

    def test_different_lengths(self):
        assert not constant_time_eq("abc", "abcd")


class TestTokenIsValid:
    def test_match(self):
        assert token_is_valid("secret", "secret")

    def test_mismatch(self):
        assert not token_is_valid("secret", "Secret")

    def test_missing(self):
        assert not token_is_valid(None, "secret")
        assert not token_is_valid("", "secret")

    def test_empty_expected(self):
        assert not token_is_valid("anything", "")

    def test_length_mismatch(self):
        assert not token_is_valid("secre", "secret")


class TestRedactToken:
    def test_none(self):
        assert redact_token(None) == "-"

    def test_empty(self):
        assert redact_token("") == "-"

    def test_short(self):
        assert redact_token("abc") == "***"

    def test_long(self):
        assert redact_token("abcdefghij") == "abc***hij"


class TestIpIsPublic:
    def test_public_v4(self):
        assert ip_is_public("8.8.8.8")
        assert ip_is_public("1.1.1.1")

    def test_loopback_v4(self):
        assert not ip_is_public("127.0.0.1")
        assert not ip_is_public("127.255.255.254")

    def test_private_v4(self):
        assert not ip_is_public("10.0.0.1")
        assert not ip_is_public("192.168.1.1")
        assert not ip_is_public("172.16.0.1")

    def test_link_local(self):
        assert not ip_is_public("169.254.169.254")
        assert not ip_is_public("169.254.0.1")

    def test_unspecified(self):
        assert not ip_is_public("0.0.0.0")

    def test_public_v6(self):
        # 2606:4700:: is Cloudflare's range; should be considered public
        assert ip_is_public("2606:4700:4700::1111")

    def test_loopback_v6(self):
        assert not ip_is_public("::1")

    def test_invalid(self):
        assert not ip_is_public("not-an-ip")
        assert not ip_is_public("")


class TestValidateUpstreamUrl:
    def test_valid_https(self):
        ok, reason = validate_upstream_url(
            "https://stream.example.com/live/c.m3u8", "stream.example.com"
        )
        assert ok
        assert reason == "ok"

    def test_valid_http(self):
        ok, _ = validate_upstream_url(
            "http://stream.example.com/c.m3u8", "stream.example.com"
        )
        assert ok

    def test_empty_url(self):
        ok, reason = validate_upstream_url("", "host.example")
        assert not ok
        assert "no-upstream" in reason

    def test_bad_scheme(self):
        ok, reason = validate_upstream_url(
            "ftp://stream.example.com/c", "stream.example.com"
        )
        assert not ok
        assert "bad-scheme" in reason

    def test_host_mismatch(self):
        ok, reason = validate_upstream_url(
            "https://other.example.com/c", "stream.example.com"
        )
        assert not ok
        assert "host-not-allowed" in reason

    def test_host_mismatch_case(self):
        # Allowlist comparison is case-insensitive on the host.
        ok, _ = validate_upstream_url(
            "https://Stream.Example.com/c", "stream.example.com"
        )
        assert ok

    def test_private_ip(self):
        ok, reason = validate_upstream_url(
            "http://10.0.0.5/c.m3u8", "10.0.0.5"
        )
        assert not ok
        assert "non-public" in reason

    def test_loopback_ip(self):
        ok, reason = validate_upstream_url(
            "http://127.0.0.1/c.m3u8", "127.0.0.1"
        )
        assert not ok

    def test_metadata_ip(self):
        ok, _ = validate_upstream_url(
            "http://169.254.169.254/latest", "169.254.169.254"
        )
        assert not ok

    def test_no_host(self):
        ok, reason = validate_upstream_url("https:///path", "")
        assert not ok
        assert "no-host" in reason

    def test_empty_allowed_host(self):
        ok, reason = validate_upstream_url("https://x.example.com/c", "")
        assert not ok
        assert "no-allowed-host" in reason


class TestValidateUpstreamUrlRuntime:
    """The runtime variant additionally DNS-resolves; we only smoke-test
    with IPs (no DNS lookup needed)."""

    def test_public_ip(self):
        ok, _ = validate_upstream_url_runtime(
            "https://8.8.8.8/c.m3u8", "8.8.8.8"
        )
        assert ok

    def test_private_ip_blocked(self):
        ok, _ = validate_upstream_url_runtime(
            "https://10.0.0.1/c.m3u8", "10.0.0.1"
        )
        assert not ok