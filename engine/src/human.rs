//! GhostEngine human layer — terminal browse mode (Lynx/w3m style).
//!
//! Humans ke liye: page ko READABLE layout me render karta hai —
//! headings, links, forms, paragraphs. GUI pixels M6+ ka kaam hai;
//! abhi har page text-mode me poora usable hai (read + navigate +
//! form dekh ke submit). GhostMouse get se fetch -> render.

use crate::{Node, Page};

/// Text-mode render — human-readable layout.
/// Links [1], [2]... numbered (navigation map neeche), forms boxed.
pub fn render(page: &Page, width: usize) -> String {
    let mut out = String::new();
    let width = width.max(40);
    let mut links: Vec<(String, String)> = Vec::new();

    out.push_str(&format!("{}\n", "─".repeat(width.min(72))));
    let title = page.title();
    if !title.is_empty() {
        out.push_str(&format!("  {} {}\n", "▣".repeat(1), title));
        out.push_str(&format!("{}\n", "─".repeat(width.min(72))));
    }
    render_node(&page.root, 0, width, &mut out, &mut links, false);

    if !links.is_empty() {
        out.push_str(&format!("\n{}\n LINKS\n", "═".repeat(width.min(72))));
        for (i, (href, text)) in links.iter().enumerate() {
            let label = if text.is_empty() { href.clone() } else { text.clone() };
            out.push_str(&format!("  [{:>2}] {} \n       → {}\n", i + 1, truncate(&label, 60), truncate(href, width.saturating_sub(8))));
        }
    }
    let forms = page.forms();
    if !forms.is_empty() {
        out.push_str(&format!("\n{}\n FORMS\n", "═".repeat(width.min(72))));
        for (action, method, fields) in &forms {
            out.push_str(&format!("  ▸ {} {} ({})\n", method, action, fields.len()));
            for (name, ty, val) in fields {
                out.push_str(&format!("      · {}: {} = {}\n", name, ty, truncate(val, 24)));
            }
        }
    }
    out
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        s.to_string()
    } else {
        let t: String = s.chars().take(max.saturating_sub(1)).collect();
        format!("{}…", t)
    }
}

const HEADINGS: &[&str] = &["h1", "h2", "h3", "h4", "h5", "h6"];
const SKIP: &[&str] = &["script", "style", "noscript", "svg", "head"];

fn render_node(n: &Node, depth: usize, width: usize, out: &mut String,
               links: &mut Vec<(String, String)>, in_link: bool) {
    let tag = n.tag.as_deref().unwrap_or("");
    if SKIP.contains(&tag) {
        return;
    }
    if n.tag.is_none() {
        let t = n.text.trim();
        if !t.is_empty() && !in_link {
            // body text — wrap indent
            let indent = "  ".repeat(depth.min(3));
            for line in t.split('.').filter(|s| !s.trim().is_empty()) {
                let l = line.trim().trim_end_matches('.');
                if !l.is_empty() {
                    out.push_str(&format!("{}{}.\n", indent, wrap(l, width, depth.min(3))));
                }
            }
        }
        return;
    }
    if tag == "a" {
        let href = n.attr("href").unwrap_or("").to_string();
        let text = n.inner_text();
        let idx = links.len() + 1;
        if !href.is_empty() && !href.starts_with('#') {
            links.push((href, text.clone()));
            out.push_str(&format!("  [{}] {} ", idx, if text.is_empty() { "•".into() } else { text }));
        } else if !text.is_empty() {
            out.push_str(&format!("  {} ", text));
        }
        // link ke andar ka text flag ke saath chalao (upar print ho gaya)
        return;
    }
    if HEADINGS.contains(&tag) {
        out.push_str(&format!("\n{} {} {}\n", "  ".repeat(depth.min(2)),
                    "◈".repeat(7 - tag[1..].parse::<usize>().unwrap_or(1).min(6)),
                    n.inner_text()));
        return;
    }
    if tag == "p" || tag == "li" || tag == "blockquote" {
        let prefix = if tag == "li" { "  – " } else { "  " };
        let t = n.inner_text();
        if !t.is_empty() {
            out.push_str(&format!("{}{}\n", prefix, wrap(&t, width, 1)));
        }
        return;
    }
    if tag == "hr" {
        out.push_str(&format!("  {}\n", "·".repeat(40)));
        return;
    }
    if tag == "br" {
        out.push('\n');
        return;
    }
    if tag == "img" {
        let alt = n.attr("alt").unwrap_or("");
        let src = n.attr("src").unwrap_or("");
        out.push_str(&format!("  ▭ [img: {}] {}\n", alt, truncate(src, 40)));
        return;
    }
    if tag == "button" {
        let t = n.inner_text();
        if !t.is_empty() {
            out.push_str(&format!("  ⬡ [btn: {}]\n", t));
        }
        return;
    }
    for c in &n.children {
        render_node(c, depth + 1, width, out, links, false);
    }
}

fn wrap(s: &str, width: usize, indent: usize) -> String {
    let avail = width.saturating_sub(4 + indent * 2).max(20);
    let mut out = String::new();
    let mut line = String::new();
    for w in s.split_whitespace() {
        if line.chars().count() + w.chars().count() + 1 > avail {
            out.push_str(&line);
            out.push('\n');
            out.push_str(&" ".repeat(4 + indent * 2));
            line.clear();
        }
        if !line.is_empty() {
            line.push(' ');
        }
        line.push_str(w);
    }
    out.push_str(&line);
    out
}

/// Terminal interactive browse — arrow-free, number-driven.
/// "GhostShell": enter link number to navigate, q to quit.
/// (examples/human_browse.rs isse use karta hai)
pub struct GhostShell {
    pub history: Vec<String>,
    pub current: Option<Page>,
}

impl GhostShell {
    pub fn new() -> Self {
        GhostShell { history: vec![], current: None }
    }
    pub fn show(&mut self, page: Page, url: &str) -> String {
        self.history.push(url.to_string());
        self.current = Some(page);
        let p = self.current.as_ref().unwrap();
        render(p, 80)
    }
    /// Link number -> absolute URL (base resolve).
    pub fn link_url(&self, n: usize) -> Option<String> {
        let p = self.current.as_ref()?;
        let links = p.links();
        let (href, _) = links.get(n.checked_sub(1)?)?;
        let base = self.history.last()?;
        Some(resolve_url(base, href))
    }
}

/// Minimal URL resolver — absolute/relative dono.
pub fn resolve_url(base: &str, href: &str) -> String {
    if href.starts_with("http://") || href.starts_with("https://") {
        return href.to_string();
    }
    // scheme extract
    let (scheme, rest) = if let Some(r) = base.strip_prefix("https://") {
        ("https://", r)
    } else if let Some(r) = base.strip_prefix("http://") {
        ("http://", r)
    } else {
        return href.to_string();
    };
    let (host, _path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    if href.starts_with('/') {
        return format!("{}{}{}", scheme, host, href);
    }
    if href.starts_with("..") || href.starts_with('.') {
        // parent-dir resolve — best effort: host root
        return format!("{}{}/{}", scheme, host, href.trim_start_matches("./"));
    }
    format!("{}{}/{}", scheme, host, href)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Page;

    #[test]
    fn render_readable() {
        let html = "<title>News</title><h1>Big Story</h1>\
                    <p>Government announced policy. Details follow soon.</p>\
                    <a href='/a1'>Read more</a><a href='https://x.com/e'>External</a>";
        let p = Page::parse(html);
        let r = render(&p, 80);
        assert!(r.contains("Big Story"));
        assert!(r.contains("policy."));
        assert!(r.contains("[1] Read more"));
        assert!(r.contains("→ https://x.com/e"));
        assert!(r.contains("LINKS"));
    }

    #[test]
    fn shell_nav_resolve() {
        let mut sh = GhostShell::new();
        let html = "<a href='/next'>Next page</a>";
        let out = sh.show(Page::parse(html), "https://example.com/news/today");
        assert!(out.contains("Next page"));
        assert_eq!(sh.link_url(1).unwrap(), "https://example.com/next");
        assert_eq!(resolve_url("https://a.com/x/y", "https://b.com/z"), "https://b.com/z");
    }
}
