//! GhostEngine M3 — form submit engine: fill → encode → submit.
//!
//! Teen layers, sab std-only:
//! 1. `FormData` — urlencoded + multipart/form-data body builders.
//! 2. `FormSubmit` / `LoginFlow` — `Page::forms()` surface ko fill karke
//!    `Transport` trait ke through bhejte hain. Network I/O jaan-boojh kar
//!    trait ke piche hai: net::Session (parallel milestone) isse
//!    implement karega, tests mock se chalte hain — integration baad me
//!    wire hoga, code nahi badlega.
//! 3. `OAuthFlow` — authorize + token-exchange builders (Google/GitHub
//!    generic patterns) + std-only API-key generator.

use std::collections::BTreeMap;

use crate::{resolve_url, Page};

// ---------------------------------------------------------------- data ----

/// Named form data — text fields + file uploads. Builder-style chaining.
#[derive(Debug, Clone, Default)]
pub struct FormData {
    fields: Vec<(String, String)>,
    /// (field-name, filename, content) — sirf multipart body me jaata hai.
    files: Vec<(String, String, Vec<u8>)>,
}

impl FormData {
    pub fn new() -> Self {
        FormData::default()
    }

    /// Text field add (chainable).
    pub fn field(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.fields.push((name.into(), value.into()));
        self
    }

    /// File upload add (chainable). Content-Type application/octet-stream
    /// (server-side sniff uska kaam hai).
    pub fn file(
        mut self,
        field: impl Into<String>,
        filename: impl Into<String>,
        content: Vec<u8>,
    ) -> Self {
        self.files.push((field.into(), filename.into(), content));
        self
    }

    pub fn fields(&self) -> &[(String, String)] {
        &self.fields
    }

    pub fn files(&self) -> &[(String, String, Vec<u8>)] {
        &self.files
    }

    /// application/x-www-form-urlencoded body. Files ignore hoti hain —
    /// unke liye multipart hi ek hi format hai.
    pub fn urlencode(&self) -> String {
        query_pairs(&self.fields)
    }

    /// multipart/form-data body, given boundary ke saath.
    pub fn multipart(&self, boundary: &str) -> Vec<u8> {
        let mut out = Vec::new();
        for (k, v) in &self.fields {
            out.extend_from_slice(
                format!(
                    "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n",
                    boundary,
                    sanitize(k)
                )
                .as_bytes(),
            );
            out.extend_from_slice(v.as_bytes());
            out.extend_from_slice(b"\r\n");
        }
        for (field, filename, content) in &self.files {
            out.extend_from_slice(
                format!(
                    "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\nContent-Type: application/octet-stream\r\n\r\n",
                    boundary,
                    sanitize(field),
                    sanitize(filename)
                )
                .as_bytes(),
            );
            out.extend_from_slice(content);
            out.extend_from_slice(b"\r\n");
        }
        out.extend_from_slice(format!("--{}--\r\n", boundary).as_bytes());
        out
    }

    /// Multipart body + matching Content-Type header — fresh random
    /// boundary generate karke dono return karta hai.
    pub fn multipart_body(&self) -> (Vec<u8>, String) {
        let b = generate_boundary();
        (self.multipart(&b), multipart_content_type(&b))
    }
}

/// Multipart Content-Type header value.
pub fn multipart_content_type(boundary: &str) -> String {
    format!("multipart/form-data; boundary={}", boundary)
}

/// Header value sanitize — quotes/backslash/CRLF header-injection se
/// bachne ke liye strip.
fn sanitize(s: &str) -> String {
    s.chars()
        .filter(|c| *c != '"' && *c != '\\' && *c != '\r' && *c != '\n')
        .collect()
}

/// application/x-www-form-urlencoded percent-encoding (WHATWG serializer):
/// unreserved = ALPHA / DIGIT / `*` `-` `.` `_` raw, space → '+', baaki
/// (incl. `~`) %XX (UTF-8 bytes).
pub fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'*' | b'-' | b'_' | b'.' => {
                out.push(b as char)
            }
            b' ' => out.push('+'),
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

/// k=v pairs → 'a=1&b=2' query string (har part percent-encoded).
fn query_pairs(pairs: &[(String, String)]) -> String {
    pairs
        .iter()
        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
        .collect::<Vec<_>>()
        .join("&")
}

// ------------------------------------------------------------- transport --

/// HTTP method — form surface ke liye kaafi.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Method {
    Get,
    Post,
}

impl Method {
    /// HTML form method attr parse — unknown/missing → GET (HTML default).
    pub fn parse(m: &str) -> Method {
        match m.trim().to_ascii_uppercase().as_str() {
            "POST" => Method::Post,
            _ => Method::Get,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Method::Get => "GET",
            Method::Post => "POST",
        }
    }
}

/// Ek bahar-jaata request — url, method, headers, body.
#[derive(Debug, Clone)]
pub struct Request {
    pub url: String,
    pub method: Method,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    pub fn get(url: impl Into<String>) -> Self {
        Request {
            url: url.into(),
            method: Method::Get,
            headers: Vec::new(),
            body: Vec::new(),
        }
    }

    pub fn post(url: impl Into<String>, body: Vec<u8>) -> Self {
        Request {
            url: url.into(),
            method: Method::Post,
            headers: Vec::new(),
            body,
        }
    }

    /// Header add (chainable).
    pub fn header(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.headers.push((name.into(), value.into()));
        self
    }

    /// Case-insensitive header lookup.
    pub fn header_value(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}

/// Ek aaya-hua response.
#[derive(Debug, Clone)]
pub struct Response {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    /// Body → lossy UTF-8 string.
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }

    /// Case-insensitive header lookup.
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }

    /// 2xx/3xx = ok.
    pub fn ok(&self) -> bool {
        (200..400).contains(&self.status)
    }
}

/// Network I/O ka single point — form/login/OAuth sab isi se jaate hain.
///
/// net::Session (parallel me ban raha hai) isse implement karke cookies,
/// redirects, TLS dega; tab tak tests MockTransport se. `&self` rakha hai
/// (reqwest Client style) — session state (cookie jar) interior
/// mutability me rahega, trait nahi badlega.
pub trait Transport {
    /// Ek request bhejo, response lao. Err = transport-level failure
    /// (network dead, DNS fail...) — HTTP error status Err nahi hai.
    fn send(&self, req: &Request) -> Result<Response, String>;

    /// Convenience POST — body + content-type ke saath.
    fn post(&self, url: &str, body: &[u8], content_type: &str) -> Result<Response, String> {
        let req = Request::post(url, body.to_vec()).header("Content-Type", content_type);
        self.send(&req)
    }
}

// ---------------------------------------------------------------- submit --

/// Page ke extracted form ko fill karke Transport se submit.
pub struct FormSubmit;

impl FormSubmit {
    /// Form ki action URL resolve karo (relative → absolute, empty action =
    /// current page URL — browser jaisa).
    pub fn action_url(page: &Page, base_url: &str, form_index: usize) -> Result<String, String> {
        let forms = page.forms();
        let (action, _, _) = forms.get(form_index).ok_or_else(|| {
            format!(
                "form #{} nahi mila (page pe {} forms hain)",
                form_index,
                forms.len()
            )
        })?;
        Ok(if action.is_empty() {
            base_url.to_string()
        } else {
            resolve_url(base_url, action)
        })
    }

    /// Form submit: DOM default values + `fills` overrides se body banao,
    /// method (GET → query string, POST → body) ke hisaab se request
    /// bhejo. Returns (sent request, response) — caller inspect kar sake.
    ///
    /// - enctype=multipart/form-data → multipart body (fresh boundary)
    /// - warna POST → urlencoded body, GET → urlencoded query string
    pub fn submit(
        page: &Page,
        base_url: &str,
        form_index: usize,
        fills: &BTreeMap<String, String>,
        t: &dyn Transport,
    ) -> Result<(Request, Response), String> {
        let url = Self::action_url(page, base_url, form_index)?;

        let forms = page.forms();
        let (_, method, fields) = forms.get(form_index).ok_or_else(|| {
            format!(
                "form #{} nahi mila (page pe {} forms hain)",
                form_index,
                forms.len()
            )
        })?;
        let method = Method::parse(method);

        // fill: DOM ke default values base, fills override (naam se match).
        let mut data = FormData::new();
        for (name, _ty, default_val) in fields {
            if name.is_empty() {
                continue;
            }
            let val = fills
                .get(name)
                .cloned()
                .unwrap_or_else(|| default_val.clone());
            data = data.field(name, val);
        }

        // enctype form node se — forms() tuple me nahi aata, node se padho.
        let enctype = page
            .select("form")
            .get(form_index)
            .and_then(|f| f.attr("enctype"))
            .unwrap_or("")
            .to_ascii_lowercase();

        let req = match method {
            Method::Get => {
                let qs = data.urlencode();
                let url = if url.contains('?') {
                    format!("{}&{}", url, qs)
                } else {
                    format!("{}?{}", url, qs)
                };
                Request::get(url)
            }
            Method::Post if enctype.contains("multipart") => {
                let (body, ctype) = data.multipart_body();
                Request::post(url, body).header("Content-Type", ctype)
            }
            Method::Post => Request::post(url, data.urlencode().into_bytes())
                .header("Content-Type", "application/x-www-form-urlencoded"),
        };

        let resp = t
            .send(&req)
            .map_err(|e| format!("submit {} fail: {}", req.url, e))?;
        Ok((req, resp))
    }
}

// ------------------------------------------------------------ login flow --

/// Login ka ek step: page URL + us page ka form index + fills.
pub struct LoginStep {
    /// Step ka page URL. "" → previous step ki submit URL reuse (same-page
    /// multi-form flows, POST-then-form scenarios).
    pub url: String,
    pub form_index: usize,
    pub fills: BTreeMap<String, String>,
}

impl LoginStep {
    pub fn new(url: impl Into<String>, form_index: usize) -> Self {
        LoginStep {
            url: url.into(),
            form_index,
            fills: BTreeMap::new(),
        }
    }

    /// Field fill (chainable).
    pub fn fill(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        self.fills.insert(name.into(), value.into());
        self
    }
}

/// Multi-step login: har step = (page fetch → form submit).
/// Steps ek hi Transport/session se jaate hain, isliye cookies
/// (session token) Transport ke andar flow karte hain.
pub struct LoginFlow {
    pub steps: Vec<LoginStep>,
}

impl LoginFlow {
    /// Step 1 se flow start (chainable: `.fill(...).then(...)`).
    pub fn new(url: impl Into<String>, form_index: usize) -> Self {
        LoginFlow {
            steps: vec![LoginStep::new(url, form_index)],
        }
    }

    /// Last step me field fill (chainable).
    pub fn fill(mut self, name: impl Into<String>, value: impl Into<String>) -> Self {
        if let Some(last) = self.steps.last_mut() {
            last.fills.insert(name.into(), value.into());
        }
        self
    }

    /// Naya step add (chainable). url "" → previous submit URL reuse.
    pub fn then(mut self, url: impl Into<String>, form_index: usize) -> Self {
        self.steps.push(LoginStep::new(url, form_index));
        self
    }

    /// Poori flow chalao. Har step: GET page → parse → submit form.
    /// Returns har step ka (submit request, response) — URLs, bodies
    /// caller inspect kar sake.
    pub fn run(&self, t: &dyn Transport) -> Result<Vec<(Request, Response)>, String> {
        let flow_base = self
            .steps
            .first()
            .map(|s| s.url.clone())
            .ok_or("LoginFlow khali hai")?;
        let mut last_url = flow_base.clone();
        let mut out = Vec::new();

        for (i, step) in self.steps.iter().enumerate() {
            let page_url = if step.url.is_empty() {
                last_url.clone()
            } else {
                resolve_url(&flow_base, &step.url)
            };

            let get_req = Request::get(&page_url);
            let resp = t
                .send(&get_req)
                .map_err(|e| format!("step {}: GET {} fail: {}", i + 1, page_url, e))?;

            let page = Page::parse(&resp.text());
            let (req, resp) = FormSubmit::submit(&page, &page_url, step.form_index, &step.fills, t)
                .map_err(|e| format!("step {}: submit fail: {}", i + 1, e))?;
            last_url = req.url.clone();
            out.push((req, resp));
        }
        Ok(out)
    }
}

// ------------------------------------------------------------------ oauth --

/// OAuth 2.0 authorization-code flow — generic builders.
/// Skeleton: authorize URL + token exchange request. Redirect/CSRF/state
/// handling caller (GUI/session layer) ka kaam hai.
#[derive(Debug, Clone)]
pub struct OAuthFlow {
    pub provider: String,
    pub client_id: String,
    pub client_secret: Option<String>,
    pub redirect_uri: String,
    pub scope: String,
    pub authorize_endpoint: String,
    pub token_endpoint: String,
}

impl OAuthFlow {
    /// Google generic pattern.
    pub fn google(
        client_id: impl Into<String>,
        redirect_uri: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self::custom(
            "google",
            "https://accounts.google.com/o/oauth2/v2/auth",
            "https://oauth2.googleapis.com/token",
            client_id,
            redirect_uri,
            scope,
        )
    }

    /// GitHub generic pattern.
    pub fn github(
        client_id: impl Into<String>,
        redirect_uri: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        Self::custom(
            "github",
            "https://github.com/login/oauth/authorize",
            "https://github.com/login/oauth/access_token",
            client_id,
            redirect_uri,
            scope,
        )
    }

    /// Kisi bhi provider — apne endpoints ke saath.
    pub fn custom(
        provider: impl Into<String>,
        authorize_endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        client_id: impl Into<String>,
        redirect_uri: impl Into<String>,
        scope: impl Into<String>,
    ) -> Self {
        OAuthFlow {
            provider: provider.into(),
            client_id: client_id.into(),
            client_secret: None,
            redirect_uri: redirect_uri.into(),
            scope: scope.into(),
            authorize_endpoint: authorize_endpoint.into(),
            token_endpoint: token_endpoint.into(),
        }
    }

    /// Client secret attach (chainable) — token exchange me jaata hai.
    pub fn secret(mut self, client_secret: impl Into<String>) -> Self {
        self.client_secret = Some(client_secret.into());
        self
    }

    /// Authorize URL + random state. Caller state save kare, redirect
    /// aane pe compare kare (CSRF). Browser/user authorize URL kholta hai.
    pub fn authorize_url(&self) -> (String, String) {
        let state = hex(&random_bytes(16));
        let qs = query_pairs(&[
            ("response_type".into(), "code".into()),
            ("client_id".into(), self.client_id.clone()),
            ("redirect_uri".into(), self.redirect_uri.clone()),
            ("scope".into(), self.scope.clone()),
            ("state".into(), state.clone()),
        ]);
        (format!("{}?{}", self.authorize_endpoint, qs), state)
    }

    /// Authorization-code → access-token exchange request (POST token
    /// endpoint, urlencoded body).
    pub fn token_request(&self, code: &str) -> Request {
        let mut d = FormData::new()
            .field("grant_type", "authorization_code")
            .field("code", code)
            .field("redirect_uri", self.redirect_uri.clone())
            .field("client_id", self.client_id.clone());
        if let Some(sec) = &self.client_secret {
            d = d.field("client_secret", sec.clone());
        }
        Request::post(self.token_endpoint.clone(), d.urlencode().into_bytes())
            .header("Content-Type", "application/x-www-form-urlencoded")
            .header("Accept", "application/json")
    }

    /// Refresh-token grant request.
    pub fn refresh_request(&self, refresh_token: &str) -> Request {
        let mut d = FormData::new()
            .field("grant_type", "refresh_token")
            .field("refresh_token", refresh_token)
            .field("client_id", self.client_id.clone());
        if let Some(sec) = &self.client_secret {
            d = d.field("client_secret", sec.clone());
        }
        Request::post(self.token_endpoint.clone(), d.urlencode().into_bytes())
            .header("Content-Type", "application/x-www-form-urlencoded")
            .header("Accept", "application/json")
    }
}

// --------------------------------------------------------------- entropy --

/// Std-only random bytes: pehle /dev/urandom (kernel CSPRNG), fallback
/// time+pid xorshift (non-Linux / emergency — documented weaker).
pub fn random_bytes(n: usize) -> Vec<u8> {
    use std::io::Read;
    if n == 0 {
        return Vec::new();
    }
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        let mut buf = vec![0u8; n];
        if f.read_exact(&mut buf).is_ok() {
            return buf;
        }
    }
    // Fallback CSPRNG nahi hai — boot-time nonce, pid, time mix karke
    // xorshift. Linux pe kabhi hit nahi hota (upar wala path).
    let mut state = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0x9E37_79B9_7F4A_7C15)
        ^ ((std::process::id() as u64) << 32)
        ^ 0xdead_beef_cafe_f00d;
    let mut out = Vec::with_capacity(n);
    while out.len() < n {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        out.extend_from_slice(&state.to_le_bytes());
    }
    out.truncate(n);
    out
}

/// Lowercase hex encode.
pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{:02x}", b)).collect()
}

/// Random API key — 32 bytes, lowercase hex (64 chars), std-only entropy.
pub fn api_key() -> String {
    hex(&random_bytes(32))
}

/// Random multipart boundary (16 random bytes hex — collision-practically-
/// impossible, content me match nahi hota).
pub fn generate_boundary() -> String {
    format!("----GhostFormBoundary{}", hex(&random_bytes(16)))
}

// ----------------------------------------------------------------- tests --

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Page;
    use std::cell::RefCell;

    /// URL substring match karke canned HTML serve karne wala transport.
    /// Har request log me jaata hai — assert karne ke liye.
    struct Mock {
        routes: Vec<(String, String)>, // (url contains → html)
        log: RefCell<Vec<Request>>,
    }

    impl Mock {
        fn new(routes: &[(&str, &str)]) -> Self {
            Mock {
                routes: routes
                    .iter()
                    .map(|(n, h)| (n.to_string(), h.to_string()))
                    .collect(),
                log: RefCell::new(Vec::new()),
            }
        }
    }

    impl Transport for Mock {
        fn send(&self, req: &Request) -> Result<Response, String> {
            self.log.borrow_mut().push(req.clone());
            // exact-path route match (URL decode nahi karte — test URLs
            // plain hain). Substring nahi — "/dologin" me "login" collide
            // karta, galat page serve hota.
            let path = req.url.split('?').next().unwrap_or("");
            for (needle, html) in &self.routes {
                // suffix-match: needle "/login" full-URL "https://x.io/login"
                // pe bhi lage (URL-decode nahi — test URLs plain hain)
                if path == needle.as_str()
                    || (needle.starts_with('/')
                        && path.strip_suffix(needle.as_str()).is_some())
                {
                    return Ok(Response {
                        status: 200,
                        headers: vec![("Content-Type".into(), "text/html".into())],
                        body: html.as_bytes().to_vec(),
                    });
                }
            }
            Err(format!("no route for {}", req.url))
        }
    }

    fn logged(m: &Mock) -> Vec<Request> {
        m.log.borrow().clone()
    }

    // ---- urlencode ----

    #[test]
    fn urlencode_spec() {
        // space → '+', & = ? percent-encoded, UTF-8 multibyte %XX%XX,
        // unreserved (- _ . ~) raw.
        let d = FormData::new()
            .field("a", "1")
            .field("b", "x y")
            .field("c", "a&b=c")
            .field("k-_.~", "safe-chars_.");
        assert_eq!(d.urlencode(), "a=1&b=x+y&c=a%26b%3Dc&k-_.%7E=safe-chars_.");
        // ~ percent-encoded hota hai (urlencoded spec me ~ reserved
        // history ki wajah se encode hota hai — %7E — RFC-conservative)
    }

    #[test]
    fn urlencode_utf8() {
        let d = FormData::new().field("é", "héllo wörld");
        assert_eq!(d.urlencode(), "%C3%A9=h%C3%A9llo+w%C3%B6rld");
        assert_eq!(urlencode("a/b:c@d"), "a%2Fb%3Ac%40d");
    }

    // ---- multipart ----

    #[test]
    fn multipart_framing() {
        let d = FormData::new()
            .field("name", "ghost rider")
            .file("doc", "a.txt", vec![1, 2, 3, 4]);
        let body = d.multipart("XYZ");
        let s = String::from_utf8(body.clone()).unwrap();
        assert!(s.starts_with("--XYZ\r\n"));
        assert!(s.contains("Content-Disposition: form-data; name=\"name\"\r\n\r\nghost rider\r\n"));
        assert!(s.contains("Content-Disposition: form-data; name=\"doc\"; filename=\"a.txt\"\r\n"));
        assert!(s.contains("Content-Type: application/octet-stream\r\n\r\n"));
        assert!(s.ends_with("--XYZ--\r\n"));
        assert!(body.windows(4).any(|w| w == [1, 2, 3, 4]));
        // file bytes \r\n framing ke beech hona chahiye, not base64
        let idx = s.find(&[1u8 as char, 2u8 as char].iter().collect::<String>());
        assert!(idx.is_some());
    }

    #[test]
    fn multipart_content_type_and_boundary() {
        let d = FormData::new().field("f", "v");
        let (body, ctype) = d.multipart_body();
        assert!(ctype.starts_with("multipart/form-data; boundary=----GhostFormBoundary"));
        let s = String::from_utf8(body).unwrap();
        assert!(s.contains("--GhostFormBoundary"));
        assert!(s.ends_with("--\r\n"));
        // do boundaries alag hone chahiye
        assert_ne!(generate_boundary(), generate_boundary());
    }

    // ---- FormSubmit ----

    #[test]
    fn submit_post_urlencoded() {
        let html = "<form action='/login' method='post'>\
                    <input name='email' type='email'>\
                    <input name='pass' type='password'>\
                    </form>";
        let page = Page::parse(html);
        let mut fills = BTreeMap::new();
        fills.insert("email".to_string(), "a@b.io".to_string());
        fills.insert("pass".to_string(), "p w".to_string());

        let m = Mock::new(&[("/login", "ok")]);
        let (req, resp) = FormSubmit::submit(&page, "https://x.io/page", 0, &fills, &m).unwrap();

        assert_eq!(req.url, "https://x.io/login");
        assert_eq!(req.method, Method::Post);
        assert_eq!(
            req.header_value("Content-Type").unwrap(),
            "application/x-www-form-urlencoded"
        );
        assert_eq!(
            String::from_utf8(req.body).unwrap(),
            "email=a%40b.io&pass=p+w"
        );
        assert_eq!(resp.status, 200);
    }

    #[test]
    fn submit_get_query_string_and_defaults() {
        let html = "<form action='/s' method='get'>\
                    <input name='q' value='ghost'>\
                    <input name='t' type='hidden' value='abc'>\
                    </form>";
        let page = Page::parse(html);
        let mut fills = BTreeMap::new();
        fills.insert("q".to_string(), "new query".to_string()); // override

        let m = Mock::new(&[("/s", "ok")]);
        let (req, _resp) = FormSubmit::submit(&page, "https://x.io/home", 0, &fills, &m).unwrap();

        assert_eq!(req.method, Method::Get);
        assert_eq!(req.url, "https://x.io/s?q=new+query&t=abc");
        assert!(req.body.is_empty());
    }

    #[test]
    fn submit_multipart_enctype() {
        let html = "<form action='/up' method='post' enctype='multipart/form-data'>\
                    <input name='f' value='v'>\
                    </form>";
        let page = Page::parse(html);
        let fills = BTreeMap::new();

        let m = Mock::new(&[("/up", "ok")]);
        let (req, _r) = FormSubmit::submit(&page, "https://x.io/", 0, &fills, &m).unwrap();

        assert_eq!(req.method, Method::Post);
        assert!(req
            .header_value("Content-Type")
            .unwrap()
            .starts_with("multipart/form-data; boundary="));
        let body = String::from_utf8(req.body).unwrap();
        assert!(body.contains("name=\"f\""));
        assert!(body.contains("v"));
        assert!(body.ends_with("--\r\n"));
    }

    #[test]
    fn submit_empty_action_and_bad_index() {
        // empty action → current page URL
        let html = "<form method='post'><input name='a' value='1'></form>";
        let page = Page::parse(html);
        let m = Mock::new(&[("/page", "ok")]);
        let fills = BTreeMap::new();
        let (req, _) = FormSubmit::submit(&page, "https://x.io/page", 0, &fills, &m).unwrap();
        assert_eq!(req.url, "https://x.io/page");

        // out-of-range index
        let err = FormSubmit::submit(&page, "https://x.io/page", 5, &fills, &m);
        assert!(err.is_err());
        assert!(err.unwrap_err().contains("form #5"));
    }

    // ---- LoginFlow ----

    #[test]
    fn login_flow_two_steps() {
        let page1 = "<form action='/dologin' method='post'>\
                     <input name='email' type='email'>\
                     <input name='pass' type='password'>\
                     </form>";
        let page2 = "<form action='/2fa' method='post'>\
                     <input name='otp' type='text'>\
                     </form>";

        let m = Mock::new(&[("/login", page1), ("/dologin", page2), ("/2fa", "<h1>welcome</h1>")]);

        let flow = LoginFlow::new("https://x.io/login", 0)
            .fill("email", "a@b.io")
            .fill("pass", "secret1")
            .then("", 0) // same URL (last submit URL: /dologin)
            .fill("otp", "998877");

        let out = flow.run(&m).unwrap();
        assert_eq!(out.len(), 2);

        // har step: GET page + POST submit = 4 requests total
        let log = logged(&m);
        assert_eq!(log.len(), 4);
        assert_eq!(log[0].url, "https://x.io/login");
        assert_eq!(log[0].method, Method::Get);
        assert_eq!(log[1].url, "https://x.io/dologin");
        assert_eq!(log[1].method, Method::Post);
        assert_eq!(
            String::from_utf8(log[1].body.clone()).unwrap(),
            "email=a%40b.io&pass=secret1"
        );
        assert_eq!(log[2].url, "https://x.io/dologin"); // "" → last_url reuse
        assert_eq!(log[3].url, "https://x.io/2fa");
        assert_eq!(
            String::from_utf8(log[3].body.clone()).unwrap(),
            "otp=998877"
        );
    }

    // ---- OAuth ----

    #[test]
    fn oauth_google_authorize_url() {
        let flow = OAuthFlow::google("my-client-id", "https://app.io/cb", "openid email");
        let (url, state) = flow.authorize_url();
        assert!(url.starts_with("https://accounts.google.com/o/oauth2/v2/auth?"));
        assert!(url.contains("response_type=code"));
        assert!(url.contains("client_id=my-client-id"));
        assert!(url.contains("redirect_uri=https%3A%2F%2Fapp.io%2Fcb"));
        assert!(url.contains("scope=openid+email"));
        assert!(url.contains(&format!("state={}", state)));
        assert_eq!(state.len(), 32); // 16 bytes hex
        assert!(state.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn oauth_github_token_request() {
        let flow =
            OAuthFlow::github("gh-client", "https://app.io/cb", "read:user").secret("gh-secret");
        let req = flow.token_request("CODE123");
        assert_eq!(req.url, "https://github.com/login/oauth/access_token");
        assert_eq!(req.method, Method::Post);
        assert_eq!(
            req.header_value("Content-Type").unwrap(),
            "application/x-www-form-urlencoded"
        );
        let body = String::from_utf8(req.body).unwrap();
        assert!(body.contains("grant_type=authorization_code"));
        assert!(body.contains("code=CODE123"));
        assert!(body.contains("client_id=gh-client"));
        assert!(body.contains("client_secret=gh-secret"));

        // refresh
        let rr = flow.refresh_request("RT0");
        let rb = String::from_utf8(rr.body).unwrap();
        assert!(rb.contains("grant_type=refresh_token"));
        assert!(rb.contains("refresh_token=RT0"));
    }

    // ---- entropy / api key ----

    #[test]
    fn api_key_random_32byte_hex() {
        let k1 = api_key();
        let k2 = api_key();
        assert_eq!(k1.len(), 64); // 32 bytes → 64 hex chars
        assert!(k1
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
        assert_ne!(k1, k2); // entropy: consecutive calls differ
        for _ in 0..8 {
            assert_ne!(api_key(), k1);
        }
        assert_eq!(random_bytes(0).len(), 0);
        assert_eq!(random_bytes(31).len(), 31);
        assert_eq!(hex(&[0xde, 0xad, 0xbe, 0xef]), "deadbeef");
    }
}
