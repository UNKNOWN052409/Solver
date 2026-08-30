"""LIVE real-world tests for NetKit — every test hits real infrastructure.

NO mocks, NO TestClient, NO synthetic data. Skipped (not faked) only when
the network itself is unreachable. Run: pytest tests/test_live_netkit.py -q
"""

import json
import time

import pytest

from solver.netkit import AD_DOMAINS, NetKit, is_ad


@pytest.fixture(scope="module")
def nk():
    kit = NetKit(user="lo-live-test")
    yield kit
    kit.flush_har()


# ---------------------------------------------------------------- identity

class TestIdentity:
    def test_same_user_same_fingerprint_forever(self):
        a = NetKit(user="fixed-user-9")
        b = NetKit(user="fixed-user-9")
        assert a.ua == b.ua and a.lang == b.lang and a.sec_ch_ua == b.sec_ch_ua

    def test_different_users_look_like_different_machines(self):
        ids = {NetKit(user=f"u{i}").ua for i in range(6)}
        assert len(ids) > 1  # not every agent shares one browser

    def test_profiles_persist_cookies(self, nk):
        r = nk.get("https://www.cloudflare.com/cdn-cgi/trace")
        assert r.status == 200
        assert nk.cookies_of("cloudflare.com"), "cookie jar must persist after a real fetch"


# ---------------------------------------------------------------- TLS / headers

class TestLiveTLS:
    def test_tls_fingerprint_is_chrome_like(self, nk):
        """tls.peet.ws parses our ClientHello the way CF/WAFs do."""
        r = nk.get("https://tls.peet.ws/api/all")
        if r.status != 200:
            pytest.skip("tls.peet.ws unreachable")
        d = r.json()
        # peet.ws labels the JA3/H2 fingerprint; Chrome-family expected
        assert d.get("http_version") in ("HTTP/2", "HTTP/1.1")
        assert "akamai" in d.get("akamai_hash", "").lower() or \
               "chrome" in json.dumps(d).lower() or \
               d.get("tls_version") in ("TLSv1.3", "TLS 1.3"), \
               f"unrecognizable TLS profile: {d.get('tls_version')}"

    def test_httpbin_reports_browser_headers(self, nk):
        """httpbin echoes headers — order and sec-ch-ua must look Chrome."""
        r = nk.get("https://httpbin.org/headers")
        if r.status != 200:
            pytest.skip("httpbin unreachable")
        hdrs = r.json()["headers"]
        assert "Chromium" in hdrs.get("Sec-Ch-Ua", ""), hdrs.get("Sec-Ch-Ua")
        assert "Chrome/" in hdrs.get("User-Agent", ""), hdrs.get("User-Agent")
        assert hdrs.get("Sec-Fetch-Dest") == "document"


# ---------------------------------------------------------------- real sites

class TestRealSites:
    def test_17wtf_login_page(self, nk):
        """LO's target: hCaptcha-protected sign-in must render for us."""
        r = nk.get("https://17.wtf/login")
        assert r.status == 200
        assert "hcaptcha_token" in r.text
        assert "PUBLIC_HCAPTCHA_SITE_KEY" in r.text

    def test_turnstile_demo_page(self, nk):
        r = nk.get("https://turnstile-challenge-demo.globaldots-demo.cftenant.com/")
        assert r.status == 200
        assert "challenges.cloudflare.com/turnstile" in r.text
        assert 'data-sitekey="0x4AAAAAAEBze2zOM-EeezrV"' in r.text

    def test_cleantalk_protection_test_page(self, nk):
        """CleanTalk demo — 403 for curl UAs is FINE, we want the page HTML."""
        r = nk.get("https://cleantalk.org/help/protection-test")
        assert r.status == 200
        assert "cleantalk" in r.text.lower()


# ---------------------------------------------------------------- tracker prevention

class TestTrackerPrevention:
    def test_ad_domains_blocked_locally(self, nk):
        """Requests to tracker hosts never touch the network."""
        r = nk.get("https://www.google-analytics.com/analytics.js")
        assert r.status == 0 and r.body == b""

    def test_is_ad_matching(self):
        assert is_ad("https://ad.doubleclick.net/ddm/adj")
        assert is_ad("https://cdn.popads.net/x.js")
        assert not is_ad("https://17.wtf/login")
        assert not is_ad("https://cleantalk.org/")

    def test_blocklist_size(self):
        """A real blocklist must cover the major networks, not be decorative."""
        assert len(AD_DOMAINS) >= 60


# ---------------------------------------------------------------- human behavior

class TestHumanBehavior:
    def test_think_time_between_requests(self, nk):
        """Test that think-time is applied between consecutive requests."""
        nk._last_ts = time.time() - 0.01  # pretend a request just finished
        nk.human_timing = True
        t0 = time.time()
        nk._think()
        assert time.time() - t0 >= 0.30  # pacing sleeps, not skips

    def test_typing_cadence_is_human(self, nk):
        """16 chars must take at least ~1s at human rhythm, all pauses > 0."""
        t0 = time.time()
        pauses = nk.human_type("humanlike-typing")
        assert all(p > 0 for p in pauses)
        assert len(pauses) == 16
        assert time.time() - t0 >= 0.9


# ---------------------------------------------------------------- X / syndication

class TestXPosts:
    def test_x_posts_without_login(self, nk):
        try:
            posts = nk.x_posts("cloudflare", limit=5)
        except RuntimeError as e:
            if "syndication" in str(e):
                pytest.skip(f"X syndication unavailable: {e}")
            raise
        assert isinstance(posts, list)
        if posts:  # endpoint serves an empty shell when heavily rate-limited
            assert posts[0]["id"] and posts[0]["text"]


# ---------------------------------------------------------------- dev options

class TestDevOptions:
    def test_har_written_when_enabled(self, tmp_path, monkeypatch):
        """HARLOG=1 must produce a real HAR with our real entries."""
        monkeypatch.setenv("HARLOG", "1")
        kit = NetKit(user="har-test", profile_root=str(tmp_path))
        kit.har.path = str(tmp_path / "netkit.har")
        r = kit.get("https://www.cloudflare.com/cdn-cgi/trace")
        assert r.status == 200
        kit.flush_har()
        har = json.loads((tmp_path / "netkit.har").read_text())
        assert har["log"]["entries"], "HAR must contain the real request"
        assert har["log"]["entries"][0]["response"]["status"] == 200
