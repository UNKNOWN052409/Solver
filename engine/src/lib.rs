//! GhostEngine — Solver ka APNA browser engine, scratch se.
//!
//! Design philosophy (agents-first, humans-second):
//! - Chromium 40M lines isliye hai kyunki wo HUMANS ke liye bana hai:
//!   pixels paint karna, tabs, extensions, GPU. AI agent ko sirf chahiye:
//!   DOM ka structured access + forms + links + text + fetch/submit.
//! - Wahi hum banayenge: chhota, auditable, ZERO-dependency Rust.
//! - Detection-layer: hamara TLS (next milestone), hamara parser (ye),
//!   hamara input. Koi third-party runtime fingerprint nahi.
//!
//! Milestone 1 (ye file): HTML tokenizer + DOM tree + CSS-selector-lite
//! + form/link extraction. Zero allocations waste, streaming parse.

use std::collections::BTreeMap;

// ---------------------------------------------------------------- tokens --
// (engine ke andar M1 wale core + naye layers: human render + anti-captcha)

pub mod adaptive;
pub mod drl;
pub mod anti;
pub mod form;
pub mod human;
pub mod js;
pub mod render;
pub mod net;
#[cfg(feature = "vault")]
pub mod vault;
#[cfg(feature = "crypt")]
pub mod crypt;

pub use anti::{CapTech, WallInfo};
pub use form::{
    api_key, FormData, FormSubmit, LoginFlow, LoginStep, Method, OAuthFlow, Request, Response,
};
pub use human::{render, GhostShell, resolve_url};
pub use net::{
    b64_encode, host_core, url_host, AuthHeader, Cookie, CookieJar, ProxyEndpoint, ProxyRoute,
    Resp, Session, SessionDoc, Transport,
};
#[cfg(feature = "vault")]
pub use vault::{Entry as VaultEntry, Vault, VaultError};

// ---------------------------------------------------------------- tokens --
#[derive(Debug, Clone, PartialEq)]
pub enum Tok {
    Text(String),
    OpenTag {
        name: String,
        attrs: Vec<(String, String)>,
        self_closing: bool,
    },
    CloseTag(String),
    Comment(String),
    Doctype(String),
}

/// HTML tokenizer — spec-lite (error-tolerant jaise browsers hote hain).
/// Vec<Tok> chunk me build karta hai; 50k-line pages bhi ok.
pub fn tokenize(src: &str) -> Vec<Tok> {
    let b = src.as_bytes();
    let n = b.len();
    let mut i = 0usize;
    let mut out = Vec::new();
    let mut text = String::new();

    macro_rules! flush_text {
        () => {
            if !text.is_empty() {
                out.push(Tok::Text(std::mem::take(&mut text)));
            }
        };
    }

    while i < n {
        if b[i] == b'<' {
            // comment / doctype / tag / raw text
            if src[i..].starts_with("<!--") {
                flush_text!();
                if let Some(end) = src[i + 4..].find("-->") {
                    out.push(Tok::Comment(src[i + 4..i + 4 + end].to_string()));
                    i += 4 + end + 3;
                } else {
                    out.push(Tok::Comment(src[i + 4..].to_string()));
                    i = n;
                }
                continue;
            }
            if src[i..].starts_with("<!") {
                flush_text!();
                if let Some(gt) = src[i..].find('>') {
                    out.push(Tok::Doctype(src[i + 2..i + gt].to_string()));
                    i += gt + 1;
                } else {
                    i = n;
                }
                continue;
            }
            if src[i..].starts_with("</") {
                flush_text!();
                if let Some(gt) = src[i..].find('>') {
                    out.push(Tok::CloseTag(
                        src[i + 2..i + gt].trim().to_ascii_lowercase(),
                    ));
                    i += gt + 1;
                } else {
                    i = n;
                }
                continue;
            }
            // open tag
            if i + 1 < n && (b[i + 1].is_ascii_alphabetic()) {
                flush_text!();
                let mut j = i + 1;
                while j < n && !b[j].is_ascii_whitespace() && b[j] != b'>' && b[j] != b'/' {
                    j += 1;
                }
                let name = src[i + 1..j].to_ascii_lowercase();
                let mut attrs: Vec<(String, String)> = Vec::new();
                let mut self_closing = false;
                // attribute scan
                let mut k = j;
                while k < n && b[k] != b'>' {
                    // skip ws
                    while k < n && b[k].is_ascii_whitespace() {
                        k += 1;
                    }
                    if k >= n || b[k] == b'>' {
                        break;
                    }
                    if b[k] == b'/' && k + 1 < n && b[k + 1] == b'>' {
                        self_closing = true;
                        // sirf '/' consume — closing-loop '>' pe khud
                        // rukega (k+=2 double-advance hota tha — dogfood)
                        k += 1;
                        break;
                    }
                    // attr name
                    let mut a = k;
                    while a < n
                        && !b[a].is_ascii_whitespace()
                        && b[a] != b'='
                        && b[a] != b'>'
                        && b[a] != b'/'
                    {
                        a += 1;
                    }
                    if a >= n {
                        break;
                    }
                    let an = src[k..a].to_ascii_lowercase();
                    let mut av = String::new();
                    k = a;
                    if k < n && b[k] == b'=' {
                        k += 1;
                        if k < n && (b[k] == b'"' || b[k] == b'\'') {
                            let q = b[k];
                            let qe = src[k + 1..].find(q as char).map(|e| k + 1 + e);
                            if let Some(e) = qe {
                                av = src[k + 1..e].to_string();
                                k = e + 1;
                            } else {
                                av = src[k + 1..].to_string();
                                k = n;
                            }
                        } else {
                            let mut v = k;
                            while v < n && !b[v].is_ascii_whitespace() && b[v] != b'>' {
                                v += 1;
                            }
                            av = src[k..v].to_string();
                            k = v;
                        }
                    }
                    if !an.is_empty() {
                        attrs.push((an, decode_entities(&av)));
                    }
                }
                // closing '>' consume — self_closing '/>' already
                // consumed (WARN: bina is check ke next tag ka takra
                // ho jata — dogfood self-test ne pakda tha)
                if !self_closing {
                    while k < n && b[k] != b'>' {
                        k += 1;
                    }
                }
                i = if k < n { k + 1 } else { n };
                out.push(Tok::OpenTag {
                    name,
                    attrs,
                    self_closing,
                });
                // void tags — self_closing treat
                continue;
            }
            // stray '<' — text me daal do (browsers aise hi karte hain)
            text.push('<');
            i += 1;
        } else {
            // raw-text elements ke liye char-by-char text
            let ch = src[i..].chars().next().unwrap();
            text.push(ch);
            i += ch.len_utf8();
        }
    }
    flush_text!();
    out
}

/// HTML entity decoder — common set (spec-pure nahi, practical).
pub fn decode_entities(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(p) = rest.find('&') {
        out.push_str(&rest[..p]);
        rest = &rest[p..];
        if let Some(semi) = rest.find(';').filter(|&e| e <= 12) {
            let ent = &rest[1..semi];
            let rep = match ent {
                "amp" => Some('&'),
                "lt" => Some('<'),
                "gt" => Some('>'),
                "quot" => Some('"'),
                "apos" => Some('\''),
                "nbsp" => Some(' '),
                _ => None,
            };
            if let Some(c) = rep {
                out.push(c);
                rest = &rest[semi + 1..];
                continue;
            }
            // numeric &#123; / &#x1F600;
            if let Some(num) = ent
                .strip_prefix('#')
                .or(ent.strip_prefix("#x"))
                .map(|e| u32::from_str_radix(e, 16).ok().and_then(char::from_u32))
            {
                if let Some(c) = num {
                    out.push(c);
                    rest = &rest[semi + 1..];
                    continue;
                }
            }
        }
        out.push('&');
        rest = &rest[1..];
    }
    out.push_str(rest);
    out
}

// ------------------------------------------------------------------- DOM --
#[derive(Debug, Clone)]
pub struct Node {
    pub tag: Option<String>, // None = text node
    pub attrs: BTreeMap<String, String>,
    pub text: String, // text nodes ke liye
    pub children: Vec<Node>,
}

impl Node {
    pub fn new_elem(tag: &str) -> Node {
        Node {
            tag: Some(tag.to_string()),
            attrs: BTreeMap::new(),
            text: String::new(),
            children: Vec::new(),
        }
    }
    pub fn new_text(t: &str) -> Node {
        Node {
            tag: None,
            attrs: BTreeMap::new(),
            text: t.to_string(),
            children: Vec::new(),
        }
    }
    pub fn attr(&self, k: &str) -> Option<&str> {
        self.attrs.get(k).map(|s| s.as_str())
    }
    /// direct text content (descendants bhi — innerText jaisa, ws-normalized)
    pub fn inner_text(&self) -> String {
        let mut buf = String::new();
        self._walk_text(&mut buf);
        // collapse whitespace
        buf.split_whitespace().collect::<Vec<_>>().join(" ")
    }
    fn _walk_text(&self, buf: &mut String) {
        if self.tag.is_none() {
            buf.push_str(&self.text);
            buf.push(' ');
        } else {
            for c in &self.children {
                c._walk_text(buf);
            }
        }
    }
}

/// DOM builder — tokenizer output -> tree. Void-elements handle,
/// implicit-close (p, li jaise) best-effort. Stack-based, single pass.
pub fn build_dom(toks: &[Tok]) -> Node {
    const VOID: &[&str] = &[
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
    ];
    let mut root = Node::new_elem("#root");
    let mut stack: Vec<Node> = vec![std::mem::replace(&mut root, Node::new_elem("#tmp"))];
    for t in toks {
        match t {
            Tok::Text(s) => {
                let clean = decode_entities(s);
                stack
                    .last_mut()
                    .unwrap()
                    .children
                    .push(Node::new_text(&clean));
            }
            Tok::OpenTag {
                name,
                attrs,
                self_closing,
            } => {
                let mut el = Node::new_elem(name);
                for (k, v) in attrs {
                    el.attrs.insert(k.clone(), v.clone());
                }
                if *self_closing || VOID.contains(&name.as_str()) {
                    stack.last_mut().unwrap().children.push(el);
                } else {
                    stack.push(el);
                }
            }
            Tok::CloseTag(name) => {
                // matching tag dhoondo stack me (unmatched ignore — browsers jaisa)
                if let Some(pos) = stack
                    .iter()
                    .rposition(|n| n.tag.as_deref() == Some(name.as_str()))
                {
                    if pos > 0 {
                        let mut tail = stack.split_off(pos);
                        let el = tail.remove(0);
                        stack.last_mut().unwrap().children.push(el);
                    }
                }
            }
            Tok::Comment(_) | Tok::Doctype(_) => {}
        }
    }
    // bacha hua stack root me merge
    while stack.len() > 1 {
        if let Some(el) = stack.pop() {
            stack.last_mut().unwrap().children.push(el);
        }
    }
    stack.pop().unwrap_or(Node::new_elem("#root"))
}

// -------------------------------------------------------------- selector --
/// CSS-selector-LITE: "tag", ".class", "#id", "tag.class#id" combos.
/// Descendant matching (space) bhi. Pseudo nth/attr next milestone.
#[derive(Debug, Clone)]
pub struct SelPart {
    pub tag: Option<String>,
    pub classes: Vec<String>,
    pub id: Option<String>,
}

pub fn parse_sel(s: &str) -> Vec<SelPart> {
    s.split_whitespace()
        .map(|part| {
            let mut p = SelPart {
                tag: None,
                classes: Vec::new(),
                id: None,
            };
            let mut rest = part;
            if let Some(h) = rest.find(|c| c == '.' || c == '#') {
                p.tag = Some(rest[..h].to_ascii_lowercase());
                rest = &rest[h..];
            } else {
                p.tag = Some(rest.to_ascii_lowercase());
                rest = "";
            }
            while !rest.is_empty() {
                if rest.starts_with('.') {
                    let e = rest[1..]
                        .find(|c| c == '.' || c == '#')
                        .map(|e| 1 + e)
                        .unwrap_or(rest.len());
                    p.classes.push(rest[1..e].to_string());
                    rest = &rest[e..];
                } else if rest.starts_with('#') {
                    let e = rest[1..]
                        .find(|c| c == '.' || c == '#')
                        .map(|e| 1 + e)
                        .unwrap_or(rest.len());
                    p.id = Some(rest[1..e].to_string());
                    rest = &rest[e..];
                } else {
                    break;
                }
            }
            p
        })
        .collect()
}

fn matches(n: &Node, p: &SelPart) -> bool {
    let tag = n.tag.as_deref().unwrap_or("");
    if let Some(t) = &p.tag {
        if tag != t.as_str() {
            return false;
        }
    }
    if let Some(id) = &p.id {
        if n.attr("id") != Some(id.as_str()) {
            return false;
        }
    }
    if !p.classes.is_empty() {
        let cls = n.attr("class").unwrap_or("");
        let set: Vec<&str> = cls.split_whitespace().collect();
        for c in &p.classes {
            if !set.contains(&c.as_str()) {
                return false;
            }
        }
    }
    true
}

/// query — descendant combinator ke saath. Returns matching nodes (borrowed paths
/// as indices ki jagah clone — engine chhota hai, page DOM <100k nodes).
pub fn query<'a>(root: &'a Node, sel: &str) -> Vec<&'a Node> {
    let parts = parse_sel(sel);
    let mut out = Vec::new();
    fn walk<'a>(n: &'a Node, parts: &[SelPart], idx: usize, out: &mut Vec<&'a Node>) {
        for c in &n.children {
            if matches(c, &parts[idx]) {
                if idx + 1 == parts.len() {
                    out.push(c);
                } else {
                    walk(c, parts, idx + 1, out);
                }
            }
            // har child ke subtree me bhi selector ki pehli part dhoondo
            walk(c, parts, 0, out);
        }
    }
    if parts.is_empty() {
        return out;
    }
    walk(root, &parts, 0, &mut out);
    out
}

// ---------------------------------------------------------- page surface --
/// Form + link extraction — agent navigation ka core surface.
pub struct Page {
    pub root: Node,
}

impl Page {
    pub fn parse(html: &str) -> Page {
        Page {
            root: build_dom(&tokenize(html)),
        }
    }

    pub fn select(&self, sel: &str) -> Vec<&Node> {
        query(&self.root, sel)
    }

    pub fn title(&self) -> String {
        self.select("title")
            .first()
            .map(|t| t.inner_text())
            .unwrap_or_default()
    }

    pub fn text(&self) -> String {
        self.root.inner_text()
    }

    /// Forms: (action, method, [(name, type, value), ...])
    pub fn forms(&self) -> Vec<(String, String, Vec<(String, String, String)>)> {
        let mut out = Vec::new();
        for f in self.select("form") {
            let action = f.attr("action").unwrap_or("").to_string();
            let method = f.attr("method").unwrap_or("GET").to_ascii_uppercase();
            let mut fields = Vec::new();
            for inp in f.children.iter().flat_map(|c| {
                let mut v = Vec::new();
                fn collect_inputs<'a>(n: &'a Node, v: &mut Vec<&'a Node>) {
                    if n.tag.as_deref() == Some("input")
                        || n.tag.as_deref() == Some("textarea")
                        || n.tag.as_deref() == Some("select")
                    {
                        v.push(n);
                    }
                    for c in &n.children {
                        collect_inputs(c, v);
                    }
                }
                collect_inputs(c, &mut v);
                v
            }) {
                fields.push((
                    inp.attr("name").unwrap_or("").to_string(),
                    inp.attr("type").unwrap_or("text").to_string(),
                    inp.attr("value").unwrap_or("").to_string(),
                ));
            }
            out.push((action, method, fields));
        }
        out
    }

    /// Links: (href, text)
    pub fn links(&self) -> Vec<(String, String)> {
        self.select("a")
            .iter()
            .filter_map(|a| {
                let href = a.attr("href")?;
                Some((href.to_string(), a.inner_text()))
            })
            .collect()
    }

    /// Meta: (name/property, content)
    pub fn meta(&self) -> Vec<(String, String)> {
        self.select("meta")
            .iter()
            .filter_map(|m| {
                let k = m.attr("name").or_else(|| m.attr("property"))?.to_string();
                let v = m.attr("content").unwrap_or("").to_string();
                Some((k, v))
            })
            .collect()
    }
}

// ------------------------------------------------------------------ tests --
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selfclose_then_form_regression() {
        // dogfood RE self-test ne pakda tha: <meta/> ke baad form swallow
        let html = "<meta name=\"a\" content=\"b\"/><form action=\"/login\"><input name=\"email\"/></form>";
        let p = Page::parse(html);
        assert_eq!(p.forms().len(), 1, "form swallowed after self-closing tag");
        let html2 = "<meta a=\"1\"/><form action=\"/x\"><input name=\"q\"/></form>";
        let p2 = Page::parse(html2);
        assert_eq!(p2.forms().len(), 1);
    }

    #[test]
    fn tokenize_basic() {
        let t = tokenize("<p class='x'>Hi &amp; bye</p>");
        assert!(matches!(t[0], Tok::OpenTag { ref name, .. } if name == "p"));
        // entities DOM-stage pe decode hoti hain; tokenizer raw text deta hai
        assert!(matches!(t[1], Tok::Text(ref s) if s == "Hi &amp; bye"));
        let dom = build_dom(&t);
        assert_eq!(dom.inner_text(), "Hi & bye");
    }

    #[test]
    fn dom_tree_and_text() {
        let p = Page::parse("<div><p>hello <b>world</b></p></div>");
        assert_eq!(p.text(), "hello world");
        assert_eq!(p.select("b").len(), 1);
    }

    #[test]
    fn forms_links_meta() {
        let html = "<title>T</title>\
            <form action='/login' method='post'><input name='email' type='email' value=''></form>\
            <a href='/x'>Next</a>\
            <meta name='desc' content='hi'>";
        let p = Page::parse(html);
        assert_eq!(p.title(), "T");
        let (a, m, f) = &p.forms()[0];
        assert_eq!((a.as_str(), m.as_str()), ("/login", "POST"));
        assert_eq!(f[0].0, "email");
        assert_eq!(p.links()[0].0, "/x");
        assert_eq!(p.meta()[0], ("desc".into(), "hi".into()));
    }

    #[test]
    fn malformed_tolerant() {
        // unclosed tags, stray '<', entities — sab tolerate
        let p = Page::parse("<div><p>unclosed <img src='x.png'><b>bold");
        assert!(p.text().contains("unclosed"));
        assert_eq!(p.select("img").len(), 1);
    }
}
