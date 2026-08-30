"""NetKit — ONE-file zero-dependency stealth web client (pure Python stdlib).

Everything a browsing agent needs, in one module, no Chrome, no Playwright,
no requests lib. Runs in a few MB of RAM; scale it down by disabling what
you don't use.

Layers (bottom-up, OSI-wise):
  L4/TCP-L7/TLS : Chrome cipher order + ALPN h2/http1.1 via stdlib ssl
  L7/HTTP       : Chrome's exact header ORDER, casing, client hints
  L7/COOKIES    : per-domain jar persisted to the user profile
  BEHAVIOR      : per-user seeded identity + human timing between requests
  FILTER        : DuckDuckGo-style tracker prevention + adblock (inline)

Identity: one user = one stable browser persona (UA, sec-ch-ua, Accept-
Language, think-time rhythm). Different users look like different
machines; the same user looks like the same machine forever.

    from solver.netkit import NetKit
    nk = NetKit(user="lo-agent-1")
    r = nk.get("https://17.wtf/login")          # -> Response
    r.html, r.status, r.headers
    img = nk.fetch_bytes("https://site/captcha.jpg", referer="https://site/")
    text = nk.human_type("hello")               # cadence profile
    posts = nk.x_posts("elonmusk")              # X timeline without login

Dev mode: HARLOG=1 python ... writes a HAR of every request to netkit.har
"""

import gzip
import json
import random
import re
import ssl
import time
import urllib.parse
import zlib
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
from pathlib import Path

try:
    import brotli  # optional ~1MB; without it br responses error loudly
except ImportError:
    brotli = None

# ==========================================================================
# IDENTITY TABLES (Chrome 125-127 era)
# ==========================================================================

USER_AGENTS = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     "Windows", '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"'),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
     "Windows", '"Chromium";v="125", "Not.A/Brand";v="24", "Google Chrome";v="125"'),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
     "Macintosh", '"Chromium";v="127", "Not.A/Brand";v="24", "Google Chrome";v="127"'),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     "Linux", '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"'),
]

ACCEPT_LANGS = ["en-US,en;q=0.9", "en-GB,en;q=0.8,en-US;q=0.7", "en-US,en;q=0.9,hi;q=0.8"]

CHROME_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:"
    "AES256-GCM-SHA384:AES128-SHA:AES256-SHA"
)

# ==========================================================================
# TRACKER PREVENTION (DuckDuckGo-tracker-radar-style domain list)
# ==========================================================================

AD_DOMAINS = frozenset({
    # ad networks
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "adnxs.com", "adsrvr.org", "adform.net",
    "adroll.com", "criteo.com", "criteo.net", "pubmatic.com",
    "rubiconproject.com", "taboola.com", "outbrain.com",
    "media.net", "sharethrough.com", "smartadserver.com", "casalemedia.com",
    "openx.net", "indexww.com", "yieldmo.com", "33across.com",
    # trackers / analytics / beacons
    "google-analytics.com", "googletagmanager.com", "analytics.google.com",
    "scorecardresearch.com", "quantserve.com", "quantcount.com",
    "hotjar.com", "hotjar.io", "mouseflow.com", "fullstory.com",
    "mixpanel.com", "segment.io", "segment.com", "amplitude.com",
    "heapanalytics.com", "kissmetrics.com", "chartbeat.com",
    "nr-data.net", "bugsnag.com",
    # social pixels
    "ct.pinterest.com", "ads-twitter.com", "analytics.twitter.com",
    "px.ads.linkedin.com", "bat.bing.com", "clarity.ms",
    # popunders / redirects
    "popads.net", "popcash.net", "propellerads.com", "adcash.com",
    "adsterra.com", "hilltopads.net", "clickadu.com", "exoclick.com",
    # fingerprinty CDP stuff
    "branch.io", "appsflyer.com", "adjust.com", "kochava.com",
    "onesignal.com", "pushwoosh.com",
    # misc junk
    "moatads.com", "adsafeprotected.com", "doubleverify.com",
    "advertising.com", "teads.tv", "mgid.com", "revcontent.com",
})


def is_ad(url: str) -> bool:
    """True if the URL's host matches a known ad/tracker suffix."""
    try:
        host = url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    except (ValueError, IndexError):
        return False
    host = host.lower().strip(".")
    if not host:
        return False
    for d in AD_DOMAINS:
        if host == d or host.endswith("." + d):
            return True
    return False


# ==========================================================================
# HAR DEV MODE (developer option: HARLOG=1)
# ==========================================================================

class _Har:
    """Optional request log -> ./netkit.har (Chrome HAR 1.2 schema)."""

    def __init__(self, enabled: bool, path: str = "netkit.har"):
        self.on = enabled
        self.path = path
        self.entries = []

    def add(self, method, url, status, ms, req_headers, resp_headers, size):
        if not self.on:
            return
        self.entries.append({
            "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "time": round(ms, 1),
            "request": {"method": method, "url": url,
                        "headers": [{"name": k, "value": v} for k, v in req_headers]},
            "response": {"status": status,
                         "headers": [{"name": k, "value": v} for k, v in resp_headers.items()],
                         "content": {"size": size}},
        })

    def flush(self):
        if not self.on or not self.entries:
            return
        har = {"log": {"version": "1.2", "creator": {"name": "netkit"},
                       "entries": self.entries}}
        Path(self.path).write_text(json.dumps(har, indent=1))


# ==========================================================================
# RESPONSE
# ==========================================================================

class Response:
    """Parsed HTTP response with a requests-like surface."""

    def __init__(self, status: int, headers: dict, body: bytes, url: str):
        self.status = status
        self.headers = headers
        self.body = body
        self.url = url
        self._text = None

    @property
    def text(self) -> str:
        if self._text is None:
            m = re.search(rb"charset=([\w-]+)", self.body[:2048])
            charset = m.group(1).decode() if m else "utf-8"
            try:
                self._text = self.body.decode(charset, errors="replace")
            except LookupError:
                self._text = self.body.decode("utf-8", errors="replace")
        return self._text

    html = text  # alias: page HTML and API text share the same decode

    def json(self):
        return json.loads(self.body)

    def save(self, path: str):
        Path(path).write_bytes(self.body)

    def __repr__(self):
        return f"<Response [{self.status}] {len(self.body)}b {self.url[:60]}>"


# ==========================================================================
# NETKIT
# ==========================================================================

def _seeded(user: str) -> random.Random:
    """Deterministic per-user RNG: same user -> same fingerprint forever.

    blake2b, not int.from_bytes: short ASCII names ('u0'..'u5') collide
    badly under a direct byte-int read (measured: identical UAs across
    six 'different' users). A real hash spreads them.
    """
    import hashlib
    h = int.from_bytes(hashlib.blake2b(("netkit:" + user).encode(),
                                       digest_size=8).digest(), "big")
    return random.Random(h)


class NetKit:
    """Stealth web client bound to one user identity + profile directory."""

    def __init__(self, user: str = "agent-1", profile_root: str = "~/.solver/profiles",
                 human_timing: bool = True, adblock: bool = True,
                 timeout: float = 30.0):
        self.user = user
        self.rng = _seeded(user)
        self.ua, self.platform, self.sec_ch_ua = self.rng.choice(USER_AGENTS)
        self.lang = self.rng.choice(ACCEPT_LANGS)
        self.timeout = timeout
        self.human_timing = human_timing
        self.adblock = adblock
        self.profile_dir = Path(profile_root).expanduser() / user
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.profile_dir / "cookies.json"
        self._domain_cookies: dict[str, dict[str, str]] = {}
        self._load_cookies()
        self._last_ts = 0.0
        self.har = _Har(bool(__import__("os").environ.get("HARLOG")))
        self._typo_rng = random.Random()  # typing cadence isn't identity-bound

    # -------------------------------------------------------------- identity

    def identity(self) -> dict:
        """The persona this user presents (stable across restarts)."""
        return {
            "user": self.user, "ua": self.ua, "platform": self.platform,
            "sec_ch_ua": self.sec_ch_ua, "accept_language": self.lang,
            "profile_dir": str(self.profile_dir),
            "tracker_prevention": self.adblock,
        }

    # -------------------------------------------------------------- cookies

    def _load_cookies(self):
        if self.cookie_file.exists():
            try:
                self._domain_cookies = json.loads(self.cookie_file.read_text())
            except (json.JSONDecodeError, OSError):
                self._domain_cookies = {}

    def _save_cookies(self):
        try:
            self.cookie_file.write_text(json.dumps(self._domain_cookies))
        except OSError:
            pass  # read-only fs: cookies live for this session only

    def _cookies_for(self, host: str) -> str:
        parts = host.split(".")
        candidates = {host, ".".join(parts[-2:]) if len(parts) >= 2 else ""}
        jar: dict[str, str] = {}
        for dom, cks in self._domain_cookies.items():
            if dom in candidates:
                jar.update(cks)
        return "; ".join(f"{k}={v}" for k, v in jar.items())

    def _store_cookies(self, host: str, msg) -> None:
        for raw in msg.get_all("set-cookie", []) or []:
            first = raw.split(";", 1)[0]
            if "=" in first:
                name, _, value = first.partition("=")
                self._domain_cookies.setdefault(host, {})[name.strip()] = value.strip()
        self._save_cookies()

    def cookies_of(self, domain: str = "") -> list[dict]:
        """Jar snapshot; filter by domain substring if given."""
        out = []
        for dom, cks in self._domain_cookies.items():
            if not domain or domain in dom:
                out += [{"domain": dom, "name": k, "value": v} for k, v in cks.items()]
        return out

    # -------------------------------------------------------------- TLS

    def _ssl_context(self) -> ssl.SSLContext:
        """Chrome-profiled TLS with a CA bundle that actually exists.

        Bare Kali/Android images ship a broken default verify path
        (measured: CERTIFICATE_VERIFY_FAILED on every host). certifi —
        already present with any pip install — carries the full Mozilla
        CA set; requests uses the same bundle.
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.set_ciphers(CHROME_CIPHERS)
        except ssl.SSLError:
            pass  # restricted OpenSSL: defaults still interoperate
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        # Offer ALPN but ONLY http/1.1: stdlib http.client cannot speak h2,
        # and a negotiated-h2 server sends binary frames that explode as
        # BadStatusLine (measured on 17.wtf + Cloudflare). Keeping the
        # ALPN extension present preserves the Chrome-family extension list.
        ctx.set_alpn_protocols(["http/1.1"])
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except ImportError:
            try:
                ctx.load_default_certs()  # last resort; may be empty on Android
            except ssl.SSLError:
                pass
        return ctx

    # -------------------------------------------------------------- human

    def _think(self):
        """Inter-request pause that models a human reading a page."""
        if not self.human_timing:
            return
        now = time.time()
        if self._last_ts:
            if now - self._last_ts < 0.35:
                time.sleep(self.rng.uniform(0.35, 1.9))
            elif self.rng.random() < 0.12:
                time.sleep(self.rng.uniform(0.5, 2.5))  # occasional long read
        self._last_ts = time.time()

    def human_type(self, text: str) -> list[float]:
        """Cadence profile (seconds per key) for human-like typing.

        Returns the actual pauses used — feed them to any UI layer so
        key timing matches this identity's rhythm.
        """
        pauses = []
        for ch in text:
            base = self._typo_rng.uniform(0.045, 0.16)
            if self._typo_rng.random() < 0.05:  # thinking hiccup
                base += self._typo_rng.uniform(0.2, 0.6)
            pauses.append(base)
            time.sleep(base)
        return pauses

    # -------------------------------------------------------------- headers

    def _base_headers(self, url: str, referer: str | None,
                      accept: str | None = None, dest: str = "document",
                      site_mode: str = "none") -> list[tuple[str, str]]:
        """Chrome's exact first-request header ORDER (order is a fingerprint)."""
        h = [
            ("Host", urllib.parse.urlparse(url).netloc),
            ("Connection", "keep-alive"),
            ("sec-ch-ua", self.sec_ch_ua),
            ("sec-ch-ua-mobile", "?0"),
            ("sec-ch-ua-platform",
             '"Windows"' if self.platform == "Windows"
             else '"macOS"' if self.platform == "Macintosh" else '"Linux"'),
            ("Upgrade-Insecure-Requests", "1"),
            ("User-Agent", self.ua),
            ("Accept", accept or (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7")),
            ("Sec-Fetch-Site", "same-origin" if referer else site_mode),
            ("Sec-Fetch-Mode", "navigate" if dest == "document" else "no-cors"),
            ("Sec-Fetch-User", "?1"),
            ("Sec-Fetch-Dest", dest),
            ("Accept-Encoding", "gzip, deflate, br, zstd"),
            ("Accept-Language", self.lang),
        ]
        if referer:
            h.insert(6, ("Referer", referer))
        return h

    def _headers_dict(self, headers: list[tuple[str, str]], url: str) -> dict:
        """Ordered dict: http.client sends insertion order; supplying Host +
        Accept-Encoding suppresses its auto-headers so wire order = Chrome's."""
        d = dict(headers)
        cookie = self._cookies_for(urllib.parse.urlparse(url).netloc)
        if cookie:
            d["Cookie"] = cookie
        return d

    # -------------------------------------------------------------- decode

    @staticmethod
    def _decode(resp: HTTPResponse) -> bytes:
        enc = (resp.getheader("Content-Encoding") or "").lower()
        body = resp.read()
        if enc == "gzip" or (enc == "" and body[:2] == b"\x1f\x8b"):
            body = gzip.decompress(body)
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        elif enc == "br":
            if brotli is None:
                raise RuntimeError("brotli body — pip install brotli")
            body = brotli.decompress(body)
        elif enc == "zstd":
            pass  # rare for HTML; body returned raw rather than erroring
        return body

    # -------------------------------------------------------------- core

    def request(self, method: str, url: str, referer: str | None = None,
                extra_headers: dict | None = None, data: bytes | None = None,
                accept: str | None = None, dest: str = "document",
                site_mode: str = "none", max_redirects: int = 5) -> Response:
        """Stealth request with browser-style redirect following."""
        t0 = time.time()
        if self.adblock and is_ad(url):
            return Response(0, {}, b"", url)  # refused locally, never sent

        current, method0 = url, method
        for _ in range(max_redirects + 1):
            p = urllib.parse.urlparse(current)
            host, path = p.netloc, p.path or "/"
            if p.query:
                path += "?" + p.query
            self._think()
            headers = self._base_headers(current, referer, accept, dest)
            for k, v in (extra_headers or {}).items():
                headers.append((k, v))

            if p.scheme == "https":
                conn = HTTPSConnection(host, timeout=self.timeout,
                                       context=self._ssl_context())
            else:
                conn = HTTPConnection(host, timeout=self.timeout)
            try:
                conn.request(method, path, body=data,
                             headers=self._headers_dict(headers, current))
                resp = conn.getresponse()
                raw = resp.getheaders()
                body = self._decode(resp)
                self._store_cookies(host, resp.msg)
                status = resp.status
            finally:
                conn.close()

            hdict = {k.lower(): v for k, v in raw}
            if 300 <= status < 400 and hdict.get("location"):
                nxt = urllib.parse.urljoin(current, hdict["location"])
                if self.adblock and is_ad(nxt):
                    return Response(0, {}, b"", nxt)
                referer, current = current, nxt
                if status in (301, 302, 303):
                    method, data = "GET", None
                continue

            self.har.add(method0, url, status, (time.time() - t0) * 1000,
                         headers, hdict, len(body))
            return Response(status, hdict, body, current)
        raise RuntimeError(f"too many redirects: {url}")

    # -------------------------------------------------------------- sugar

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, form: dict | None = None, json_body: dict | None = None,
             referer: str | None = None, **kw) -> Response:
        """Browser form POST: exactly what a submit button sends."""
        if json_body is not None:
            data = json.dumps(json_body).encode()
            ct = "application/json"
        else:
            data = urllib.parse.urlencode(form or {}).encode()
            ct = "application/x-www-form-urlencoded"
        return self.request(
            "POST", url, data=data, referer=referer,
            extra_headers={"Content-Type": ct,
                           "Origin": urllib.parse.urlparse(url).scheme + "://" + urllib.parse.urlparse(url).netloc},
            **kw)

    def fetch_bytes(self, url: str, referer: str | None = None) -> bytes:
        """<img>/<audio> fetch: binary body with image-tuned Accept."""
        r = self.request(
            "GET", url, referer=referer,
            accept="image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            dest="image", site_mode="cross-site")
        return r.body

    # -------------------------------------------------------------- X (twitter)

    def x_posts(self, user: str, limit: int = 20) -> list[dict]:
        """Fetch X/Twitter posts WITHOUT login via the public syndication API.

        syndication.twitter.com serves the embed-widget timeline: no auth,
        no account. Posts ride inside <script id="__NEXT_DATA__"> as plain
        JSON — parse that, never regex the HTML shell. Raises loudly if
        the endpoint changes instead of returning junk.
        """
        params = {"showReplies": "false", "lang": "en", "domain": "twitter.com"}
        api = (f"https://syndication.twitter.com/srv/timeline-profile/"
               f"screen-name/{urllib.parse.quote(user)}?"
               + urllib.parse.urlencode(params))
        r = self.get(api, accept="*/*", dest="iframe", site_mode="same-site")
        if r.status != 200:
            raise RuntimeError(f"x syndication returned {r.status} for @{user}")
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            raise RuntimeError(f"x syndication shell changed for @{user} (no __NEXT_DATA__)")
        try:
            tree = json.loads(m.group(1))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"x syndication JSON malformed: {e}") from e

        def find_tweets(obj):
            if isinstance(obj, dict):
                if "full_text" in obj and "id_str" in obj:
                    yield obj
                for v in obj.values():
                    yield from find_tweets(v)
            elif isinstance(obj, list):
                for v in obj:
                    yield from find_tweets(v)

        posts = []
        for tw in find_tweets(tree):
            posts.append({
                "id": tw.get("id_str", ""),
                "text": re.sub(r"<[^>]+>", "", tw.get("full_text", "")),
                "created": tw.get("created_at", ""),
                "likes": tw.get("favorite_count", 0),
                "rts": tw.get("retweet_count", 0),
            })
            if len(posts) >= limit:
                break
        return posts[:limit]

    # -------------------------------------------------------------- teardown

    def flush_har(self):
        """Write the collected HAR (developer option)."""
        self.har.flush()
