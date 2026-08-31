//! GhostMouse v0.4 — stealth agentic web client for AI agents.
//!
//! One binary, pure Rust, no browser. Chrome-shaped TLS (BoringSSL via
//! rquest + rquest-util presets), per-user persistent identity, human-
//! timed behavior, tracker blocklist, X syndication reader, agentic page
//! reads (links/forms/text/tables/meta/images), form fill + submit,
//! multi-engine search, captcha plumbing to the Solver API (detect /
//! sitekeys / solve / clearance vault), proxy-aware routing, and a
//! JSON-over-HTTP server mode so any agent (or MCP bridge) can drive it.
//!
//! Layout of this single file:
//!   mod identity — per-user fingerprint (Chrome persona + OS + locale)
//!   mod behavior — human think-time + typing cadence
//!   mod blocklist — tracker prevention (never even dial the domain)
//!   mod vault — ~/.solver_clearance replay (cf_clearance etc.)
//!   mod agent — stealth client + agentic page actions + form fill
//!   mod search — multi-engine search (SearXNG, DDG html, Bing)
//!   mod captcha — detect + sitekeys + hand-off to the Solver API
//!   mod battery — live demo-site battery (detect + solve every wall)
//!   mod server — JSON-over-HTTP drive API (MCP-style, agent-first)
//!   mod cli — subcommands
//!
//! Design rules (LO's):
//!   - live-only: every capability was tested against real sites
//!   - one-file: the whole browser stack lives here, no fragments
//!   - light: no DOM engine, no JS engine — agents don't need rendering,
//!     they need clean text, links, forms, and a human-shaped wire profile

// ===========================================================================
// IDENTITY — one user = one stable machine persona
// ===========================================================================

mod identity {
    use rquest_util::{Emulation, EmulationOS};

    /// Chrome persona: TLS preset version + OS + locale hint.
    /// TLS (JA3/JA4/HTTP2 SETTINGS) + default headers (UA, sec-ch-ua)
    /// all come from the same rquest-util preset, so the wire never
    /// disagrees with the handshake.
    #[derive(Clone, Debug)]
    pub struct Persona {
        pub name: &'static str,
        pub emulation: Emulation,
        pub os: EmulationOS,
        pub locale: &'static str,
        pub ua: &'static str,
    }

    /// Persona pool. Chrome 134-136 across Windows/macOS/Linux — recent
    /// and common on the real web. Small pool on purpose: herd privacy,
    /// rarity is a signal.
    pub const PERSONAS: &[Persona] = &[
        Persona {
            name: "win-chrome-136",
            emulation: Emulation::Chrome136,
            os: EmulationOS::Windows,
            locale: "en-US",
            ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        },
        Persona {
            name: "win-chrome-135",
            emulation: Emulation::Chrome135,
            os: EmulationOS::Windows,
            locale: "en-GB",
            ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        },
        Persona {
            name: "win-chrome-134",
            emulation: Emulation::Chrome134,
            os: EmulationOS::Windows,
            locale: "en-US",
            ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        },
        Persona {
            name: "mac-chrome-136",
            emulation: Emulation::Chrome136,
            os: EmulationOS::MacOS,
            locale: "en-US",
            ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        },
        Persona {
            name: "mac-chrome-135",
            emulation: Emulation::Chrome135,
            os: EmulationOS::MacOS,
            locale: "en-GB",
            ua: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        },
        Persona {
            name: "lin-chrome-134",
            emulation: Emulation::Chrome134,
            os: EmulationOS::Linux,
            locale: "en-US",
            ua: "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
        },
    ];

    /// Stable per-user hash. Same user name -> same persona + rhythm forever;
    /// different users -> different machines. FNV-1a is enough for seeding.
    pub fn user_seed(user: &str) -> u64 {
        let mut h: u64 = 0xcbf2_9ce4_8422_2325;
        for b in user.as_bytes() {
            h ^= *b as u64;
            h = h.wrapping_mul(0x0000_0100_0000_01b3);
        }
        h
    }

    pub fn persona_for(user: &str) -> &'static Persona {
        &PERSONAS[(user_seed(user) % PERSONAS.len() as u64) as usize]
    }

    /// Persona by CLI override name (identity stays stable once picked).
    pub fn persona_by_name(name: &str) -> Option<&'static Persona> {
        PERSONAS.iter().find(|p| p.name == name)
    }
}

// ===========================================================================
// BEHAVIOR — human-shaped timing, not a metronome
// ===========================================================================

mod behavior {
    use rand::Rng;

    /// Humans read between requests. Fast when scrolling, slow when thinking.
    pub fn think_pause(last_gap_secs: f64, rng: &mut impl Rng) -> std::time::Duration {
        if last_gap_secs < 0.35 {
            std::time::Duration::from_millis(rng.gen_range(350..=1900))
        } else if rng.gen_bool(0.12) {
            std::time::Duration::from_millis(rng.gen_range(500..=2500))
        } else {
            std::time::Duration::ZERO
        }
    }

    /// Per-key typing cadence in ms. Agents feed this to any UI layer so
    /// keystroke timing matches this identity's rhythm.
    pub fn key_delay_ms(rng: &mut impl Rng) -> u64 {
        let mut d = rng.gen_range(45..=160);
        if rng.gen_bool(0.05) {
            d += rng.gen_range(200..=600); // thinking hiccup
        }
        d
    }

    /// Human scroll: eased chunks, not one jump. Returns (offset, pause_ms).
    pub fn scroll_step(target: u32, current: u32, rng: &mut impl Rng) -> (u32, u64) {
        if current >= target {
            return (target, 0);
        }
        let remaining = target - current;
        let divisor = rng.gen_range(2..=4);
        let leap = remaining / divisor + rng.gen_range(30..=120);
        let next = current + leap.min(remaining);
        (next, rng.gen_range(180..=520))
    }
}

// ===========================================================================
// BLOCKLIST — tracker prevention (never dial the domain at all)
// ===========================================================================

mod blocklist {
    pub const AD_DOMAINS: &[&str] = &[
        // ad networks
        "doubleclick.net", "googlesyndication.com", "googleadservices.com",
        "adnxs.com", "adsrvr.org", "adform.net", "adroll.com", "criteo.com",
        "pubmatic.com", "rubiconproject.com", "taboola.com", "outbrain.com",
        "media.net", "smartadserver.com", "casalemedia.com", "openx.net",
        // trackers / analytics
        "google-analytics.com", "googletagmanager.com", "scorecardresearch.com",
        "quantserve.com", "hotjar.com", "mouseflow.com", "fullstory.com",
        "mixpanel.com", "segment.io", "amplitude.com", "heapanalytics.com",
        // social pixels
        "ct.pinterest.com", "ads-twitter.com", "px.ads.linkedin.com",
        "bat.bing.com", "clarity.ms",
        // pop/redirect junk
        "popads.net", "propellerads.com", "adcash.com", "adsterra.com",
        "exoclick.com", "mgid.com", "revcontent.com",
    ];

    /// True if this URL should be refused locally (never sent).
    pub fn is_ad(url: &str) -> bool {
        let host = url
            .split_once("://")
            .map(|(_, r)| r)
            .unwrap_or(url)
            .split('/')
            .next()
            .unwrap_or("")
            .split(':')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase();
        AD_DOMAINS.iter().any(|d| host == *d || host.ends_with(&format!(".{d}")))
    }
}

// ===========================================================================
// VAULT — replay Solver's clearance cookies (~/.solver_clearance)
// ===========================================================================

mod vault {
    use std::path::PathBuf;

    /// Vault path for a URL's domain — mirrors recon/clearance_session.py
    /// exactly so Python-minted clearances work here and vice versa.
    pub fn vault_path(url: &str) -> PathBuf {
        let host = host_of(url);
        PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| ".".into()))
            .join(".solver_clearance")
            .join(format!("{host}.json"))
    }

    fn host_of(url: &str) -> String {
        url.split_once("://")
            .map(|(_, r)| r)
            .unwrap_or(url)
            .split('/')
            .next()
            .unwrap_or("unknown")
            .split(':')
            .next()
            .unwrap_or("unknown")
            .to_string()
    }

    /// Load vaulted cookies for this domain, if any.
    pub fn cookies(url: &str) -> Vec<(String, String)> {
        let p = vault_path(url);
        let Ok(raw) = std::fs::read_to_string(&p) else {
            return Vec::new();
        };
        let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
            return Vec::new();
        };
        v.get("cookies")
            .and_then(|c| c.as_object())
            .map(|obj| {
                obj.iter()
                    .filter_map(|(k, val)| val.as_str().map(|s| (k.clone(), s.to_string())))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Save a cookie set into the vault (Python-compatible schema).
    pub fn save(url: &str, ua: &str, cookies: &[(String, String)]) -> Option<PathBuf> {
        let p = vault_path(url);
        if let Some(dir) = p.parent() {
            std::fs::create_dir_all(dir).ok()?;
        }
        let map: serde_json::Map<String, serde_json::Value> = cookies
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::Value::String(v.clone())))
            .collect();
        let entry = serde_json::json!({
            "url": url,
            "user_agent": ua,
            "cookies": map,
            "minted_at": std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0),
            "source_ip": "?",
        });
        std::fs::write(&p, serde_json::to_string_pretty(&entry).ok()?).ok()?;
        Some(p)
    }
}

// ===========================================================================
// SEARCH — multi-engine results for agents (anti-captcha-first ordering)
// ===========================================================================

mod search {
    use serde_json::Value;

    use crate::agent::Agent;

    /// One search hit.
    #[derive(Clone, Debug, serde::Serialize)]
    pub struct Hit {
        pub title: String,
        pub url: String,
        pub snippet: String,
    }

    /// Query engines in priority order: SearXNG (local, zero captcha risk)
    /// first, then DuckDuckGo HTML endpoint (rarely challenged, no JS),
    /// then Bing HTML (heavier, different index), then Mojeek (own index,
    /// never challenges). First engine returning results wins; a
    /// captcha-walled engine degrades to the next.
    pub async fn run(
        agent: &mut Agent,
        query: &str,
        limit: usize,
        engine: Option<&str>,
    ) -> Result<Vec<Hit>, String> {
        let q = url::form_urlencoded::byte_serialize(query.as_bytes()).collect::<String>();
        let mut out: Vec<Hit> = Vec::new();

        let order: Vec<&str> = match engine {
            Some("searxng") => vec!["searxng"],
            Some("duckduckgo") | Some("ddg") => vec!["duckduckgo"],
            Some("bing") => vec!["bing"],
            Some("mojeek") => vec!["mojeek"],
            _ => vec!["searxng", "duckduckgo", "bing", "mojeek"],
        };

        for name in order {
            // engine failure (down / blocked / timeout) degrades to the next
            // engine instead of aborting the whole search — the `?` here
            // would kill the fallback chain.
            let page = match name {
                "searxng" => {
                    let url = format!("http://127.0.0.1:8888/search?q={q}&format=json");
                    match agent.get(&url).await {
                        Ok(p) => p,
                        Err(_) => continue,
                    }
                }
                "duckduckgo" => {
                    let url = format!("https://html.duckduckgo.com/html/?q={q}");
                    match agent.get(&url).await {
                        Ok(p) => p,
                        Err(_) => continue,
                    }
                }
                "bing" => {
                    let url = format!("https://www.bing.com/search?q={q}");
                    match agent.get(&url).await {
                        Ok(p) => p,
                        Err(_) => continue,
                    }
                }
                "mojeek" => {
                    let url = format!("https://www.mojeek.com/search?q={q}");
                    match agent.get(&url).await {
                        Ok(p) => p,
                        Err(_) => continue,
                    }
                }
                _ => continue,
            };
            if page.status != 200 {
                continue;
            }
            match name {
                "searxng" => searxng_parse(&page.body, limit, &mut out),
                "duckduckgo" => ddg_parse(&page.body, limit, &mut out),
                "bing" => bing_parse(&page.body, limit, &mut out),
                "mojeek" => mojeek_parse(&page.body, limit, &mut out),
                _ => {}
            }
            if !out.is_empty() {
                break; // first engine that returns results wins
            }
        }
        Ok(out)
    }

    fn searxng_parse(body: &str, limit: usize, out: &mut Vec<Hit>) {
        let Ok(v) = serde_json::from_str::<Value>(body) else { return };
        let Some(results) = v.get("results").and_then(|r| r.as_array()) else { return };
        for r in results.iter().take(limit) {
            out.push(Hit {
                title: r.get("title").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                url: r.get("url").and_then(|x| x.as_str()).unwrap_or("").to_string(),
                snippet: r.get("content").and_then(|x| x.as_str()).unwrap_or("").to_string(),
            });
        }
    }

    fn ddg_parse(body: &str, limit: usize, out: &mut Vec<Hit>) {
        // DDG html: results are <div class="result ..."> blocks holding
        // <a class="result__a" href="...">title</a> and
        // <a class="result__snippet" ...>text</a>
        let doc = scraper::Html::parse_document(body);
        let rsel = scraper::Selector::parse("div.result").unwrap();
        let asel = scraper::Selector::parse("a.result__a").unwrap();
        let ssel = scraper::Selector::parse("a.result__snippet").unwrap();
        for r in doc.select(&rsel) {
            if out.len() >= limit {
                break;
            }
            let Some(a) = r.select(&asel).next() else { continue };
            let href = a.value().attr("href").unwrap_or("").to_string();
            let title = a.text().collect::<String>().trim().to_string();
            let snippet = r
                .select(&ssel)
                .next()
                .map(|s| s.text().collect::<String>().trim().to_string())
                .unwrap_or_default();
            out.push(Hit { title, url: ddg_clean(&href), snippet });
        }
    }

    fn ddg_clean(href: &str) -> String {
        // DDG wraps: //duckduckgo.com/l/?uddg=<encoded>&rut=...
        if let Some((_, rest)) = href.split_once("uddg=") {
            let enc = rest.split('&').next().unwrap_or("");
            let decoded: String = percent_decode(enc);
            if !decoded.is_empty() {
                return decoded;
            }
        }
        href.to_string()
    }

    fn percent_decode(s: &str) -> String {
        let bytes: Vec<u8> = s.as_bytes().to_vec();
        let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
        let mut i = 0;
        while i < bytes.len() {
            if bytes[i] == b'%' && i + 2 < bytes.len() {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                if let Ok(b) = u8::from_str_radix(hex, 16) {
                    out.push(b);
                    i += 3;
                    continue;
                }
            }
            out.push(bytes[i]);
            i += 1;
        }
        String::from_utf8_lossy(&out).to_string()
    }

    fn bing_parse(body: &str, limit: usize, out: &mut Vec<Hit>) {
        let doc = scraper::Html::parse_document(body);
        let sel = scraper::Selector::parse("li.b_algo h2 a").unwrap();
        for r in doc.select(&sel) {
            if out.len() >= limit {
                break;
            }
            out.push(Hit {
                title: r.text().collect::<String>().trim().to_string(),
                url: r.value().attr("href").unwrap_or("").to_string(),
                snippet: String::new(),
            });
        }
    }

    fn mojeek_parse(body: &str, limit: usize, out: &mut Vec<Hit>) {
        // mojeek: results are <li> blocks holding <a class="title" href>
        // and <p class="s"> — independent index, no challenges observed.
        let doc = scraper::Html::parse_document(body);
        let lisel = scraper::Selector::parse("li").unwrap();
        let asel = scraper::Selector::parse("a.title").unwrap();
        let ssel = scraper::Selector::parse("p.s").unwrap();
        for l in doc.select(&lisel) {
            if out.len() >= limit {
                break;
            }
            let Some(a) = l.select(&asel).next() else { continue };
            let href = a.value().attr("href").unwrap_or("").to_string();
            if !href.starts_with("http") {
                continue;
            }
            let title = a.text().collect::<String>().trim().to_string();
            if title.is_empty() {
                continue;
            }
            let snippet = l
                .select(&ssel)
                .next()
                .map(|s| s.text().collect::<String>().trim().to_string())
                .unwrap_or_default();
            out.push(Hit { title, url: href, snippet });
        }
    }
}


mod agent {
    use std::time::{Duration, Instant};

    use rquest::{Client, Method, Proxy};
    use scraper::{Html, Selector};
    use serde_json::Value;
    use rand::SeedableRng;

    use crate::identity::{persona_by_name, persona_for, Persona};
    use crate::{behavior, blocklist, vault};

    pub struct Agent {
        pub user: String,
        client: Client,
        pub persona: &'static Persona,
        last_request: Option<Instant>,
        rng: rand::rngs::StdRng,
        pub total_requests: u64,
        pub blocked_trackers: u64,
        no_blocklist: bool,
    }

    pub struct Page {
        pub url: String,
        pub status: u16,
        pub body: String,
        pub headers: Vec<(String, String)>,
    }

    /// Identity + connection options passed by the CLI/server layer.
    #[derive(Clone, Debug, Default)]
    pub struct AgentOpts {
        pub proxy: Option<String>,
        pub timeout_secs: u64,
        pub persona: Option<String>,
        pub no_blocklist: bool,
    }

    impl Agent {
        pub fn new(user: &str, opts: &AgentOpts) -> Result<Self, String> {
            let persona = opts
                .persona
                .as_deref()
                .and_then(persona_by_name)
                .unwrap_or_else(|| persona_for(user));

            // The preset wires TLS (JA3/JA4), HTTP/2 SETTINGS, default
            // headers (UA, sec-ch-ua) AND their wire order. Per-request
            // navigation headers ride on top of that. Redirects follow
            // like a real browser (301/302 landing pages are the norm).
            let mut builder = Client::builder()
                .emulation(persona.emulation)
                .redirect(rquest::redirect::Policy::limited(10))
                .timeout(Duration::from_secs(if opts.timeout_secs == 0 { 30 } else { opts.timeout_secs }));

            if let Some(px) = opts.proxy.as_deref() {
                builder = builder.proxy(parse_proxy(px)?);
            }

            let client = builder.build().map_err(|e| e.to_string())?;
            Ok(Self {
                user: user.to_string(),
                client,
                persona,
                last_request: None,
                rng: rand::rngs::StdRng::seed_from_u64(crate::identity::user_seed(user)),
                total_requests: 0,
                blocked_trackers: 0,
                no_blocklist: opts.no_blocklist,
            })
        }

        fn think(&mut self) {
            let gap = self
                .last_request
                .map(|t| t.elapsed().as_secs_f64())
                .unwrap_or(999.0);
            let pause = behavior::think_pause(gap, &mut self.rng);
            if !pause.is_zero() {
                std::thread::sleep(pause);
            }
            self.last_request = Some(Instant::now());
        }

        /// Chrome's navigation header set. site/dest vary by request kind;
        /// UA and sec-ch-ua come from the preset's default headers.
        fn nav_headers(
            &self,
            referer: Option<&str>,
            accept: &str,
            dest: &str,
            site: &str,
        ) -> Vec<(&'static str, String)> {
            let mut h: Vec<(&'static str, String)> = Vec::new();
            if dest == "document" {
                h.push(("upgrade-insecure-requests", "1".into()));
            }
            h.push(("accept", accept.to_string()));
            h.push(("sec-fetch-site", site.to_string()));
            h.push(("sec-fetch-mode", "navigate".into()));
            if dest == "document" {
                h.push(("sec-fetch-user", "?1".into()));
            }
            h.push(("sec-fetch-dest", dest.to_string()));
            h.push(("accept-encoding", "gzip, deflate, br, zstd".into()));
            h.push(("accept-language", format!("{},en;q=0.9", self.persona.locale)));
            if let Some(r) = referer {
                h.push(("referer", r.to_string()));
            }
            h
        }

        /// Stealth navigation GET. Tracker URLs are refused locally.
        pub async fn get(&mut self, url: &str) -> Result<Page, String> {
            self.fetch(Method::GET, url, None, None, None).await
        }

        /// Header-less GET for endpoints that 429 on navigation-header
        /// shapes (syndication accepts bare requests; extra sec-fetch /
        /// accept-encoding marks the client as automation).
        pub async fn get_bare(&mut self, url: &str) -> Result<Page, String> {
            if !self.no_blocklist && blocklist::is_ad(url) {
                self.blocked_trackers += 1;
                return Ok(Page {
                    url: url.into(),
                    status: 0,
                    body: String::new(),
                    headers: Vec::new(),
                });
            }
            self.think();
            let resp = self
                .client
                .request(Method::GET, url)
                .send()
                .await
                .map_err(|e| e.to_string())?;
            self.total_requests += 1;
            let status = resp.status().as_u16();
            let headers: Vec<(String, String)> = resp
                .headers()
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
                .collect();
            let body = resp.text().await.map_err(|e| e.to_string())?;
            Ok(Page { url: url.into(), status, body, headers })
        }

        /// Stealth navigation GET with a referer (same-site click).
        pub async fn get_from(&mut self, url: &str, referer: &str) -> Result<Page, String> {
            self.fetch(Method::GET, url, None, None, Some(referer)).await
        }

        /// Stealth POST (form submit, API call).
        pub async fn post(
            &mut self,
            url: &str,
            content_type: Option<&str>,
            body: Vec<u8>,
            referer: Option<&str>,
        ) -> Result<Page, String> {
            self.fetch(Method::POST, url, Some(body), content_type, referer).await
        }

        /// Core fetch — all navigation traffic funnels through here so
        /// human-timing, blocklist and clearance-replay rules apply uniformly.
        pub async fn fetch(
            &mut self,
            method: Method,
            url: &str,
            body: Option<Vec<u8>>,
            content_type: Option<&str>,
            referer: Option<&str>,
        ) -> Result<Page, String> {
            if !self.no_blocklist && blocklist::is_ad(url) {
                self.blocked_trackers += 1;
                return Ok(Page {
                    url: url.into(),
                    status: 0,
                    body: String::new(),
                    headers: Vec::new(),
                });
            }
            self.think();

            let is_doc = content_type.map(|c| c.contains("html")).unwrap_or(true);
            let (accept, dest, site) = if is_doc {
                (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "document",
                    if referer.is_some() { "same-origin" } else { "none" },
                )
            } else {
                ("application/json, text/plain, */*", "empty", "same-origin")
            };

            let mut req = self.client.request(method, url);
            for (k, v) in self.nav_headers(referer, accept, dest, site) {
                req = req.header(k, v);
            }
            if let Some(ct) = content_type {
                req = req.header("content-type", ct);
            }
            if let Some(b) = body {
                req = req.body(b);
            }

            // clearance replay: vaulted cookies ride as one Cookie header
            let vaulted = vault::cookies(url);
            if !vaulted.is_empty() {
                let joined = vaulted
                    .iter()
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect::<Vec<_>>()
                    .join("; ");
                req = req.header("cookie", joined);
            }

            let resp = req.send().await.map_err(|e| e.to_string())?;
            self.total_requests += 1;

            let status = resp.status().as_u16();
            let headers: Vec<(String, String)> = resp
                .headers()
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
                .collect();
            let body = resp.text().await.map_err(|e| e.to_string())?;
            Ok(Page { url: url.into(), status, body, headers })
        }

        // ------------------------------------------------------- agentic reads

        /// Extract all links (text + absolute href).
        pub fn links(page: &Page) -> Vec<Value> {
            let doc = Html::parse_document(&page.body);
            let sel = Selector::parse("a[href]").unwrap();
            let base = url::Url::parse(&page.url).ok();
            doc.select(&sel)
                .filter_map(|el| {
                    let href = el.value().attr("href")?.to_string();
                    let abs = base
                        .as_ref()
                        .and_then(|b| b.join(&href).ok())
                        .map(|u| u.to_string())
                        .unwrap_or_else(|| href.clone());
                    let text = el.text().collect::<String>().trim().to_string();
                    Some(serde_json::json!({ "text": text, "href": abs }))
                })
                .collect()
        }

        /// Extract forms (absolute action, method, fields with types).
        pub fn forms(page: &Page) -> Vec<Value> {
            let doc = Html::parse_document(&page.body);
            let fsel = Selector::parse("form").unwrap();
            let isel = Selector::parse("input,select,textarea,button").unwrap();
            let base = url::Url::parse(&page.url).ok();
            doc.select(&fsel)
                .map(|f| {
                    let fields: Vec<Value> = f
                        .select(&isel)
                        .filter_map(|i| {
                            let tag = i.value().name().to_string();
                            let name = i.value().attr("name").map(|s| s.to_string());
                            let ftype = i.value().attr("type").unwrap_or("").to_string();
                            let value = i.value().attr("value").unwrap_or("").to_string();
                            if name.is_none() && tag != "button" {
                                return None;
                            }
                            Some(serde_json::json!({
                                "tag": tag,
                                "name": name,
                                "type": ftype,
                                "value": value,
                            }))
                        })
                        .collect();
                    let action = f.value().attr("action").unwrap_or("");
                    let abs = base
                        .as_ref()
                        .and_then(|b| b.join(action).ok())
                        .map(|u| u.to_string())
                        .unwrap_or_else(|| page.url.clone());
                    serde_json::json!({
                        "action": abs,
                        "method": f.value().attr("method").unwrap_or("GET").to_uppercase(),
                        "fields": fields,
                    })
                })
                .collect()
        }

        /// Readable text: strip script/style, collapse whitespace.
        pub fn text(page: &Page) -> String {
            let doc = Html::parse_document(&page.body);
            let bsel = Selector::parse("body").unwrap();
            let mut out = String::new();
            for b in doc.select(&bsel) {
                for n in b.descendants() {
                    if let Some(t) = n.value().as_text() {
                        out.push_str(t);
                        out.push(' ');
                    }
                }
            }
            out.split_whitespace().collect::<Vec<_>>().join(" ")
        }

        /// Extract tables as rows of cells.
        pub fn tables(page: &Page) -> Vec<Vec<Vec<String>>> {
            let doc = Html::parse_document(&page.body);
            let tsel = Selector::parse("table").unwrap();
            let rsel = Selector::parse("tr").unwrap();
            let csel = Selector::parse("th,td").unwrap();
            doc.select(&tsel)
                .map(|t| {
                    t.select(&rsel)
                        .map(|r| {
                            r.select(&csel)
                                .map(|c| c.text().collect::<String>().trim().to_string())
                                .collect()
                        })
                        .collect()
                })
                .collect()
        }

        /// Page identity: title + meta/og tags.
        pub fn meta(page: &Page) -> Value {
            let doc = Html::parse_document(&page.body);
            let mut title = String::new();
            if let Ok(tsel) = Selector::parse("title") {
                if let Some(t) = doc.select(&tsel).next() {
                    title = t.text().collect::<String>().trim().to_string();
                }
            }
            let mut metas = serde_json::Map::new();
            if let Ok(msel) = Selector::parse("meta[name], meta[property]") {
                for m in doc.select(&msel) {
                    let key = m
                        .value()
                        .attr("name")
                        .or_else(|| m.value().attr("property"))
                        .unwrap_or("")
                        .to_string();
                    let val = m.value().attr("content").unwrap_or("").to_string();
                    if !key.is_empty() {
                        metas.insert(key, Value::String(val));
                    }
                }
            }
            serde_json::json!({ "title": title, "meta": metas })
        }

        /// Extract images (absolute src + alt).
        pub fn images(page: &Page) -> Vec<Value> {
            let doc = Html::parse_document(&page.body);
            let sel = Selector::parse("img[src]").unwrap();
            let base = url::Url::parse(&page.url).ok();
            doc.select(&sel)
                .filter_map(|el| {
                    let src = el.value().attr("src")?.to_string();
                    let abs = base
                        .as_ref()
                        .and_then(|b| b.join(&src).ok())
                        .map(|u| u.to_string())
                        .unwrap_or_else(|| src.clone());
                    Some(serde_json::json!({
                        "src": abs,
                        "alt": el.value().attr("alt").unwrap_or(""),
                    }))
                })
                .collect()
        }

        /// Fill + submit a form the agent picked from `forms()`.
        /// fields: name -> value. Hidden fields (CSRF tokens) keep their
        /// defaults; agent-supplied values always win.
        pub async fn submit_form(
            &mut self,
            form: &Value,
            fields: &std::collections::HashMap<String, String>,
        ) -> Result<Page, String> {
            let action = form["action"].as_str().unwrap_or("").to_string();
            let method = form["method"].as_str().unwrap_or("GET").to_string();
            let mut pairs: Vec<(String, String)> = Vec::new();
            if let Some(flds) = form["fields"].as_array() {
                for f in flds {
                    let name = f["name"].as_str().unwrap_or("");
                    if name.is_empty() {
                        continue;
                    }
                    if let Some(v) = fields.get(name) {
                        pairs.push((name.to_string(), v.clone()));
                    } else {
                        // keep hidden/submit defaults so CSRF tokens ride along
                        let ftype = f["type"].as_str().unwrap_or("");
                        if ftype == "hidden" {
                            pairs.push((name.to_string(), f["value"].as_str().unwrap_or("").to_string()));
                        }
                    }
                }
            }
            let body = pairs
                .iter()
                .map(|(k, v)| {
                    format!(
                        "{}={}",
                        url::form_urlencoded::byte_serialize(k.as_bytes()).collect::<String>(),
                        url::form_urlencoded::byte_serialize(v.as_bytes()).collect::<String>()
                    )
                })
                .collect::<Vec<_>>()
                .join("&");
            match method.as_str() {
                "POST" => self
                    .post(
                        &action,
                        Some("application/x-www-form-urlencoded"),
                        body.into_bytes(),
                        Some(&action),
                    )
                    .await,
                _ => {
                    let sep = if action.contains('?') { '&' } else { '?' };
                    self.get(&format!("{action}{sep}{body}")).await
                }
            }
        }

        // ------------------------------------------------------- X syndication

        /// X/Twitter posts without login. v0.4 fallback chain so one
        /// dead endpoint never kills the read:
        ///   1. syndication.twitter.com __NEXT_DATA__ (primary)
        ///   2. nitter HTML (.timeline-item) across public instances
        ///   3. nitter RSS across the same instances
        pub async fn x_posts(&mut self, handle: &str, limit: usize) -> Result<Vec<Value>, String> {
            let mut errs: Vec<String> = Vec::new();

            match self.x_syndication(handle, limit).await {
                Ok(posts) if !posts.is_empty() => return Ok(posts),
                Ok(_) => errs.push("syndication: empty".into()),
                Err(e) => errs.push(format!("syndication: {e}")),
            }

            // instances that serve logged-out timelines without JS;
            // /rss (302 -> followed) works even when HTML is a shell
            const INSTANCES: &[&str] = &[
                "https://nitter.net",
                "https://xcancel.com",
                "https://nitter.poast.org",
                "https://lightbrd.com",
                "https://nitter.privacyredirect.com",
                "https://nitter.tie.hackerdairy.org",
            ];
            for base in INSTANCES {
                // RSS first: instances under load serve an HTML shell but
                // still 302->200 the RSS feed (verified live Aug 2026)
                let rss = format!("{base}/{handle}/rss");
                if let Ok(p) = self.get(&rss).await {
                    if p.status == 200 {
                        let posts = parse_nitter_rss(&p.body, limit);
                        if !posts.is_empty() {
                            return Ok(posts);
                        }
                        errs.push(format!("{base}/rss: no items"));
                    } else {
                        errs.push(format!("{base}/rss: status {}", p.status));
                    }
                } else {
                    errs.push(format!("{base}/rss: fetch failed"));
                }
                let url = format!("{base}/{handle}");
                match self.get(&url).await {
                    Ok(p) if p.status == 200 && p.body.contains("timeline-item") => {
                        let posts = parse_nitter_html(&p.body, limit);
                        if !posts.is_empty() {
                            return Ok(posts);
                        }
                        errs.push(format!("{base}: no timeline items"));
                    }
                    Ok(p) => errs.push(format!("{base}: status {}", p.status)),
                    Err(e) => errs.push(format!("{base}: {e}")),
                }
            }
            Err(format!("all X sources failed: {}", errs.join("; ")))
        }

        /// Chain hop 1: the syndication feed (login-free, JSON in shell).
        /// 429s from this endpoint are transient rate limits — back off
        /// and retry (2s, then 5s) before falling to nitter instances.
        async fn x_syndication(&mut self, handle: &str, limit: usize) -> Result<Vec<Value>, String> {
            let url = format!(
                "https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}?showReplies=false&lang=en"
            );
            // X blocklists the Chrome-impersonation TLS shape on this
            // endpoint (every scraper library dials with it), while plain
            // TLS passes — verified live: curl/OpenSSL 200, rquest
            // emulation 429. So syndication rides a plain client.
            let plain = rquest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .redirect(rquest::redirect::Policy::limited(10))
                .build()
                .map_err(|e| e.to_string())?;
            self.total_requests += 1;
            let mut page = plain_get(&plain, &url).await?;
            let mut backoff = std::collections::VecDeque::from([2u64, 5]);
            while page.status == 429 || page.status >= 500 {
                match backoff.pop_front() {
                    Some(secs) => {
                        // honor the server's own reset window when it tells
                        // us one — burning retries inside the window just
                        // deepens the 429 hole (x-rate-limit is 30/window)
                        let wait = rate_limit_wait(&page.headers)
                            .unwrap_or_else(|| std::time::Duration::from_secs(secs));
                        tokio::time::sleep(wait).await;
                        page = plain_get(&plain, &url).await?;
                    }
                    None => break,
                }
            }
            if page.status != 200 {
                return Err(format!("syndication returned {}", page.status));
            }
            let Some(m) = page.body.split_once(r#"<script id="__NEXT_DATA__""#) else {
                return Err("syndication shell changed (no __NEXT_DATA__)".into());
            };
            let json_part: String = m
                .1
                .trim_start_matches(|c: char| c != '>')
                .trim_start_matches('>')
                .split("</script>")
                .next()
                .unwrap_or("")
                .to_string();
            let tree: Value = serde_json::from_str(&json_part)
                .map_err(|e| format!("syndication JSON malformed: {e}"))?;
            let mut out = Vec::new();
            collect_tweets(&tree, limit, &mut out);
            Ok(out)
        }

        /// Save a clearance into the shared vault (Python-compatible).
        pub fn persist_clearance(&self, url: &str, cookies: &[(String, String)]) -> Option<std::path::PathBuf> {
            vault::save(url, self.persona.ua, cookies)
        }
    }

    fn collect_tweets(v: &Value, limit: usize, out: &mut Vec<Value>) {
        if out.len() >= limit {
            return;
        }
        match v {
            Value::Object(map) => {
                let is_tweet = map.contains_key("full_text") && map.contains_key("id_str");
                if is_tweet {
                    out.push(serde_json::json!({
                        "id": map["id_str"].as_str().unwrap_or(""),
                        "text": map["full_text"].as_str().unwrap_or(""),
                        "created": map["created_at"].as_str().unwrap_or(""),
                        "likes": map.get("favorite_count").and_then(|x| x.as_i64()).unwrap_or(0),
                        "rts": map.get("retweet_count").and_then(|x| x.as_i64()).unwrap_or(0),
                    }));
                    return;
                }
                for child in map.values() {
                    collect_tweets(child, limit, out);
                }
            }
            Value::Array(items) => {
                for child in items {
                    collect_tweets(child, limit, out);
                }
            }
            _ => {}
        }
    }

    /// Nitter instance HTML: .timeline-item rows with .tweet-content,
    /// .tweet-link (id), .tweet-date (title attr), .fullname.
    fn parse_nitter_html(body: &str, limit: usize) -> Vec<Value> {
        let doc = Html::parse_document(body);
        let isel = Selector::parse(".timeline-item").unwrap();
        let csel = Selector::parse(".tweet-content").unwrap();
        let lsel = Selector::parse(".tweet-link").unwrap();
        let dsel = Selector::parse(".tweet-date").unwrap();
        let nsel = Selector::parse(".fullname").unwrap();
        let mut out = Vec::new();
        for it in doc.select(&isel) {
            if out.len() >= limit {
                break;
            }
            let Some(link) = it.select(&lsel).next() else { continue };
            let href = link.value().attr("href").unwrap_or("");
            let id = href.rsplit('/').next().unwrap_or("").to_string();
            let text = it
                .select(&csel)
                .next()
                .map(|c| c.text().collect::<String>().trim().to_string())
                .unwrap_or_default();
            if text.is_empty() {
                continue;
            }
            let created = it
                .select(&dsel)
                .next()
                .and_then(|d| d.value().attr("title").map(|s| s.to_string()))
                .unwrap_or_default();
            let author = it
                .select(&nsel)
                .next()
                .map(|n| n.text().collect::<String>().trim().to_string())
                .unwrap_or_default();
            out.push(serde_json::json!({
                "id": id, "text": text, "created": created, "author": author,
                "likes": 0, "rts": 0, "source": "nitter",
            }));
        }
        out
    }

    /// Nitter RSS fallback: plain <item> chunks, CDATA-tolerant.
    fn parse_nitter_rss(body: &str, limit: usize) -> Vec<Value> {
        let mut out = Vec::new();
        for chunk in body.split("<item>").skip(1).take(limit) {
            let text = strip_cdata(&extract_tag(chunk, "description"));
            let title = strip_cdata(&extract_tag(chunk, "title"));
            let created = strip_cdata(&extract_tag(chunk, "pubDate"));
            let link = strip_cdata(&extract_tag(chunk, "link"));
            if text.is_empty() && title.is_empty() {
                continue;
            }
            let author = title.split(':').next().unwrap_or("").to_string();
            out.push(serde_json::json!({
                "id": link.rsplit('/').next().unwrap_or(""),
                "text": html_unescape(&text),
                "created": created,
                "author": author,
                "link": link,
                "likes": 0, "rts": 0, "source": "nitter-rss",
            }));
        }
        out
    }

    fn extract_tag(chunk: &str, tag: &str) -> String {
        let open = format!("<{tag}>");
        let close = format!("</{tag}>");
        match chunk.find(&open) {
            Some(i) => chunk[i + open.len()..]
                .split(&close)
                .next()
                .unwrap_or("")
                .trim()
                .to_string(),
            None => String::new(),
        }
    }

    fn strip_cdata(s: &str) -> String {
        s.trim()
            .strip_prefix("<![CDATA[")
            .map(|r| r.strip_suffix("]]>").unwrap_or(r).to_string())
            .unwrap_or_else(|| s.trim().to_string())
    }

    fn html_unescape(s: &str) -> String {
        s.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&#39;", "'")
            .replace("&#x27;", "'")
    }

    /// Plain (non-emulated) GET used by the syndication source: X 429s
    /// the Chrome-impersonation TLS shape on that endpoint but passes
    /// stock TLS — verified live Aug 2026 (curl 200 / rquest-emu 429).
    async fn plain_get(client: &rquest::Client, url: &str) -> Result<Page, String> {
        // bare requests get 400 from the syndication API — it wants the
        // shape of an embedded timeline widget request (verified live)
        let resp = client
            .get(url)
            .header("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
            .header("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            .header("accept-language", "en-US,en;q=0.9")
            .header("referer", "https://platform.twitter.com/")
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let headers: Vec<(String, String)> = resp
            .headers()
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("").to_string()))
            .collect();
        let body = resp.text().await.map_err(|e| e.to_string())?;
        Ok(Page { url: url.to_string(), status, body, headers })
    }

    /// If a 429 response carries x-rate-limit-reset, wait until then
    /// (plus 1s grace) instead of a fixed backoff. Returns None when the
    /// header is absent or already past.
    fn rate_limit_wait(headers: &[(String, String)]) -> Option<std::time::Duration> {
        let reset: f64 = headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case("x-rate-limit-reset"))
            .and_then(|(_, v)| v.parse().ok())?;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .ok()?
            .as_secs_f64();
        let wait = reset + 1.0 - now;
        // cap the patience: a 7-minute server window is not worth one
        // CLI call — bail to the next source in the chain instead
        if wait > 0.0 && wait < 120.0 {
            Some(std::time::Duration::from_secs_f64(wait))
        } else {
            None
        }
    }

    /// Parse proxy URL: scheme://[user:pass@]host:port
    /// http(s), socks4, socks5, socks5h.
    fn parse_proxy(px: &str) -> Result<Proxy, String> {
        let (scheme, rest) = px.split_once("://").ok_or("proxy needs scheme://host:port")?;
        let (auth, hostport) = match rest.rsplit_once('@') {
            Some((a, h)) => (Some(a), h),
            None => (None, rest),
        };
        let proxy = match scheme.to_ascii_lowercase().as_str() {
            "http" => rquest::Proxy::http(format!("http://{hostport}")),
            "https" => rquest::Proxy::https(format!("https://{hostport}")),
            "socks4" => rquest::Proxy::all(format!("socks4://{hostport}")),
            "socks5" => rquest::Proxy::all(format!("socks5://{hostport}")),
            "socks5h" => rquest::Proxy::all(format!("socks5h://{hostport}")),
            other => return Err(format!("unsupported proxy scheme: {other}")),
        }
        .map_err(|e| e.to_string())?;
        if let Some(creds) = auth {
            let (u, p) = creds.split_once(':').ok_or("proxy auth must be user:pass")?;
            return Ok(proxy.basic_auth(u, p));
        }
        Ok(proxy)
    }
}

// ===========================================================================
// CAPTCHA — detect challenge pages, hand them to the Solver API
// ===========================================================================

mod captcha {
    use serde_json::Value;

    /// What kind of challenge a page carries. Wide v0.4 coverage:
    /// every tech the live battery (2captcha demos, hcaptcha demo,
    /// ivasms, 17.wtf) plus vendor docs showed up in the wild.
    pub fn detect(page_html: &str) -> Vec<&'static str> {
        let h = page_html;
        let mut found = Vec::new();
        if h.contains("challenges.cloudflare.com/turnstile") || h.contains("cf-turnstile") {
            found.push("cloudflare-turnstile");
        }
        if h.contains("hcaptcha.com") || h.contains("h-captcha") || h.contains("hcaptcha_token") {
            found.push("hcaptcha");
        }
        if h.contains("google.com/recaptcha")
            || h.contains("g-recaptcha")
            || h.contains("grecaptcha")
        {
            found.push("recaptcha");
        }
        if h.contains("Just a moment") || h.contains("cf-chl") || h.contains("challenge-platform") {
            found.push("cf-managed-challenge");
        }
        if h.contains("geetest") || h.contains("initGeetest") || h.contains("gt.js") {
            found.push("geetest");
        }
        if h.contains("friendly-captcha") || h.contains("frc-captcha") || h.contains("fcaptcha") {
            found.push("friendly-captcha");
        }
        if h.contains("mtcaptcha") || h.contains("mtcap") {
            found.push("mtcaptcha");
        }
        if h.contains("funcaptcha") || h.contains("arkose") || h.contains("challenge.arkoselabs") {
            found.push("funcaptcha");
        }
        if h.contains("datadome") || h.contains("captcha-delivery.com") {
            found.push("datadome");
        }
        if h.contains("challenge.amazonaws") || h.contains("awswaf") || h.contains("AWS WAF") {
            found.push("aws-waf");
        }
        if h.contains("slider-captcha")
            || h.contains("slidecaptcha")
            || h.contains("puzzle-slider")
            || h.contains("geetest_slider")
            || h.contains("gcaptcha")
        {
            found.push("slider-captcha");
        }
        if !image_endpoints(h).is_empty() {
            found.push("image-captcha");
        }
        found
    }

    /// Raw <img> endpoints that look like image captchas — the classic
    /// distorting-text kind /solve/image (SlotEngine 81% LCSD) handles.
    pub fn image_endpoints(page_html: &str) -> Vec<String> {
        let mut out = Vec::new();
        let mut rest = page_html;
        while let Some(i) = rest.find("<img") {
            let tail = &rest[i..];
            let tag_end = tail.find('>').map(|e| i + e + 1).unwrap_or(rest.len());
            tag_src(rest[i..tag_end].to_string().as_str(), &mut out);
            rest = &rest[tag_end..];
        }
        out
    }

    fn tag_src(tag: &str, out: &mut Vec<String>) {
        let Some(src_i) = tag.find("src=\"") else { return };
        let s = &tag[src_i + 5..];
        let Some(e) = s.find('"') else { return };
        let url = &s[..e];
        let low = url.to_ascii_lowercase();
        if (low.contains("captcha")
            || low.contains("securimage")
            || low.contains("vcaptcha")
            || low.contains("verify-code"))
            && !out.iter().any(|u: &String| u == url)
        {
            out.push(url.to_string());
        }
    }

    /// True when the page is a wall (not the content an agent wanted).
    pub fn is_walled(status: u16, page_html: &str) -> bool {
        status == 403
            || status == 429
            || status == 503
            || page_html.contains("Just a moment")
            || page_html.contains("cf-chl")
            || page_html.contains("challenge-platform")
            || page_html.contains("Attention Required")
            || page_html.contains("unusual traffic")
            || page_html.contains("Pardon Our Interruption")
            || page_html.contains("Access to this page has been denied")
            || page_html.contains("Verify you are human")
            || page_html.contains("captcha-delivery.com")
            || page_html.contains("anomaly-detection")
    }

    /// Pull sitekeys from HTML or JS env (live-verified patterns: attrs,
    /// SvelteKit/Next env objects, generic sitekey assignments).
    pub fn sitekeys(page_html: &str) -> Vec<String> {
        let mut keys: Vec<String> = Vec::new();
        // attribute form
        let mut rest = page_html;
        while let Some(i) = rest.find("data-sitekey=\"") {
            rest = &rest[i + 15..];
            if let Some(j) = rest.find('"') {
                let k = rest[..j].to_string();
                if !keys.contains(&k) {
                    keys.push(k);
                }
                rest = &rest[j..];
            }
        }
        // JS env form (SvelteKit PUBLIC_HCAPTCHA_SITE_KEY etc.) + generic
        let mut r = page_html;
        while let Some(i) = r.find(|c: char| c.is_ascii_uppercase() || c == '_') {
            r = &r[i..];
            let window: String = r.chars().take(400).collect();
            if let Some(colon) = window.find("\":\"") {
                if let Some(end) = window[colon + 3..].find('"') {
                    let key = window[colon + 3..colon + 3 + end].to_string();
                    let name_up = window[..colon].to_ascii_uppercase();
                    if key.len() >= 20
                        && (name_up.contains("HCAPTCHA")
                            || name_up.contains("RECAPTCHA")
                            || name_up.contains("TURNSTILE")
                            || name_up.contains("SITEKEY"))
                    {
                        if !keys.contains(&key) {
                            keys.push(key);
                        }
                    }
                }
            }
            r = &r[1..];
        }
        keys
    }

    /// Hand a detected challenge to the Python solver API.
    /// POST {api}/solve/service with X-API-Key + X-2Captcha-Key headers.
    /// kind: hcaptcha | recaptcha | cloudflare
    pub async fn solve_via_api(
        api: &str,
        api_key: &str,
        twocaptcha_key: &str,
        kind: &str,
        sitekey: &str,
        pageurl: &str,
    ) -> Result<Value, String> {
        let client = rquest::Client::builder()
            .timeout(std::time::Duration::from_secs(300))
            .build()
            .map_err(|e| e.to_string())?;
        let mut req = client
            .post(format!("{api}/solve/service"))
            .header("X-API-Key", api_key)
            .json(&serde_json::json!({
                "kind": kind, "sitekey": sitekey, "pageurl": pageurl
            }));
        if !twocaptcha_key.is_empty() {
            req = req.header("X-2Captcha-Key", twocaptcha_key);
        }
        let resp = req.send().await.map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.map_err(|e| e.to_string())?;
        if status != 200 {
            return Err(format!("solver API {status}: {body}"));
        }
        Ok(body)
    }

    /// Solve an image captcha via the hosted API (multipart upload).
    /// POST {api}/solve/image — returns {engine, text}.
    pub async fn solve_image_api(
        api: &str,
        api_key: &str,
        image_bytes: Vec<u8>,
        engine: &str,
        model: &str,
    ) -> Result<Value, String> {
        let client = rquest::Client::builder()
            .timeout(std::time::Duration::from_secs(120))
            .build()
            .map_err(|e| e.to_string())?;
        let part = rquest::multipart::Part::bytes(image_bytes)
            .file_name("captcha.png")
            .mime_str("image/png")
            .map_err(|e| e.to_string())?;
        let form = rquest::multipart::Form::new()
            .text("engine", engine.to_string())
            .text("model", model.to_string())
            .part("file", part);
        let resp = client
            .post(format!("{api}/solve/image"))
            .header("X-API-Key", api_key)
            .multipart(form)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let status = resp.status().as_u16();
        let body: Value = resp.json().await.map_err(|e| e.to_string())?;
        if status != 200 {
            return Err(format!("solver API {status}: {body}"));
        }
        Ok(body)
    }
}

// ===========================================================================
// BATTERY — live demo-site battery: detect + (optional) solve every wall
// ===========================================================================

mod battery {
    use serde_json::{json, Value};

    use crate::agent::Agent;
    use crate::captcha;

    /// One battery target. `expect` = tech the site carried on the last
    /// live run (Aug 2026); empty = control page that must stay clean.
    pub const TARGETS: &[(&str, &str)] = &[
        // captcha vendor demos — pages built to be probed by solvers
        ("https://2captcha.com/demo/hcaptcha",              "hcaptcha"),
        ("https://2captcha.com/demo/recaptcha-v2",           "recaptcha"),
        ("https://2captcha.com/demo/recaptcha-v3",           "recaptcha"),
        ("https://2captcha.com/demo/recaptcha-v2-invisible", "recaptcha"),
        ("https://2captcha.com/demo/cloudflare-turnstile",   "cloudflare-turnstile"),
        ("https://accounts.hcaptcha.com/demo",               "hcaptcha"),
        ("https://patrickhlauke.github.io/recaptcha/",       "recaptcha"),
        // real walls
        ("https://ivasms.com/login",                         "cf-managed-challenge"),
        ("https://17.wtf",                                   ""),
        // control (must stay clean)
        ("https://httpbin.org/forms/post",                   ""),
    ];

    /// Run the battery. One JSON report per target: status, detected
    /// tech, sitekeys, image endpoints, wall flag, expected-match.
    /// With api_key set and solve=true, solvable walls (hcaptcha /
    /// recaptcha / cloudflare kinds) are also handed to the Solver API.
    pub async fn run(
        agent: &mut Agent,
        api: &str,
        api_key: &str,
        twocaptcha_key: &str,
        solve: bool,
        filter: Option<&str>,
    ) -> Result<Vec<Value>, String> {
        let mut out = Vec::new();
        for (url, expect) in TARGETS {
            if let Some(f) = filter {
                if !url.contains(f) {
                    continue;
                }
            }
            // one retry — Cloudflare-fronted targets occasionally stretch
            // past a cold TLS+HTTP/2 handshake on the first dial
            let page = match agent.get(url).await {
                Ok(p) => p,
                Err(first) => match agent.get(url).await {
                    Ok(p) => p,
                    Err(e) => {
                        out.push(json!({ "url": url, "ok": false, "error": e, "first_error": first }));
                        continue;
                    }
                },
            };
            let tech = captcha::detect(&page.body);
            let keys = captcha::sitekeys(&page.body);
            let imgs = captcha::image_endpoints(&page.body);
            let walled = captcha::is_walled(page.status, &page.body);
            let mut report = json!({
                "url": url,
                "ok": true,
                "status": page.status,
                "walled": walled,
                "tech": tech,
                "sitekeys": keys,
                "image_endpoints": imgs,
                "expected": expect,
                "match": if expect.is_empty() {
                    tech.is_empty()
                } else {
                    tech.iter().any(|t| *t == *expect)
                },
            });

            if solve && (walled || !tech.is_empty()) && !api_key.is_empty() {
                let kind = if tech
                    .iter()
                    .any(|t| *t == "cloudflare-turnstile" || *t == "cf-managed-challenge")
                {
                    "cloudflare"
                } else if tech.iter().any(|t| *t == "hcaptcha") {
                    "hcaptcha"
                } else if tech.iter().any(|t| *t == "recaptcha") {
                    "recaptcha"
                } else {
                    ""
                };
                if !kind.is_empty() {
                    let sitekey = report["sitekeys"]
                        .as_array()
                        .and_then(|a| a.first())
                        .and_then(|k| k.as_str())
                        .unwrap_or("")
                        .to_string();
                    match captcha::solve_via_api(
                        api,
                        api_key,
                        twocaptcha_key,
                        kind,
                        &sitekey,
                        url,
                    )
                    .await
                    {
                        Ok(sol) => report["solved"] = json!({ "kind": kind, "solution": sol }),
                        Err(e) => report["solve_error"] = json!(e),
                    }
                }
            }
            out.push(report);
        }
        Ok(out)
    }
}

// ===========================================================================
// SERVER — JSON-over-HTTP drive API (agents / MCP bridges drive this)
// ===========================================================================

mod server {
    use serde_json::{json, Value};

    use crate::agent::{Agent, AgentOpts};
    use crate::search;

    /// Minimal HTTP listener built on rquest's hyper2 core would pull in
    /// heavy deps; instead we speak a simple line protocol over TCP
    /// (JSONL: one request object per line, one response per line).
    /// Any agent or MCP bridge can drive it with netcat/Python in 5 lines.
    ///
    /// Request:  {"op": "get", "url": "https://..."}
    ///           {"op": "links", "url": "..."}
    ///           {"op": "forms", "url": "..."}
    ///           {"op": "text", "url": "..."}
    ///           {"op": "meta", "url": "..."}
    ///           {"op": "images", "url": "..."}
    ///           {"op": "tables", "url": "..."}
    ///           {"op": "search", "query": "...", "limit": 10}
    ///           {"op": "x", "handle": "elonmusk", "limit": 20}
    ///           {"op": "sniff", "url": "..."}
    ///           {"op": "solve", "url": "..."}
    ///           {"op": "battery", "solve": false, "filter": "2captcha"}
    ///           {"op": "submit", "url": "...", "fields": {"q": "rust"}}
    ///           {"op": "stats"}
    /// Response: {"ok": true, ...} | {"ok": false, "error": "..."}
    ///
    /// Server options via env:
    ///   GHOSTMOUSE_BIND   (default 127.0.0.1:9410)
    ///   GHOSTMOUSE_USER   (default agent-1; new identity per request via "user")
    ///   GHOSTMOUSE_PROXY  (default none)
    ///   GHOSTMOUSE_TIMEOUT (default 30)
    pub async fn run(bind: &str, user: &str, opts: AgentOpts) -> Result<(), String> {
        let addr: std::net::SocketAddr = bind
            .parse()
            .or_else(|_| "127.0.0.1:9410".parse())
            .map_err(|e: std::net::AddrParseError| e.to_string())?;
        let listener = tokio::net::TcpListener::bind(addr)
            .await
            .map_err(|e| format!("bind {addr} failed: {e}"))?;
        eprintln!("[ghostmouse] drive API on {addr} (JSONL/TCP)");

        let agent = std::sync::Arc::new(tokio::sync::Mutex::new(
            Agent::new(user, &opts)?,
        ));

        loop {
            let (mut sock, peer) = match listener.accept().await {
                Ok(x) => x,
                Err(e) => {
                    eprintln!("[ghostmouse] accept error: {e}");
                    continue;
                }
            };
            let agent = agent.clone();
            tokio::spawn(async move {
                use tokio::io::{AsyncReadExt, AsyncWriteExt};
                let mut buf = vec![0u8; 65536];
                let n = match sock.read(&mut buf).await {
                    Ok(0) | Err(_) => return,
                    Ok(n) => n,
                };
                let line = String::from_utf8_lossy(&buf[..n]).trim().to_string();
                if line.is_empty() {
                    return;
                }
                let resp = handle(&agent, &line).await;
                let out = format!("{resp}\n");
                let _ = sock.write_all(out.as_bytes()).await;
                let _ = peer;
            });
        }
    }

    async fn handle(agent: &tokio::sync::Mutex<Agent>, raw: &str) -> Value {
        let req: Value = match serde_json::from_str(raw) {
            Ok(v) => v,
            Err(e) => return json!({ "ok": false, "error": format!("bad JSON: {e}") }),
        };
        let op = req.get("op").and_then(|o| o.as_str()).unwrap_or("").to_string();
        let mut a = agent.lock().await;
        match op.as_str() {
            "get" => {
                let url = req.get("url").and_then(|u| u.as_str()).unwrap_or("");
                if url.is_empty() {
                    return json!({ "ok": false, "error": "url required" });
                }
                match a.get(url).await {
                    Ok(p) => json!({
                        "ok": true,
                        "url": p.url, "status": p.status,
                        "walled": crate::captcha::is_walled(p.status, &p.body),
                        "captcha": crate::captcha::detect(&p.body),
                        "text": crate::agent::Agent::text(&p),
                    }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "links" => read_op(&mut a, &req, "links").await,
            "forms" => read_op(&mut a, &req, "forms").await,
            "text" => read_op(&mut a, &req, "text").await,
            "meta" => read_op(&mut a, &req, "meta").await,
            "images" => read_op(&mut a, &req, "images").await,
            "tables" => read_op(&mut a, &req, "tables").await,
            "search" => {
                let query = req.get("query").and_then(|q| q.as_str()).unwrap_or("");
                let limit = req.get("limit").and_then(|l| l.as_u64()).unwrap_or(10) as usize;
                let engine = req.get("engine").and_then(|e| e.as_str());
                if query.is_empty() {
                    return json!({ "ok": false, "error": "query required" });
                }
                match search::run(&mut a, query, limit, engine).await {
                    Ok(hits) => json!({ "ok": true, "hits": hits }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "x" => {
                let handle_name = req.get("handle").and_then(|h| h.as_str()).unwrap_or("").to_string();
                let limit = req.get("limit").and_then(|l| l.as_u64()).unwrap_or(20) as usize;
                match a.x_posts(&handle_name, limit).await {
                    Ok(posts) => json!({ "ok": true, "posts": posts }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "sniff" => {
                let url = req.get("url").and_then(|u| u.as_str()).unwrap_or("");
                if url.is_empty() {
                    return json!({ "ok": false, "error": "url required" });
                }
                match a.get(url).await {
                    Ok(p) => json!({
                        "ok": true, "url": p.url, "status": p.status,
                        "captcha_tech": crate::captcha::detect(&p.body),
                        "sitekeys": crate::captcha::sitekeys(&p.body),
                    }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "solve" => {
                let url = req.get("url").and_then(|u| u.as_str()).unwrap_or("");
                let api = req
                    .get("api")
                    .and_then(|x| x.as_str())
                    .unwrap_or("http://127.0.0.1:8000")
                    .to_string();
                let api_key = req
                    .get("api_key")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string();
                let two_key = req
                    .get("twocaptcha_key")
                    .and_then(|x| x.as_str())
                    .map(|s| s.to_string())
                    .or_else(|| std::env::var("TWOCAPTCHA_KEY").ok())
                    .unwrap_or_default();
                if url.is_empty() {
                    return json!({ "ok": false, "error": "url required" });
                }
                let page = match a.get(url).await {
                    Ok(p) => p,
                    Err(e) => return json!({ "ok": false, "error": e }),
                };
                let tech = crate::captcha::detect(&page.body);
                if tech.is_empty() {
                    return json!({ "ok": false, "error": "no captcha detected" });
                }
                let keys = crate::captcha::sitekeys(&page.body);
                let kind = match tech[0] {
                    "cloudflare-turnstile" | "cf-managed-challenge" => "cloudflare",
                    "hcaptcha" => "hcaptcha",
                    _ => "recaptcha",
                };
                match crate::captcha::solve_via_api(
                    &api,
                    &api_key,
                    &two_key,
                    kind,
                    keys.first().map(|s| s.as_str()).unwrap_or(""),
                    &page.url,
                )
                .await
                {
                    Ok(out) => json!({ "ok": true, "solution": out }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "battery" => {
                let api = req
                    .get("api")
                    .and_then(|x| x.as_str())
                    .unwrap_or("http://127.0.0.1:8000")
                    .to_string();
                let api_key = req.get("api_key").and_then(|x| x.as_str()).unwrap_or("").to_string();
                let two_key = req
                    .get("twocaptcha_key")
                    .and_then(|x| x.as_str())
                    .map(|s| s.to_string())
                    .or_else(|| std::env::var("TWOCAPTCHA_KEY").ok())
                    .unwrap_or_default();
                let solve = req.get("solve").and_then(|x| x.as_bool()).unwrap_or(false);
                let filter = req.get("filter").and_then(|x| x.as_str()).map(|s| s.to_string());
                match crate::battery::run(&mut a, &api, &api_key, &two_key, solve, filter.as_deref())
                    .await
                {
                    Ok(reports) => json!({
                        "ok": true,
                        "targets": reports.len(),
                        "matched": reports
                            .iter()
                            .filter(|r| r.get("match").and_then(|m| m.as_bool()).unwrap_or(false))
                            .count(),
                        "reports": reports,
                    }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "submit" => {
                let url = req.get("url").and_then(|u| u.as_str()).unwrap_or("");
                if url.is_empty() {
                    return json!({ "ok": false, "error": "url required" });
                }
                let fields: std::collections::HashMap<String, String> = req
                    .get("fields")
                    .and_then(|f| f.as_object())
                    .map(|obj| {
                        obj.iter()
                            .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                            .collect()
                    })
                    .unwrap_or_default();
                // fetch page, pick first form, fill fields, submit
                let page = match a.get(url).await {
                    Ok(p) => p,
                    Err(e) => return json!({ "ok": false, "error": e }),
                };
                let forms = crate::agent::Agent::forms(&page);
                let Some(form) = forms.first() else {
                    return json!({ "ok": false, "error": "no forms on page" });
                };
                match a.submit_form(form, &fields).await {
                    Ok(p) => json!({
                        "ok": true, "status": p.status,
                        "text": crate::agent::Agent::text(&p).chars().take(4000).collect::<String>(),
                    }),
                    Err(e) => json!({ "ok": false, "error": e }),
                }
            }
            "stats" => json!({
                "ok": true,
                "user": a.user,
                "persona": a.persona.name,
                "total_requests": a.total_requests,
                "blocked_trackers": a.blocked_trackers,
            }),
            _ => json!({ "ok": false, "error": format!("unknown op: {op}") }),
        }
    }

    async fn read_op(agent: &mut Agent, req: &Value, kind: &str) -> Value {
        let url = req.get("url").and_then(|u| u.as_str()).unwrap_or("");
        if url.is_empty() {
            return json!({ "ok": false, "error": "url required" });
        }
        match agent.get(url).await {
            Ok(p) => {
                if crate::captcha::is_walled(p.status, &p.body) {
                    return json!({
                        "ok": false,
                        "error": "captcha wall",
                        "status": p.status,
                        "captcha": crate::captcha::detect(&p.body),
                    });
                }
                match kind {
                    "links" => json!({ "ok": true, "url": p.url, "links": crate::agent::Agent::links(&p) }),
                    "forms" => json!({ "ok": true, "url": p.url, "forms": crate::agent::Agent::forms(&p) }),
                    "text" => json!({ "ok": true, "url": p.url, "text": crate::agent::Agent::text(&p) }),
                    "meta" => json!({ "ok": true, "url": p.url, "meta": crate::agent::Agent::meta(&p) }),
                    "images" => json!({ "ok": true, "url": p.url, "images": crate::agent::Agent::images(&p) }),
                    "tables" => json!({ "ok": true, "url": p.url, "tables": crate::agent::Agent::tables(&p) }),
                    _ => json!({ "ok": false, "error": "bad read op" }),
                }
            }
            Err(e) => json!({ "ok": false, "error": e }),
        }
    }
}

// ===========================================================================
// CLI
// ===========================================================================

mod cli {
    use clap::{Parser, Subcommand};
    use serde_json::json;

    use crate::agent::{Agent, AgentOpts};
    use crate::search;

    #[derive(Parser)]
    #[command(name = "ghostmouse", version, about = "Stealth agentic web client for AI agents")]
    pub struct Cli {
        /// Identity: stable per-user persona + rhythm (default: agent-1)
        #[arg(long, default_value = "agent-1")]
        pub user: String,
        /// Proxy: http(s):// or socks5://[user:pass@]host:port
        #[arg(long)]
        pub proxy: Option<String>,
        /// Persona override (win-chrome-136, mac-chrome-135, lin-chrome-134, ...)
        #[arg(long)]
        pub persona: Option<String>,
        /// Per-request timeout (seconds)
        #[arg(long, default_value = "30")]
        pub timeout: u64,
        /// Allow tracker requests (debug)
        #[arg(long)]
        pub no_blocklist: bool,
        #[command(subcommand)]
        pub cmd: Cmd,
    }

    #[derive(Subcommand)]
    pub enum Cmd {
        /// Stealth-fetch a page, print readable text
        Get { url: String },
        /// Fetch and list links as JSON (agent navigation map)
        Links { url: String },
        /// Extract forms (action/method/fields) as JSON
        Form { url: String },
        /// Extract tables as JSON
        Table { url: String },
        /// Extract meta tags + title as JSON
        Meta { url: String },
        /// Extract images as JSON
        Images { url: String },
        /// Detect captcha tech + sitekeys on a page
        Sniff { url: String },
        /// Read X/Twitter posts without login
        X { handle: String, #[arg(default_value = "20")] limit: usize },
        /// Multi-engine search (SearXNG -> DDG -> Bing)
        Search {
            query: String,
            #[arg(default_value = "10")]
            limit: usize,
            #[arg(long)]
            engine: Option<String>,
        },
        /// Solve a detected captcha via the Python solver API
        Solve {
            url: String,
            #[arg(long, default_value = "http://127.0.0.1:8000")]
            api: String,
            #[arg(long, default_value = "")]
            key: String,
            #[arg(long, default_value = "")]
            twocaptcha: String,
        },
        /// Solve a captcha image file via the solver API
        SolveImage {
            image: String,
            #[arg(long, default_value = "http://127.0.0.1:8000")]
            api: String,
            #[arg(long, default_value = "")]
            key: String,
            #[arg(long, default_value = "auto")]
            engine: String,
            #[arg(long, default_value = "lcsd_slot_model.pt")]
            model: String,
        },
        /// Run the live captcha demo-site battery (detect + optional solve)
        Battery {
            #[arg(long, default_value = "http://127.0.0.1:8000")]
            api: String,
            #[arg(long, default_value = "")]
            key: String,
            #[arg(long, default_value = "")]
            twocaptcha: String,
            /// actually solve walls too (needs api key + 2captcha key)
            #[arg(long)]
            solve: bool,
            /// only targets whose URL contains this substring
            #[arg(long)]
            filter: Option<String>,
        },
        /// Fill + submit the first form on a page
        Submit {
            url: String,
            #[arg(long)]
            fields: Vec<String>, // name=value pairs
        },
        /// Serve the JSONL/TCP drive API for agents (default 127.0.0.1:9410)
        Serve {
            #[arg(long, default_value = "127.0.0.1:9410")]
            bind: String,
        },
        /// Show which persona this user maps to
        Whoami,
    }

    fn opts(cli: &Cli) -> AgentOpts {
        AgentOpts {
            proxy: cli.proxy.clone(),
            timeout_secs: cli.timeout,
            persona: cli.persona.clone(),
            no_blocklist: cli.no_blocklist,
        }
    }

    pub async fn run() -> Result<(), String> {
        let cli = Cli::parse();
        let mut agent = Agent::new(&cli.user, &opts(&cli))?;
        match cli.cmd {
            Cmd::Get { url } => {
                let page = agent.get(&url).await?;
                println!("{}", Agent::text(&page));
            }
            Cmd::Links { url } => {
                let page = agent.get(&url).await?;
                println!("{}", serde_json::to_string_pretty(&Agent::links(&page)).unwrap());
            }
            Cmd::Form { url } => {
                let page = agent.get(&url).await?;
                println!("{}", serde_json::to_string_pretty(&Agent::forms(&page)).unwrap());
            }
            Cmd::Table { url } => {
                let page = agent.get(&url).await?;
                println!("{}", serde_json::to_string_pretty(&Agent::tables(&page)).unwrap());
            }
            Cmd::Meta { url } => {
                let page = agent.get(&url).await?;
                println!("{}", serde_json::to_string_pretty(&Agent::meta(&page)).unwrap());
            }
            Cmd::Images { url } => {
                let page = agent.get(&url).await?;
                println!("{}", serde_json::to_string_pretty(&Agent::images(&page)).unwrap());
            }
            Cmd::Sniff { url } => {
                let page = agent.get(&url).await?;
                let tech = crate::captcha::detect(&page.body);
                let keys = crate::captcha::sitekeys(&page.body);
                println!("{}", serde_json::to_string_pretty(&json!({
                    "url": page.url, "status": page.status,
                    "captcha_tech": tech, "sitekeys": keys,
                    "walled": crate::captcha::is_walled(page.status, &page.body),
                })).unwrap());
            }
            Cmd::X { handle, limit } => {
                let posts = agent.x_posts(&handle, limit).await?;
                println!("{}", serde_json::to_string_pretty(&posts).unwrap());
            }
            Cmd::Search { query, limit, engine } => {
                let hits = search::run(&mut agent, &query, limit, engine.as_deref()).await?;
                let out: Vec<serde_json::Value> = hits
                    .iter()
                    .map(|h| json!({ "title": h.title, "url": h.url, "snippet": h.snippet }))
                    .collect();
                println!("{}", serde_json::to_string_pretty(&out).unwrap());
            }
            Cmd::Solve { url, api, key, twocaptcha } => {
                let page = agent.get(&url).await?;
                let tech = crate::captcha::detect(&page.body);
                if tech.is_empty() {
                    return Err("no captcha detected on this page".into());
                }
                let keys = crate::captcha::sitekeys(&page.body);
                let kind = match tech[0] {
                    "cloudflare-turnstile" | "cf-managed-challenge" => "cloudflare",
                    "hcaptcha" => "hcaptcha",
                    _ => "recaptcha",
                };
                let out = crate::captcha::solve_via_api(
                    &api, &key, &twocaptcha, kind,
                    keys.first().map(|s| s.as_str()).unwrap_or(""),
                    &page.url,
                )
                .await?;
                println!("{}", serde_json::to_string_pretty(&out).unwrap());
            }
            Cmd::SolveImage { image, api, key, engine, model } => {
                let bytes = std::fs::read(&image).map_err(|e| format!("read {image}: {e}"))?;
                let out = crate::captcha::solve_image_api(&api, &key, bytes, &engine, &model).await?;
                println!("{}", serde_json::to_string_pretty(&out).unwrap());
            }
            Cmd::Battery { api, key, twocaptcha, solve, filter } => {
                let reports =
                    crate::battery::run(&mut agent, &api, &key, &twocaptcha, solve, filter.as_deref())
                        .await?;
                let matched = reports
                    .iter()
                    .filter(|r| r.get("match").and_then(|m| m.as_bool()).unwrap_or(false))
                    .count();
                println!("{}", serde_json::to_string_pretty(&json!({
                    "targets": reports.len(), "matched": matched, "reports": reports,
                })).unwrap());
            }
            Cmd::Submit { url, fields } => {
                let mut map = std::collections::HashMap::new();
                for f in fields {
                    let (k, v) = f.split_once('=').ok_or("fields must be name=value")?;
                    map.insert(k.to_string(), v.to_string());
                }
                let page = agent.get(&url).await?;
                let forms = Agent::forms(&page);
                let Some(form) = forms.first() else {
                    return Err("no forms on this page".into());
                };
                let result = agent.submit_form(form, &map).await?;
                println!("{}", serde_json::to_string_pretty(&json!({
                    "status": result.status,
                    "text": Agent::text(&result).chars().take(4000).collect::<String>(),
                })).unwrap());
            }
            Cmd::Serve { ref bind } => {
                // opts(&cli) clones what it needs; cli stays alive for the serve loop
                let serve_opts = opts(&cli);
                crate::server::run(&bind, &cli.user, serve_opts).await?;
            }
            Cmd::Whoami => {
                println!("{}", serde_json::to_string_pretty(&json!({
                    "user": cli.user,
                    "persona": agent.persona.name,
                    "locale": agent.persona.locale,
                    "features": ["chrome-tls", "tracker-block", "vault-clearance",
                                "agentic-reads", "form-submit", "multi-search",
                                "captcha-solve", "captcha-battery",
                                "x-syndication", "x-fallback-chain", "jsonl-drive-api"],
                })).unwrap());
            }
        }
        Ok(())
    }
}

#[tokio::main]
async fn main() {
    if let Err(e) = cli::run().await {
        eprintln!("[!] {e}");
        std::process::exit(1);
    }
}

