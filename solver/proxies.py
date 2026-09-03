"""IP-proxy pool — manage, validate, rotate. Browser/requests dono ke liye.

Ek single source: proxies.txt (ya API endpoints) -> validated pool ->
round-robin/least-used rotation -> browser session us IP se launch.

Pool sources:
  1. Static file: data/proxies.txt (host:port ya user:pass@host:port,
     http/https/socks5 — scheme nahi ho to http maana)
  2. Remote API: apna proxy-vendor ka endpoint (JSON/text list) —
     PROXY_POOL_URL env ya add_api(). Har refresh pe fetch+merge.

Usage:
    from solver.proxies import ProxyPool
    pool = ProxyPool()
    pool.add("http://user:pass@1.2.3.4:8080")
    pool.add_file("data/proxies.txt")
    pool.add_api("https://vendor.example/api/proxies?key=...")

    p = pool.next()            # rotate — least-recently-used validated
    ok = pool.check(p)         # live check (ip echo + latency)
    stats = pool.stats()      # health report
"""
import json
import os
import time
import urllib.request

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
POOL_FILE = os.path.join(DATA_DIR, "proxxies.json")
IP_ECHO = "https://api.ipify.org?format=json"


def _norm(p):
    """user:pass@host:port / host:port -> scheme://..."""
    p = p.strip()
    if not p:
        return None
    if "://" not in p:
        p = "http://" + p
    return p.rstrip("/")


class _Entry:
    __slots__ = ("url", "fails", "wins", "last_used", "last_latency", "last_ip")

    def __init__(self, url):
        self.url = url
        self.fails = 0
        self.wins = 0
        self.last_used = 0.0
        self.last_latency = 0.0
        self.last_ip = ""

    def to_json(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_json(cls, d):
        e = cls(d["url"])
        for k in cls.__slots__[1:]:
            setattr(e, k, d.get(k, 0 if k in ("fails", "wins") else ("" if k == "last_ip" else 0.0)))
        return e


class ProxyPool:
    def __init__(self, pool_file=POOL_FILE):
        self.pool_file = pool_file
        self.entries = {}          # url -> _Entry
        self._order = []           # urls in insertion order
        self._rr = 0
        os.makedirs(os.path.dirname(pool_file), exist_ok=True)
        self._load()
        self._apis = []

    # ------------------------------------------------------------ load --
    def _load(self):
        try:
            data = json.load(open(self.pool_file))
            for d in data.get("entries", []):
                e = _Entry.from_json(d)
                self.entries[e.url] = e
                self._order.append(e.url)
            self._apis = data.get("apis", [])
        except Exception:
            pass

    def save(self):
        tmp = self.pool_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"entries": [self.entries[u].to_json() for u in self._order],
                       "apis": self._apis}, f, indent=1)
        os.replace(tmp, self.pool_file)

    # ------------------------------------------------------------ add --
    def add(self, proxy):
        p = _norm(proxy)
        if not p or p in self.entries:
            return False
        self.entries[p] = _Entry(p)
        self._order.append(p)
        return True

    def add_file(self, path):
        n = 0
        with open(path) as f:
            for line in f:
                if self.add(line):
                    n += 1
        self.save()
        return n

    def add_api(self, url):
        """Vendor API — refresh() pe fetch+parse karega."""
        if url not in self._apis:
            self._apis.append(url)
            self.save()
            return True
        return False

    def remove(self, proxy):
        p = _norm(proxy)
        if p in self.entries:
            del self.entries[p]
            self._order.remove(p)
            self.save()
            return True
        return False

    # ---------------------------------------------------------- rotate --
    def next(self, healthy_only=True, rotate=True):
        """Least-recently-used validated proxy. Fail hone pe mark_bad()."""
        cands = [u for u in self._order
                 if (not healthy_only) or self.entries[u].fails < 3]
        if not cands:
            return None
        # LRU: sabse pehle jo sabse purana use hua
        cands.sort(key=lambda u: self.entries[u].last_used)
        pick = cands[0]
        if pick:
            self.entries[pick].last_used = time.time()
            self.save()
        return pick

    def mark_bad(self, proxy, reason=""):
        p = _norm(proxy or "")
        if p in self.entries:
            self.entries[p].fails += 1
            self.save()

    def mark_good(self, proxy, latency=0.0, ip=""):
        p = _norm(proxy or "")
        if p in self.entries:
            e = self.entries[p]
            e.wins += 1
            e.fails = 0
            e.last_latency = latency
            e.last_ip = ip
            self.save()

    # ---------------------------------------------------------- check --
    def check(self, proxy, timeout=8):
        """Live check: proxy ke through IP echo. (ip, latency) ya (None, err)."""
        p = _norm(proxy)
        t0 = time.time()
        try:
            proxy_handler = urllib.request.ProxyHandler({
                "http": p, "https": p})
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(IP_ECHO,
                                         headers={"User-Agent": "curl/8.0"})
            with opener.open(req, timeout=timeout) as r:
                ip = json.loads(r.read().decode()).get("ip", "?")
            lat = round((time.time() - t0) * 1000)
            self.mark_good(p, lat, ip)
            return ip, lat
        except Exception as e:
            self.mark_bad(p)
            return None, str(e)[:60]

    def check_all(self, timeout=8):
        """Poore pool ka health-check. [(proxy, ip, latency_or_err)]"""
        out = []
        for u in list(self._order):
            ip, lat = self.check(u, timeout)
            out.append((u, ip, lat))
        return out

    # ---------------------------------------------------------- stats --
    def stats(self):
        healthy = sum(1 for e in self.entries.values() if e.fails < 3)
        return {
            "total": len(self._order),
            "healthy": healthy,
            "dead": len(self._order) - healthy,
            "apis": len(self._apis),
            "ips": sorted({e.last_ip for e in self.entries.values() if e.last_ip})[:20],
        }

    def list(self):
        return [
            {"url": u, "fails": self.entries[u].fails, "wins": self.entries[u].wins,
             "last_ip": self.entries[u].last_ip,
             "latency_ms": self.entries[u].last_latency}
            for u in self._order
        ]

    # ---------------------------------------------------------- refresh --
    def refresh(self):
        """Saare vendor APIs fetch karke naye proxies merge karo."""
        added = 0
        for api in list(self._apis):
            try:
                req = urllib.request.Request(api, headers={"User-Agent": "curl/8.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read().decode(errors="replace")
                added += self._parse_feed(body)
            except Exception:
                continue
        if added:
            self.save()
        return added

    def _parse_feed(self, body):
        """Vendor response (JSON list | text lines | {proxies:[...]}) parse."""
        n = 0
        try:
            data = json.loads(body)
            items = data.get("proxies", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, str):
                        n += 1 if self.add(it) else 0
                    elif isinstance(it, dict):
                        u = it.get("url") or it.get("proxy") or it.get("ip")
                        if u:
                            if "://" not in str(u) and it.get("port"):
                                u = f"{u}:{it['port']}"
                            n += 1 if self.add(str(u)) else 0
                return n
        except Exception:
            pass
        # plain text: ek proxy per line
        for line in body.splitlines():
            if self.add(line):
                n += 1
        return n


_default = None


def default_pool():
    global _default
    if _default is None:
        _default = ProxyPool()
    return _default
