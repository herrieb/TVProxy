"""Unit tests for app.streaming."""

from app.streaming import (
    classify_hls,
    generate_user_m3u,
    looks_like_playlist,
    parse_m3u_attributes,
    rewrite_playlist,
    safe_host,
)


class TestParseM3UAttributes:
    def test_ext_x_key(self):
        attrs = parse_m3u_attributes(
            '#EXT-X-KEY:METHOD=AES-128,URI="https://x/key.bin",IV=0x9c7db8778570d05c3177c349fd9236aa'
        )
        assert attrs["METHOD"] == "AES-128"
        assert attrs["URI"] == "https://x/key.bin"
        assert attrs["IV"] == "0x9c7db8778570d05c3177c349fd9236aa"

    def test_ext_x_stream_inf(self):
        attrs = parse_m3u_attributes(
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"'
        )
        assert attrs["BANDWIDTH"] == "2000000"
        assert attrs["RESOLUTION"] == "1280x720"
        assert attrs["CODECS"] == "avc1.64001f,mp4a.40.2"

    def test_non_ext_line(self):
        assert parse_m3u_attributes("just a uri") == {}


class TestClassifyHls:
    def test_master_playlist(self):
        body = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
            "high.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
            "low.m3u8\n"
        )
        info = classify_hls(body)
        assert info["kind"] == "master"
        assert info["variants"] == 2
        assert info["extinf_count"] == 0

    def test_media_playlist(self):
        body = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXTINF:5.0,\nseg1.ts\n"
            "#EXTINF:5.0,\nseg2.ts\n"
        )
        info = classify_hls(body)
        assert info["kind"] == "media"
        assert info["variants"] == 0
        assert info["extinf_count"] == 2
        assert info["is_vod"] is False

    def test_vod(self):
        body = (
            "#EXTM3U\n"
            "#EXTINF:5.0,\nseg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        info = classify_hls(body)
        assert info["is_vod"] is True

    def test_empty(self):
        info = classify_hls("")
        assert info["kind"] == "unknown"
        assert info["variants"] == 0


class TestRewritePlaylist:
    BASE = "https://origin.example.com/live/c/"
    PROXY = "https://tv.berrie.uk/proxy"
    TOKEN = "tok123"

    def test_relative_segment(self):
        out = rewrite_playlist(
            "#EXTM3U\nseg1.ts\n", self.BASE, self.PROXY, self.TOKEN
        )
        assert "upstream=" in out
        assert "token=tok123" in out
        # The rewritten URI is percent-encoded inside the proxy query.
        assert "origin.example.com%2Flive%2Fc%2Fseg1.ts" in out

    def test_absolute_segment(self):
        out = rewrite_playlist(
            "#EXTM3U\nhttps://other.example.com/x.ts\n",
            self.BASE, self.PROXY, self.TOKEN,
        )
        # Absolute segment URLs are rewritten to the proxy too; the
        # upstream is percent-encoded inside the query string.
        assert "other.example.com%2Fx.ts" in out
        assert "proxy?" in out

    def test_ext_x_key_uri(self):
        line = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"'
        out = rewrite_playlist(
            "#EXTM3U\n" + line + "\n", self.BASE, self.PROXY, self.TOKEN
        )
        assert "URI=" in out
        assert "key.bin" in out
        # The key URI was rewritten via urljoin against BASE; the
        # resulting absolute upstream is percent-encoded in the query.
        assert "origin.example.com%2Flive%2Fc%2Fkey.bin" in out
        assert "token=tok123" in out

    def test_ext_x_map_uri(self):
        line = '#EXT-X-MAP:URI="init.mp4"'
        out = rewrite_playlist(
            "#EXTM3U\n" + line + "\n", self.BASE, self.PROXY, self.TOKEN
        )
        assert "init.mp4" in out
        assert "origin.example.com%2Flive%2Fc%2Finit.mp4" in out

    def test_ext_x_stream_inf_followed_by_uri(self):
        body = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000000\n"
            "high.m3u8\n"
        )
        out = rewrite_playlist(body, self.BASE, self.PROXY, self.TOKEN)
        assert "high.m3u8" in out
        # The URI is rewritten; the attribute line above it is unchanged.
        assert "#EXT-X-STREAM-INF:BANDWIDTH=2000000" in out

    def test_no_token(self):
        out = rewrite_playlist("#EXTM3U\nseg.ts\n", self.BASE, self.PROXY, None)
        assert "token=" in out
        # token query param present but empty
        assert "&token=" in out

    def test_preserves_blank_lines(self):
        body = "#EXTM3U\n\nseg.ts\n"
        out = rewrite_playlist(body, self.BASE, self.PROXY, self.TOKEN)
        assert "\n\n" in out


class TestLooksLikePlaylist:
    def test_m3u8_extension(self):
        assert looks_like_playlist("https://x.example/c.m3u8", "text/plain")

    def test_mpegurl_content_type(self):
        assert looks_like_playlist(
            "https://x.example/c", "application/vnd.apple.mpegurl"
        )

    def test_unrelated(self):
        assert not looks_like_playlist("https://x.example/seg.ts", "video/mp2t")


class TestSafeHost:
    def test_simple(self):
        assert safe_host("https://x.example/path") == "x.example"

    def test_bogus(self):
        assert safe_host("not-a-url") == "?"


class TestGenerateUserM3u:
    def test_basic(self):
        m3u = generate_user_m3u(
            "https://tv.berrie.uk",
            [
                {"slug": "kempentv", "display_name": "KempenTV", "enabled": True},
                {"slug": "radio", "display_name": "Radio", "enabled": False},
            ],
            "secret-token",
        )
        lines = m3u.splitlines()
        assert lines[0] == "#EXTM3U"
        # disabled channel not present
        assert "radio" not in m3u
        assert "kempentv" in m3u
        # URL points through the proxy
        assert "https://tv.berrie.uk/live/kempentv.m3u8?token=secret-token" in m3u
        # tvg-id present
        assert 'tvg-id="kempentv"' in m3u

    def test_with_logo(self):
        m3u = generate_user_m3u(
            "https://tv.berrie.uk",
            [
                {
                    "slug": "c",
                    "display_name": "C",
                    "logo_url": "https://x/logo.png",
                    "enabled": True,
                }
            ],
            "tok",
        )
        assert 'tvg-logo="https://x/logo.png"' in m3u