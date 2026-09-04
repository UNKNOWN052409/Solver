//! GhostEngine net layer — session state ABOVE the transport (M2).
//!
//! Ye layer network I/O NAHI karti. Wo GhostMouse (rquest, Chrome TLS,
//! real socket) ka kaam hai. Ye layer uske UPAR baithti hai: cookies
//! (domain-scoped, persistent), redirect chains (max 5, cookie carry,
//! referer), auth headers (basic/bearer), proxy rotation (config struct +
//! URL rewrite — egress GhostMouse ke `--proxy` flag se hota hai), aur
//! `Session::get` jo sab jodkar final HTML deta hai.
//!
//! Zero-dep promise intact: std only. JSON bhi hand-rolled nahi — cookie
//! persistence simple line-format me hai (`name=value; domain=x; ...`),
//! jo insaan bhi padh sakta hai aur Python bhi parse kar sakta hai. Jar
//! ki default location: `~/.ghostbrowse/cookies.json` (naam JSON hai —
//! legacy extension — content text line-format hai).
//!
//! Tests: mock transport (trait) se redirect chain + cookie carry verify,
//! koi real network nahi, deterministic.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

// =========================================================== URL utilities

/// Host extract: `https://a.b.com:8443/x` -> `a.b.com` (port stripped).
pub fn url_host(url: &str) -> &str {
    let rest = match url.split_once("://") {
        Some((_, r)) => r,
        None => url,
    };
    let end = rest.find('/').unwrap_or(rest.len());
    rest[..end].split(':').next().unwrap_or("").trim()
}

/// Path extract: `https://a.com/x/y?z=1` -> `/x/y` (query stripped).
fn url_path(url: &str) -> &str {
    let rest = match url.split_once("://") {
        Some((_, r)) => r,
        None => url,
    };
    match rest.find('/') {
        Some(i) => rest[i..].split('?').next().unwrap_or("/"),
        None => "/",
    }
}

/// Host ka registrable "core" — `www.example.co.uk` -> `example.co.uk`.
/// Best-effort: public-suffix list NAHI hai — common multi-part TLDs
/// hand-listed hain, baaki last-2-label treat (single-label host as-is).
pub fn host_core(host: &str) -> String {
    let host = host.trim().trim_end_matches('.');
    let labels: Vec<&str> = host.split('.').collect();
    if labels.len() >= 3 {
        // multi-part-TLD check: LAST TWO labels ko join karke dekho —
        // `www.example.co.uk` -> last2 = `co.uk` (list match!) -> core = last3
        let last2 = labels[labels.len() - 2..].join(".").to_ascii_lowercase();
        if matches!(
            last2.as_str(),
            "co.uk"
                | "org.uk"
                | "ac.uk"
                | "gov.uk"
                | "co.jp"
                | "or.jp"
                | "ne.jp"
                | "com.au"
                | "net.au"
                | "org.au"
                | "co.nz"
                | "com.br"
                | "com.mx"
                | "co.in"
                | "co.za"
                | "com.sg"
                | "com.hk"
                | "com.tr"
        ) {
            return labels[labels.len() - 3..].join(".");
        }
        return labels[labels.len() - 2..].join(".");
    }
    host.to_string()
}

fn now_epoch() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Percent-encoding for query-string building (header-safe chars only).
pub fn pct_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

// ============================================================ base64 (std)

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Hand-rolled standard base64 encode (padding included). Decode engine
/// ko nahi chahiye — auth headers sirf encode karte hain.
pub fn b64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(B64[(n >> 18) as usize & 63] as char);
        out.push(B64[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 {
            B64[(n >> 6) as usize & 63] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            B64[n as usize & 63] as char
        } else {
            '='
        });
    }
    out
}

// ================================================================ cookies

/// Ek domain-scoped cookie. `domain` = jisne set kiya (ya Set-Cookie ka
/// Domain attr), `host_only` = sirf exact host ke liye (Domain attr nahi tha).
#[derive(Debug, Clone, PartialEq)]
pub struct Cookie {
    pub name: String,
    pub value: String,
    pub domain: String,       // lowercase, no leading dot
    pub path: String,         // default "/"
    pub host_only: bool,      // true => exact-host match only
    pub expires: Option<u64>, // epoch secs
}

impl Cookie {
    fn new(name: &str, value: &str, domain: &str) -> Cookie {
        Cookie {
            name: name.to_string(),
            value: value.to_string(),
            domain: domain.to_ascii_lowercase(),
            path: "/".to_string(),
            host_only: true,
            expires: None,
        }
    }

    /// Cookie header line: `name=value` (bina meta).
    pub fn header_line(&self) -> String {
        format!("{}={}", self.name, self.value)
    }

    /// Persistence line format (save/parse round-trip ke liye).
    fn to_line(&self) -> String {
        let mut s = format!("{}={}", self.name, self.value);
        s.push_str(&format!("; domain={}", self.domain));
        if self.path != "/" {
            s.push_str(&format!("; path={}", self.path));
        }
        if self.host_only {
            s.push_str("; hostonly");
        }
        if let Some(e) = self.expires {
            s.push_str(&format!("; expires={}", e));
        }
        s
    }
}

/// Domain-scoped cookie jar — HashMap<domain_core, Vec<Cookie>>.
/// Ek jar multiple domains sambhalta hai; `get_cookies(url)` sirf
/// matching domains ki cookies deta hai (cross-domain leak NAHI).
#[derive(Debug, Default, Clone)]
pub struct CookieJar {
    map: HashMap<String, Vec<Cookie>>,
}

impl CookieJar {
    pub fn new() -> CookieJar {
        CookieJar {
            map: HashMap::new(),
        }
    }

    /// Set-Cookie header parse — RFC 6265 lite. Attrs: Domain, Path,
    /// Expires (HTTP-date ya epoch), Max-Age (<=0 => delete). Default
    /// domain `set_url` ka host (host-only jab tak Domain attr na ho).
    /// Domain attr host ka parent na ho => reject (browsers jaisa).
    pub fn set_from_header(&mut self, set_cookie: &str, set_url: &str) -> Option<Cookie> {
        let host = url_host(set_url).to_ascii_lowercase();
        let mut parts = set_cookie.split(';');
        let nv = parts.next()?.trim();
        let (name, value) = nv.split_once('=')?;
        let name = name.trim();
        if name.is_empty() {
            return None;
        }
        let mut c = Cookie::new(name, value.trim(), &host);
        for p in parts {
            let p = p.trim();
            let (k, v) = match p.split_once('=') {
                Some((k, v)) => (k.trim().to_ascii_lowercase(), v.trim().to_string()),
                None => (p.to_ascii_lowercase(), String::new()),
            };
            match k.as_str() {
                "domain" => {
                    if !v.is_empty() {
                        c.domain = v.trim_start_matches('.').to_ascii_lowercase();
                        c.host_only = false;
                    }
                }
                "path" if !v.is_empty() => c.path = v,
                "expires" => {
                    if let Ok(e) = v.parse::<u64>() {
                        c.expires = Some(e);
                    } else if let Some(e) = parse_http_date_epoch(&v) {
                        c.expires = Some(e);
                    }
                }
                "max-age" => {
                    if let Ok(n) = v.parse::<i64>() {
                        if n <= 0 {
                            self.remove(&c.name, &host);
                            return None;
                        }
                        c.expires = Some(now_epoch() + n as u64);
                    }
                }
                _ => {}
            }
        }
        // Domain attr validation: host khud ya uska subdomain hona chahiye
        // (dot-boundary ke saath — `evilexample.com` se Domain=example.com reject)
        if !c.host_only && host != c.domain && !host.ends_with(&format!(".{}", c.domain)) {
            return None;
        }
        self.store(c.clone());
        Some(c)
    }

    fn remove(&mut self, name: &str, host: &str) {
        if let Some(v) = self.map.get_mut(&host_core(host)) {
            v.retain(|c| !(c.name == name && c.domain == host));
        }
    }

    fn store(&mut self, c: Cookie) {
        let core = host_core(&c.domain);
        let v = self.map.entry(core).or_default();
        // replace-by-(name, domain, path) — overwrite old value
        v.retain(|e| !(e.name == c.name && e.domain == c.domain && e.path == c.path));
        v.push(c);
    }

    /// URL ke liye applicable cookies — host-suffix match + path-prefix
    /// match + expiry check. Return: `a=1; b=2` Cookie header value.
    /// Ordering deterministic: lamba path pehle (RFC specificity), fir
    /// domain, fir name.
    pub fn get_cookies(&self, url: &str) -> String {
        let host = url_host(url).to_ascii_lowercase();
        let path = url_path(url);
        let now = now_epoch();
        let host_core_s = host_core(&host);
        let mut out: Vec<&Cookie> = Vec::new();
        for (core, cookies) in &self.map {
            // bucket pre-filter: URL host is bucket ke kisi cookie se match
            // ho sakta hai? (core equal ya host core ke equal)
            if host != *core && !host.ends_with(&format!(".{}", core)) && host_core_s != *core {
                continue;
            }
            for c in cookies {
                if let Some(e) = c.expires {
                    if e <= now {
                        continue;
                    }
                }
                let ok_host = if c.host_only {
                    host == c.domain
                } else {
                    host == c.domain || host.ends_with(&format!(".{}", c.domain))
                };
                if !ok_host {
                    continue;
                }
                if c.path == "/" || path.starts_with(&c.path) {
                    out.push(c);
                }
            }
        }
        out.sort_by(|a, b| {
            a.path
                .len()
                .cmp(&b.path.len())
                .then(a.domain.cmp(&b.domain))
                .then(a.name.cmp(&b.name))
        });
        out.iter()
            .map(|c| c.header_line())
            .collect::<Vec<_>>()
            .join("; ")
    }

    /// Jar snapshot (debug/inspection).
    pub fn all(&self) -> Vec<&Cookie> {
        self.map.values().flatten().collect()
    }

    pub fn len(&self) -> usize {
        self.map.values().map(|v| v.len()).sum()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    // -------------------------------------------------- persistence (text)

    /// Save jar to file. Format — ek cookie per line:
    /// `name=value; domain=<host>; path=</p>; hostonly; expires=<epoch>`
    /// Human-readable, diff-friendly, Python-parseable. Return: path.
    pub fn save(&self, path: &Path) -> std::io::Result<PathBuf> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let mut buf = String::from("# GhostEngine cookie jar v1\n");
        for c in self.map.values().flatten() {
            buf.push_str(&c.to_line());
            buf.push('\n');
        }
        std::fs::write(path, buf)?;
        Ok(path.to_path_buf())
    }

    /// Load jar from file (same format). Invalid lines skip.
    pub fn load(path: &Path) -> std::io::Result<CookieJar> {
        let raw = std::fs::read_to_string(path)?;
        let mut jar = CookieJar::new();
        for line in raw.lines() {
            if line.trim().is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some(c) = parse_cookie_line(line) {
                jar.store(c);
            }
        }
        Ok(jar)
    }

    /// Default path: `~/.ghostbrowse/cookies.json` (content text hai,
    /// extension legacy/compat — format dekho `save` ka doc).
    pub fn default_path() -> PathBuf {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        PathBuf::from(home)
            .join(".ghostbrowse")
            .join("cookies.json")
    }

    /// Convenience: default path se load (missing/corrupt => empty jar).
    pub fn load_default() -> CookieJar {
        CookieJar::load(&CookieJar::default_path()).unwrap_or_default()
    }

    /// Convenience: default path par save.
    pub fn save_default(&self) -> std::io::Result<PathBuf> {
        self.save(&CookieJar::default_path())
    }
}

/// Persistence line parse — `name=value; domain=x; path=y; hostonly; expires=N`
fn parse_cookie_line(line: &str) -> Option<Cookie> {
    let mut parts = line.split(';');
    let nv = parts.next()?.trim();
    let (name, value) = nv.split_once('=')?;
    let mut c = Cookie::new(name.trim(), value.trim(), "");
    for p in parts {
        let p = p.trim();
        let (k, v) = match p.split_once('=') {
            Some((k, v)) => (k.trim().to_ascii_lowercase(), v.trim().to_string()),
            None => (p.to_ascii_lowercase(), String::new()),
        };
        match k.as_str() {
            "domain" => {
                c.domain = v.to_ascii_lowercase();
                c.host_only = false;
            }
            "path" => c.path = v,
            "hostonly" => c.host_only = true,
            "expires" => c.expires = v.parse::<u64>().ok(),
            _ => {}
        }
    }
    if c.domain.is_empty() {
        return None;
    }
    Some(c)
}

/// HTTP-date -> epoch (RFC 1123: `%a, %d %b %Y %H:%M:%S GMT` only).
/// Best-effort: timezone ignore (servers GMT bhejte hain).
fn parse_http_date_epoch(s: &str) -> Option<u64> {
    let s = s.trim();
    let rest = s.split_once(", ")?.1; // "09 Jun 2021 10:18:14 GMT"
    // do split: date = pehle 3 tokens, time = 4th — split_once(' ') sirf
    // pehla space pakdeta tha (bug); split_whitespace + rejoin sahi
    let parts: Vec<&str> = rest.split_whitespace().collect();
    if parts.len() < 4 {
        return None;
    }
    let date = format!("{} {} {}", parts[0], parts[1], parts[2]);
    let time = parts[3];
    let (year, month, day) = parse_ymd(&date)?;
    let (h, m, sec) = parse_hms(time)?;
    Some(days_from_civil(year, month, day) * 86400 + h * 3600 + m * 60 + sec)
}

fn parse_ymd(s: &str) -> Option<(i64, u32, u32)> {
    // "09 Jun 2021"
    let mut it = s.split_whitespace();
    let day: u32 = it.next()?.parse().ok()?;
    let mon = it.next()?;
    let year: i64 = it.next()?.parse().ok()?;
    let m = match mon {
        "Jan" => 1,
        "Feb" => 2,
        "Mar" => 3,
        "Apr" => 4,
        "May" => 5,
        "Jun" => 6,
        "Jul" => 7,
        "Aug" => 8,
        "Sep" => 9,
        "Oct" => 10,
        "Nov" => 11,
        "Dec" => 12,
        _ => return None,
    };
    Some((year, m, day))
}

fn parse_hms(s: &str) -> Option<(u64, u64, u64)> {
    // "10:18:14 GMT"
    let hms = s.split_whitespace().next()?;
    let mut it = hms.split(':');
    let h: u64 = it.next()?.parse().ok()?;
    let m: u64 = it.next()?.parse().ok()?;
    let sec: u64 = it.next()?.parse().ok()?;
    Some((h, m, sec))
}

/// Days since epoch (Howard Hinnant's civil-date algorithm, public domain).
fn days_from_civil(y: i64, m: u32, d: u32) -> u64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64; // [0, 399]
    let mp = ((m + 9) % 12) as u64; // [0, 11]
    let doy = (153 * mp + 2) / 5 + (d as u64) - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    (era * 146097 + doe as i64 - 719468).max(0) as u64
}

// =========================================================== auth headers

/// Auth header builders — basic / bearer. Standard wire syntax, jaan-
/// bujhkar koi fancy nahi.
pub struct AuthHeader;

impl AuthHeader {
    /// `Authorization: Basic <base64(user:pass)>` value part.
    pub fn basic(user: &str, pass: &str) -> String {
        format!(
            "Basic {}",
            b64_encode(format!("{}:{}", user, pass).as_bytes())
        )
    }

    /// `Authorization: Bearer <token>` value part.
    pub fn bearer(token: &str) -> String {
        format!("Bearer {}", token)
    }

    /// Pura header pair: ("authorization", "Basic ...").
    pub fn basic_header(user: &str, pass: &str) -> (String, String) {
        ("authorization".to_string(), Self::basic(user, pass))
    }

    pub fn bearer_header(token: &str) -> (String, String) {
        ("authorization".to_string(), Self::bearer(token))
    }
}

// ============================================================= proxy route

/// Proxy-pool entry — GhostMouse CLI passthrough ke liye config.
/// Engine me sirf ye struct + arg-rewrite hai; actual egress GhostMouse
/// `--proxy http://host:port` se hota hai (engine socket NAHI kholta —
/// zero-dep promise).
#[derive(Debug, Clone, PartialEq)]
pub struct ProxyEndpoint {
    /// `http://user:pass@host:port` ya `socks5://host:port`
    pub url: String,
    pub label: String, // "resi-1", "auto-de-2", "opera-eu0" ...
}

/// Rotating proxy pool. Round-robin + banned-list (dead proxy skip).
/// Single-threaded flow ke liye (Session ke saath hi chalta hai).
#[derive(Debug, Clone, Default)]
pub struct ProxyRoute {
    endpoints: Vec<ProxyEndpoint>,
    next: usize,
    banned: Vec<String>,
}

impl ProxyRoute {
    pub fn new(endpoints: Vec<ProxyEndpoint>) -> ProxyRoute {
        ProxyRoute {
            endpoints,
            next: 0,
            banned: Vec::new(),
        }
    }

    /// Next live endpoint (round-robin, banned skip). None if pool khali
    /// ya sab banned.
    pub fn next_endpoint(&mut self) -> Option<&ProxyEndpoint> {
        if self.endpoints.is_empty() {
            return None;
        }
        let n = self.endpoints.len();
        for _ in 0..n {
            let e = &self.endpoints[self.next % n];
            self.next = (self.next + 1) % n;
            if !self.banned.contains(&e.url) {
                return Some(e);
            }
        }
        None
    }

    /// Proxy dead mark karo (timeout/5xx pattern par).
    pub fn ban(&mut self, url: &str) {
        if !self.banned.contains(&url.to_string()) {
            self.banned.push(url.to_string());
        }
    }

    /// GhostMouse CLI args build karo — proxy passthrough (curl-style
    /// flag): `["--proxy", "http://host:port"]`. Rotation inbuilt.
    pub fn cli_args(&mut self) -> Vec<String> {
        match self.next_endpoint() {
            Some(e) => vec!["--proxy".to_string(), e.url.clone()],
            None => Vec::new(),
        }
    }

    pub fn len(&self) -> usize {
        self.endpoints.len()
    }

    pub fn is_empty(&self) -> bool {
        self.endpoints.is_empty()
    }
}

// ================================================================ session

/// Transport response — GhostMouse `Page` ka engine-level mirror
/// (engine GhostMouse/rquest types import NAHI karta — decoupled).
#[derive(Debug, Clone)]
pub struct Resp {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: String,
    pub final_url: String,
}

impl Resp {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// Transport trait — GhostMouse adapter ya test mock implement karta hai.
/// Ek single request bhejta hai (redirect follow NAHI — Session chain
/// khud chalata hai taaki cookies/referer har hop par lag sakein).
pub trait Transport {
    fn fetch(&mut self, url: &str, headers: &[(String, String)]) -> Result<Resp, String>;
}

/// Mock transport — tests ke liye. Scripted responses (longest-prefix
/// match) + request log (kya headers gaye wo verify hota hai).
pub struct MockTransport {
    routes: Vec<(String, Resp)>,
    pub calls: Vec<(String, Vec<(String, String)>)>,
}

impl MockTransport {
    pub fn new() -> MockTransport {
        MockTransport {
            routes: Vec::new(),
            calls: Vec::new(),
        }
    }

    pub fn route(mut self, url_prefix: &str, resp: Resp) -> Self {
        self.routes.push((url_prefix.to_string(), resp));
        self
    }
}

impl Transport for MockTransport {
    fn fetch(&mut self, url: &str, headers: &[(String, String)]) -> Result<Resp, String> {
        self.calls.push((url.to_string(), headers.to_vec()));
        let hit = self
            .routes
            .iter()
            .filter(|(p, _)| url.starts_with(p.as_str()))
            .max_by_key(|(p, _)| p.len());
        match hit {
            Some((_, r)) => Ok(r.clone()),
            None => Err(format!("mock: no route for {}", url)),
        }
    }
}

/// Full session result — chain ke saath final HTML.
#[derive(Debug, Clone)]
pub struct SessionDoc {
    pub url: String, // final URL (redirects ke baad)
    pub status: u16,
    pub html: String,
    pub hops: Vec<String>,    // URL chain jo traverse hui
    pub cookies: Vec<Cookie>, // final jar snapshot
}

/// Redirect policy result — chain loop-shield ke liye.
enum Redirect {
    Follow(String),
    Stop,
}

const MAX_HOPS: usize = 5;

/// Browser-jaisa session: cookie jar + redirect chain + referer carry
/// + auth + proxy rotation, transport-agnostic.
pub struct Session {
    pub jar: CookieJar,
    pub auth: Option<(String, String)>, // (header name, value)
    pub user_agent: String,
    pub transport: Box<dyn Transport>,
    pub proxy: ProxyRoute,
    pub max_redirects: usize,
    pub persist: bool,
    /// Har fetch ka (url, sent-headers) record — debug/tests.
    pub log: Vec<(String, Vec<(String, String)>)>,
}

impl Session {
    pub fn new(transport: Box<dyn Transport>) -> Session {
        Session {
            jar: CookieJar::new(),
            auth: None,
            user_agent: "GhostEngine/0.1 (Solver agent-browser)".to_string(),
            transport,
            proxy: ProxyRoute::default(),
            max_redirects: MAX_HOPS,
            persist: false,
            log: Vec::new(),
        }
    }

    /// Persistence on: get() ke baad jar `~/.ghostbrowse/cookies.json` me.
    pub fn with_persistence(mut self) -> Session {
        self.persist = true;
        self
    }

    pub fn with_auth(mut self, auth: (String, String)) -> Session {
        self.auth = Some(auth);
        self
    }

    pub fn with_proxy_pool(mut self, pool: Vec<ProxyEndpoint>) -> Session {
        self.proxy = ProxyRoute::new(pool);
        self
    }

    /// Request headers assemble: UA + auth + cookies (domain-matched —
    /// cross-domain leak by-construction impossible) + referer.
    fn req_headers(&self, url: &str, referer: Option<&str>) -> Vec<(String, String)> {
        let mut h: Vec<(String, String)> = Vec::new();
        h.push(("user-agent".to_string(), self.user_agent.clone()));
        if let Some((k, v)) = &self.auth {
            h.push((k.clone(), v.clone()));
        }
        let ck = self.jar.get_cookies(url);
        if !ck.is_empty() {
            h.push(("cookie".to_string(), ck));
        }
        if let Some(r) = referer {
            h.push(("referer".to_string(), r.to_string()));
        }
        h
    }

    /// Redirect decide — Location header + status 301/302/303/307/308.
    /// Relative Location resolve via `human::resolve_url`.
    fn redirect_target(&self, r: &Resp) -> Redirect {
        if ![301u16, 302, 303, 307, 308].contains(&r.status) {
            return Redirect::Stop;
        }
        match r.header("location") {
            Some(l) if !l.is_empty() => {
                Redirect::Follow(crate::human::resolve_url(&r.final_url, l))
            }
            _ => Redirect::Stop,
        }
    }

    /// GET with full session semantics:
    /// 1. cookie jar apply (domain-scoped)
    /// 2. redirect chain — max 5 hops, har hop ka Set-Cookie jar me,
    ///    referer = previous hop, loop-detect
    /// 3. final HTML return
    /// Errors: transport failure / "too many redirects" / "redirect loop".
    pub fn get(&mut self, url: &str) -> Result<SessionDoc, String> {
        let mut hops: Vec<String> = vec![];
        let mut current = url.to_string();
        let mut referer: Option<String> = None;
        let mut last: Option<Resp> = None;

        for hop in 0..=self.max_redirects {
            // visited-check: current pehle-fetch-hua (hops me record) —
            // par seed URL ka pehla fetch exempt (loop guard sirf
            // redirect se wapas-aane pe)
            if hop > 0 && hops.contains(&current) {
                return Err(format!("redirect loop detected at {}", current));
            }
            hops.push(current.clone());
            let headers = self.req_headers(&current, referer.as_deref());
            let resp = self.transport.fetch(&current, &headers)?;
            self.log.push((current.clone(), headers));
            // Set-Cookie (multiple) apply — har hop ka
            for (k, v) in &resp.headers {
                if k.eq_ignore_ascii_case("set-cookie") {
                    self.jar.set_from_header(v, &current);
                }
            }
            referer = Some(current.clone());
            match self.redirect_target(&resp) {
                Redirect::Follow(next) => {
                    if hop == self.max_redirects {
                        return Err(format!(
                            "too many redirects (>{}) at {} -> {}",
                            self.max_redirects, current, next
                        ));
                    }
                    last = Some(resp);
                    current = next;
                }
                Redirect::Stop => {
                    last = Some(resp);
                    break;
                }
            }
        }
        let final_resp = last.ok_or_else(|| "no response".to_string())?;
        if self.persist {
            let _ = self.jar.save_default();
        }
        let cookies = self.jar.all().into_iter().cloned().collect();
        Ok(SessionDoc {
            url: final_resp.final_url.clone(),
            status: final_resp.status,
            html: final_resp.body.clone(),
            hops,
            cookies,
        })
    }
}

// ================================================================ tests
#[cfg(test)]
mod tests {
    use super::*;

    fn resp(status: u16, headers: Vec<(&str, &str)>, body: &str, url: &str) -> Resp {
        Resp {
            status,
            headers: headers
                .into_iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect(),
            body: body.to_string(),
            final_url: url.to_string(),
        }
    }

    fn hdr_of(log: &[(String, Vec<(String, String)>)], i: usize, name: &str) -> String {
        log.get(i)
            .and_then(|(_, h)| h.iter().find(|(k, _)| k == name))
            .map(|(_, v)| v.clone())
            .unwrap_or_default()
    }

    // 1) Set-Cookie parse: attrs, domain scope, host-only flag
    #[test]
    fn cookie_parse() {
        let mut jar = CookieJar::new();
        let c = jar
            .set_from_header(
                "sid=abc123; Path=/; Domain=.example.com; Max-Age=3600",
                "https://www.example.com/x",
            )
            .unwrap();
        assert_eq!(c.name, "sid");
        assert_eq!(c.value, "abc123");
        assert_eq!(c.domain, "example.com");
        assert!(!c.host_only);
        assert!(c.expires.is_some());
        // host-only (no Domain attr)
        let c2 = jar
            .set_from_header("theme=dark", "https://api.site.org/")
            .unwrap();
        assert!(c2.host_only);
        assert_eq!(c2.domain, "api.site.org");
        assert_eq!(c2.path, "/");
        // invalid Domain (not parent of setting host) => reject
        assert!(jar
            .set_from_header("x=1; Domain=example.com", "https://evilexample.com/")
            .is_none());
    }

    // 2) Domain match: subdomain ko parent ki domain-cookie milti hai,
    //    host-only sirf exact host ko, dusre site ko kuch nahi
    #[test]
    fn jar_domain_match() {
        let mut jar = CookieJar::new();
        jar.set_from_header("a=1; Domain=example.com", "https://example.com/")
            .unwrap();
        jar.set_from_header("b=2", "https://other.com/").unwrap();
        // subdomain gets domain-cookie
        assert_eq!(jar.get_cookies("https://shop.example.com/x"), "a=1");
        // host-only cookie sirf exact host ko
        let mut jar2 = CookieJar::new();
        jar2.set_from_header("h=9", "https://api.site.org/")
            .unwrap();
        assert_eq!(jar2.get_cookies("https://api.site.org/v1"), "h=9");
        assert_eq!(jar2.get_cookies("https://sub.api.site.org/v1"), "");
        // cross-domain leak nahi
        assert_eq!(jar.get_cookies("https://evil.com/"), "");
    }

    // 3) Redirect chain (mock): 2 hops, relative+absolute Location,
    //    Set-Cookie mid-chain, final 200
    #[test]
    fn redirect_chain_mock() {
        let t = MockTransport::new()
            .route(
                "https://a.com/",
                resp(
                    301,
                    vec![("location", "https://b.com/step")],
                    "",
                    "https://a.com/",
                ),
            )
            .route(
                "https://b.com/step",
                resp(
                    302,
                    vec![
                        ("location", "/final"),
                        ("set-cookie", "b2=zz; Domain=b.com"),
                    ],
                    "",
                    "https://b.com/step",
                ),
            )
            .route(
                "https://b.com/final",
                resp(200, vec![], "<h1>done</h1>", "https://b.com/final"),
            );
        let mut s = Session::new(Box::new(t));
        let doc = s.get("https://a.com/").unwrap();
        assert_eq!(doc.status, 200);
        assert!(doc.html.contains("done"));
        assert_eq!(
            doc.hops,
            vec![
                "https://a.com/",
                "https://b.com/step",
                "https://b.com/final"
            ]
        );
        // mid-chain cookie jar me gaya
        assert_eq!(s.jar.get_cookies("https://b.com/"), "b2=zz");
    }

    // 4) Persistence roundtrip: save -> load -> same cookie scope
    #[test]
    fn persistence_roundtrip() {
        let mut jar = CookieJar::new();
        jar.set_from_header(
            "sess=deadbeef; Domain=example.com; Path=/app",
            "https://example.com/app",
        )
        .unwrap();
        jar.set_from_header("theme=dark", "https://cdn.example.org/")
            .unwrap();
        let tmp = std::env::temp_dir().join("ghostnet_test_cookies.txt");
        jar.save(&tmp).unwrap();
        let jar2 = CookieJar::load(&tmp).unwrap();
        assert_eq!(
            jar2.get_cookies("https://example.com/app/x"),
            "sess=deadbeef"
        );
        assert_eq!(jar2.get_cookies("https://cdn.example.org/"), "theme=dark");
        // subdomain scope + PATH match — /app ke neeche hi bhejna
        // (RFC 6265: path-matching; www.example.com/ pe /app cookie NAHI jaata)
        assert_eq!(
            jar2.get_cookies("https://www.example.com/app/deep"),
            "sess=deadbeef"
        );
        assert_eq!(jar2.get_cookies("https://www.example.com/"), "");
        let _ = std::fs::remove_file(&tmp);
    }

    // 5) Session request headers: cookie carry + referer + auth sab gaye
    #[test]
    fn session_headers_and_carry() {
        let t = MockTransport::new()
            .route(
                "https://api.test/",
                resp(
                    302,
                    vec![
                        ("location", "https://api.test/next"),
                        ("set-cookie", "tok=t1; Domain=api.test"),
                    ],
                    "",
                    "https://api.test/",
                ),
            )
            .route(
                "https://api.test/next",
                resp(200, vec![], "OK", "https://api.test/next"),
            );
        let mut s = Session::new(Box::new(t)).with_auth(AuthHeader::basic_header("user", "pass"));
        let doc = s.get("https://api.test/").unwrap();
        assert_eq!(doc.status, 200);
        assert_eq!(s.log.len(), 2);
        // second hop: hop-1 ki cookie + referer + auth
        assert_eq!(hdr_of(&s.log, 1, "cookie"), "tok=t1");
        assert_eq!(hdr_of(&s.log, 1, "referer"), "https://api.test/");
        assert_eq!(hdr_of(&s.log, 1, "authorization"), "Basic dXNlcjpwYXNz");
        assert!(hdr_of(&s.log, 1, "user-agent").starts_with("GhostEngine"));

        // persistence: jar default path par save hua (HOME tmp override)
        let saved = std::env::var("HOME").ok();
        let tmp_home = std::env::temp_dir().join("ghostnet_home_rt");
        std::fs::create_dir_all(&tmp_home).unwrap();
        std::env::set_var("HOME", &tmp_home);
        let t2 = MockTransport::new().route(
            "https://api.test/",
            resp(
                200,
                vec![("set-cookie", "x=9; Domain=api.test")],
                "",
                "https://api.test/",
            ),
        );
        let mut s2 = Session::new(Box::new(t2)).with_persistence();
        let _ = s2.get("https://api.test/");
        let jar_disk = CookieJar::load_default();
        assert_eq!(jar_disk.get_cookies("https://api.test/next"), "x=9");
        match saved {
            Some(h) => std::env::set_var("HOME", h),
            None => std::env::remove_var("HOME"),
        }
        let _ = std::fs::remove_dir_all(&tmp_home);
    }

    // 6) Too many redirects — infinite chain, max 5 => error
    #[test]
    fn redirect_limit_enforced() {
        let mut t = MockTransport::new();
        for i in 0..8 {
            let next = format!("https://loop.com/{}", i + 1);
            t = t.route(
                &format!("https://loop.com/{}", i),
                resp(
                    302,
                    vec![("location", next.as_str())],
                    "",
                    &format!("https://loop.com/{}", i),
                ),
            );
        }
        t = t.route(
            "https://loop.com/8",
            resp(200, vec![], "never", "https://loop.com/8"),
        );
        let mut s = Session::new(Box::new(t));
        let err = s.get("https://loop.com/0").unwrap_err();
        assert!(err.contains("too many redirects"), "got: {}", err);
    }

    // 7) Redirect loop (A -> B -> A) detect
    #[test]
    fn redirect_loop_detected() {
        let t = MockTransport::new()
            .route(
                "https://x.com/",
                resp(
                    302,
                    vec![("location", "https://y.com/")],
                    "",
                    "https://x.com/",
                ),
            )
            .route(
                "https://y.com/",
                resp(
                    302,
                    vec![("location", "https://x.com/")],
                    "",
                    "https://y.com/",
                ),
            );
        let mut s = Session::new(Box::new(t));
        let err = s.get("https://x.com/").unwrap_err();
        assert!(err.contains("redirect loop"), "got: {}", err);
    }

    // 8) AuthHeader helpers — exact wire values
    #[test]
    fn auth_header_values() {
        assert_eq!(AuthHeader::basic("user", "pass"), "Basic dXNlcjpwYXNz");
        assert_eq!(
            AuthHeader::basic("alice", "s3cret"),
            "Basic YWxpY2U6czNjcmV0"
        );
        assert_eq!(AuthHeader::bearer("tok123"), "Bearer tok123");
        let (k, v) = AuthHeader::bearer_header("tok123");
        assert_eq!((k.as_str(), v.as_str()), ("authorization", "Bearer tok123"));
    }

    // 9) ProxyRoute rotation + ban + CLI args
    #[test]
    fn proxy_rotation() {
        let mut r = ProxyRoute::new(vec![
            ProxyEndpoint {
                url: "http://p1:8080".into(),
                label: "resi-1".into(),
            },
            ProxyEndpoint {
                url: "http://p2:8080".into(),
                label: "resi-2".into(),
            },
            ProxyEndpoint {
                url: "http://p3:8080".into(),
                label: "auto-1".into(),
            },
        ]);
        assert_eq!(r.cli_args(), vec!["--proxy", "http://p1:8080"]);
        assert_eq!(r.cli_args(), vec!["--proxy", "http://p2:8080"]);
        r.ban("http://p2:8080");
        assert_eq!(r.cli_args(), vec!["--proxy", "http://p3:8080"]);
        // banned skip on wrap-around
        assert_eq!(r.cli_args(), vec!["--proxy", "http://p1:8080"]);
        assert_eq!(r.cli_args(), vec!["--proxy", "http://p3:8080"]);
        r.ban("http://p1:8080");
        r.ban("http://p3:8080");
        assert!(r.cli_args().is_empty());
    }

    // 10) b64 + URL utils
    #[test]
    fn b64_and_url_utils() {
        assert_eq!(b64_encode(b"user:pass"), "dXNlcjpwYXNz");
        assert_eq!(b64_encode(b"alice:s3cret"), "YWxpY2U6czNjcmV0");
        assert_eq!(b64_encode(b"a"), "YQ==");
        assert_eq!(b64_encode(b"ab"), "YWI=");
        assert_eq!(b64_encode(b"abc"), "YWJj");
        assert_eq!(url_host("https://a.b.com:8443/x"), "a.b.com");
        assert_eq!(url_host("http://plain.org"), "plain.org");
        assert_eq!(host_core("www.example.co.uk"), "example.co.uk");
        assert_eq!(host_core("news.site.com"), "site.com");
        assert_eq!(host_core("localhost"), "localhost");
    }

    // 11) HTTP-date parse — known epochs
    #[test]
    fn http_date_epoch() {
        assert_eq!(
            parse_http_date_epoch("Wed, 09 Jun 2021 10:18:14 GMT"),
            Some(1623233894)
        );
        assert_eq!(
            parse_http_date_epoch("Thu, 01 Jan 1970 00:00:00 GMT"),
            Some(0)
        );
        assert_eq!(parse_http_date_epoch("garbage"), None);
    }
}
