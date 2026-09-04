//! GhostEngine render pipeline — CSS-lite block layout + software raster + PNG.
//!
//! Milestone: pixel render (scratch se, std-only). Chrome nahi — sirf itna:
//!   Page (DOM) -> style-lite (inline style attr parse) -> block layout
//!   (y-cursor flow) -> word-wrap inline text -> RGB framebuffer
//!   -> PNG bytes (uncompressed deflate, hand-rolled CRC32).
//!
//! Kyun? Kyunki agent-first engine me bhi kabhi-kabhiPixels chahiye —
//! screenshot-diff, captcha image me text locate, ya GUI shell. Zero dep
//! wada intact: koi image-buffer crate nahi, koi zlib crate nahi.

use crate::Page;
use std::collections::BTreeMap;

// ---------------------------------------------------------------- style --

/// Hex color: #RGB / #RRGGBB (CSS-lite). Black on parse fail (browser default
/// jaisa — fail-loud nahi, fail-safe).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Color(pub u8, pub u8, pub u8);

impl Color {
    pub const BLACK: Color = Color(0, 0, 0);
    pub const WHITE: Color = Color(255, 255, 255);

    /// "#ff0000" / "#f00" / "ff0000" -> Color. Invalid = None.
    pub fn parse(s: &str) -> Option<Color> {
        let h = s.strip_prefix('#').unwrap_or(s);
        match h.len() {
            6 => Some(Color(
                u8::from_str_radix(&h[0..2], 16).ok()?,
                u8::from_str_radix(&h[2..4], 16).ok()?,
                u8::from_str_radix(&h[4..6], 16).ok()?,
            )),
            3 => Some(Color(
                u8::from_str_radix(&h[0..1], 16).ok()? * 17,
                u8::from_str_radix(&h[1..2], 16).ok()? * 17,
                u8::from_str_radix(&h[2..3], 16).ok()? * 17,
            )),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TextAlign {
    Left,
    Center,
    Right,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Display {
    Block,
    Inline,
}

/// Per-element computed style — inline `style="..."` attr se, plus tag-based
/// defaults (h1 bada font, p margin, etc). CSS classes nahi (M-later).
#[derive(Debug, Clone)]
pub struct Style {
    pub display: Display,
    pub margin: u32,
    pub padding: u32,
    pub border_width: u32,
    pub color: Color,
    pub background: Color,
    pub font_size: u32,
    pub text_align: TextAlign,
}

impl Default for Style {
    fn default() -> Self {
        Style {
            display: Display::Block,
            margin: 0,
            padding: 0,
            border_width: 0,
            color: Color::BLACK,
            background: Color::WHITE,
            font_size: 12,
            text_align: TextAlign::Left,
        }
    }
}

// kaam ke tag-defaults — real CSS reset-lite
fn tag_style(tag: &str) -> Style {
    let mut s = Style::default();
    match tag {
        "h1" => {
            s.font_size = 24;
            s.margin = 8;
        }
        "h2" => {
            s.font_size = 20;
            s.margin = 6;
        }
        "h3" => {
            s.font_size = 16;
            s.margin = 4;
        }
        "p" | "div" | "section" | "article" => {
            s.margin = 4;
        }
        "b" | "strong" | "i" | "em" | "span" | "a" | "code" => {
            s.display = Display::Inline;
        }
        _ => {}
    }
    s
}

/// inline style attr -> Style overrides. "margin:4px; color:#333" format.
pub fn parse_style_attr(attr: &str, base: Style) -> Style {
    let mut s = base;
    for decl in attr.split(';') {
        let Some((k, v)) = decl.split_once(':') else { continue };
        let k = k.trim().to_ascii_lowercase();
        let v = v.trim();
        match k.as_str() {
            "display" => match v {
                "inline" => s.display = Display::Inline,
                "block" => s.display = Display::Block,
                _ => {}
            },
            "margin" => {
                if let Some(px) = px_val(v) {
                    s.margin = px
                }
            }
            "padding" => {
                if let Some(px) = px_val(v) {
                    s.padding = px
                }
            }
            "border-width" | "border" => {
                if let Some(px) = px_val(v) {
                    s.border_width = px
                }
            }
            "color" => {
                if let Some(c) = Color::parse(v) {
                    s.color = c
                }
            }
            "background" | "background-color" => {
                if let Some(c) = Color::parse(v) {
                    s.background = c
                }
            }
            "font-size" => {
                if let Some(px) = px_val(v) {
                    s.font_size = px
                }
            }
            "text-align" => match v {
                "center" => s.text_align = TextAlign::Center,
                "right" => s.text_align = TextAlign::Right,
                "left" => s.text_align = TextAlign::Left,
                _ => {}
            },
            _ => {}
        }
    }
    s
}

fn px_val(v: &str) -> Option<u32> {
    let n = v.strip_suffix("px").unwrap_or(v);
    n.trim().parse::<u32>().ok().filter(|&x| x <= 64)
}

/// Node -> computed Style (tag defaults + inline attr).
pub fn style_of(node: &crate::Node) -> Style {
    let base = match node.tag.as_deref() {
        Some(t) => tag_style(t),
        None => {
            let mut s = Style::default();
            s.display = Display::Inline; // text nodes inline hote hain
            s
        }
    };
    match node.attr("style") {
        Some(a) => parse_style_attr(a, base),
        None => base,
    }
}

// ---------------------------------------------------------------- layout --

/// Laid-out box: geometry + text runs. x/y/w/h absolute page coords.
#[derive(Debug, Clone)]
pub struct Box {
    pub x: i32,
    pub y: i32,
    pub w: u32,
    h: u32,
    pub style: Style,
    /// wrapped lines of text content (empty for pure containers)
    pub lines: Vec<String>,
    pub tag: Option<String>,
    pub children: Vec<Box>,
}

/// Word-wrap: text -> lines jo `max_chars` me fit ho. Monospace 8px/char
/// estimate (glyph advance = font_width(font_size)).
pub fn wrap_text(text: &str, max_chars: usize) -> Vec<String> {
    let mut lines = Vec::new();
    if max_chars == 0 {
        return lines;
    }
    let mut cur = String::new();
    for word in text.split_whitespace() {
        let need = cur.len() + if cur.is_empty() { 0 } else { 1 } + word.len();
        if !cur.is_empty() && need > max_chars {
            lines.push(std::mem::take(&mut cur));
        }
        // single word khud line se lamba? hard-break nahi — rakh lo, clip ho jayega
        if cur.is_empty() {
            cur.push_str(word);
        } else {
            cur.push(' ');
            cur.push_str(word);
        }
    }
    if !cur.is_empty() {
        lines.push(cur);
    }
    if lines.is_empty() && !text.trim().is_empty() {
        lines.push(text.trim().to_string());
    }
    lines
}

/// Glyph advance: 8px-per-char at 12px font, scale linearly.
pub fn font_width(font_size: u32) -> usize {
    ((font_size as f64 / 12.0) * 8.0).round() as usize
}

/// Layout: DOM -> Box tree. y-cursor block flow, nested children, width
/// pass-down. Text nodes ek pseudo-inline box banate hain (parent ke andar).
pub fn layout(node: &crate::Node, page_width: u32) -> Box {
    let style = style_of(node);
    let avail = page_width.saturating_sub(2 * (style.margin + style.border_width + style.padding));
    let mut b = Box {
        x: 0,
        y: 0,
        w: avail,
        h: 0,
        style: style.clone(),
        lines: Vec::new(),
        tag: node.tag.clone(),
        children: Vec::new(),
    };
    let mut y: i32 = 0; // content-area local cursor
    let cw = font_width(style.font_size).max(1);

    // children: block-style stacking; inline runs merge into shared lines
    let mut inline_buf = String::new();
    let mut inline_styles: Vec<Style> = Vec::new();

    for c in &node.children {
        let cs = style_of(c);
        match cs.display {
            Display::Block => {
                // flush pending inline text first
                if !inline_buf.is_empty() {
                    let text = std::mem::take(&mut inline_buf);
                    inline_styles.clear();
                    let lines = wrap_text(&text, cw.max(1) * (b.w as usize / cw).max(1));
                    let mut lines = lines; // own
                    if lines.is_empty() {
                        lines.push(String::new());
                    }
                    let lh = (style.font_size + 2) as i32;
                    let h = (lines.len() as i32 * lh).max(1);
                    b.children.push(Box {
                        x: 0,
                        y,
                        w: b.w,
                        h: h as u32,
                        style: style.clone(),
                        lines,
                        tag: None,
                        children: vec![],
                    });
                    y += h;
                }
                let mut child = layout(c, avail);
                child.x = 0; // block: left edge
                child.y = y + c_margin(&cs);
                y += child.h as i32 + c_margin(&cs);
                b.children.push(child);
            }
            Display::Inline => {
                let text = c.inner_text();
                if !text.is_empty() {
                    inline_buf.push_str(&text);
                    inline_buf.push(' ');
                }
                inline_styles.push(cs);
            }
        }
    }
    // final inline flush
    if !inline_buf.is_empty() {
        let text = std::mem::take(&mut inline_buf);
        let max_chars = ((b.w as usize) / cw).max(1);
        let lines = wrap_text(&text, max_chars);
        let lh = (style.font_size + 2) as i32;
        let h = (lines.len() as i32 * lh).max(1);
        b.children.push(Box {
            x: 0,
            y,
            w: b.w,
            h: h as u32,
            style: style.clone(),
            lines,
            tag: None,
            children: vec![],
        });
        y += h;
    }
    b.h = y as u32 + style.padding;
    b
}

fn c_margin(s: &Style) -> i32 {
    (s.margin + s.border_width) as i32
}

/// Page-level convenience: root children ko stack karo, page box ke andar.
pub fn layout_page(page: &Page, width: u32) -> Box {
    let mut root = layout(&page.root, width);
    // root ka apna content bhi render ho — text walk
    root
}

// ======================================================== PNG writer --
/// PNG encoder, std-only: uncompressed deflate (stored blocks) —
/// zlib header 0x78 0x01, IDAT raw rows (filter-byte 0 + RGB triplets).
pub fn crc32(data: &[u8]) -> u32 {
    let mut table = [0u32; 256];
    for (i, t) in table.iter_mut().enumerate() {
        let mut c = i as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB88320 ^ (c >> 1) } else { c >> 1 };
        }
        *t = c;
    }
    let mut crc = 0xFFFF_FFFFu32;
    for b in data {
        crc = table[((crc ^ (*b as u32)) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc ^ 0xFFFF_FFFF
}

fn chunk(tag: &[u8; 4], data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(tag);
    out.extend_from_slice(data);
    let mut crc_input = Vec::new();
    crc_input.extend_from_slice(tag);
    crc_input.extend_from_slice(data);
    out.extend_from_slice(&crc32(&crc_input).to_be_bytes());
    out
}

/// RGB framebuffer -> PNG bytes (8-bit, truecolor).
pub fn encode_png(width: u32, height: u32, rgb: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(&[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);
    // IHDR
    let mut ihdr = Vec::new();
    ihdr.extend_from_slice(&width.to_be_bytes());
    ihdr.extend_from_slice(&height.to_be_bytes());
    ihdr.extend_from_slice(&[8, 2, 0, 0, 0]); // depth 8, color RGB
    out.extend_from_slice(&chunk(b"IHDR", &ihdr));
    // IDAT — stored deflate blocks: har row: filter 0 + w*3 bytes
    let stride = (width as usize) * 3;
    let mut raw = Vec::with_capacity((stride + 1) * height as usize);
    for y in 0..height as usize {
        raw.push(0u8);
        raw.extend_from_slice(&rgb[y * stride..(y + 1) * stride]);
    }
    // zlib wrapper + stored blocks (max 65535 per block)
    let mut idat = vec![0x78u8, 0x01];
    let mut pos = 0usize;
    while pos < raw.len() {
        let take = (raw.len() - pos).min(65535);
        let last = if pos + take >= raw.len() { 1u8 } else { 0u8 };
        idat.push(last);
        idat.extend_from_slice(&(take as u16).to_le_bytes());
        // NLEN = one's complement of LEN (stored-block spec — PIL isko
        // validate karta hai, missing thi)
        idat.extend_from_slice(&(!(take as u16)).to_le_bytes());
        idat.extend_from_slice(&raw[pos..pos + take]);
        pos += take;
    }
    // zlib trailer: adler32 of raw data (BIG-endian!)
    let mut a = 1u32; let mut b = 0u32;
    for byte in &raw {
        a = (a + *byte as u32) % 65521;
        b = (b + a) % 65521;
    }
    let adler = (b << 16) | a;
    idat.extend_from_slice(&adler.to_be_bytes());
    out.extend_from_slice(&chunk(b"IDAT", &idat));
    out.extend_from_slice(&chunk(b"IEND", &[]));
    out
}

// ======================================================== raster ops --
/// Simple framebuffer — RGB bytes, w*h*3.
pub struct Canvas {
    pub w: u32,
    pub h: u32,
    pub buf: Vec<u8>,
}

impl Canvas {
    pub fn new(w: u32, h: u32, bg: Color) -> Canvas {
        let mut buf = Vec::with_capacity((w * h * 3) as usize);
        for _ in 0..(w * h) {
            buf.extend_from_slice(&[bg.0, bg.1, bg.2]);
        }
        Canvas { w, h, buf }
    }
    pub fn fill_rect(&mut self, x: i32, y: i32, w: u32, h: u32, c: Color) {
        for yy in y.max(0)..((y + h as i32).min(self.h as i32)) {
            for xx in x.max(0)..((x + w as i32).min(self.w as i32)) {
                let i = ((yy as u32 * self.w + xx as u32) * 3) as usize;
                if i + 2 < self.buf.len() {
                    self.buf[i] = c.0;
                    self.buf[i + 1] = c.1;
                    self.buf[i + 2] = c.2;
                }
            }
        }
    }
    /// 3x5 mini font — readable at small scale (hand-rolled glyphs).
    pub fn draw_text(&mut self, x: i32, y: i32, s: &str, c: Color) {
        let mut cx = x;
        for ch in s.chars() {
            let g = glyph3x5(ch);
            for (gy, row) in g.iter().enumerate() {
                for gx in 0..3 {
                    if row & (1 << (2 - gx)) != 0 {
                        self.fill_rect(cx + gx as i32, y + gy as i32, 1, 1, c);
                    }
                }
            }
            cx += 4;
        }
    }
    pub fn png_bytes(&self) -> Vec<u8> {
        encode_png(self.w, self.h, &self.buf)
    }
    pub fn save(&self, path: &str) -> std::io::Result<()> {
        std::fs::write(path, self.png_bytes())
    }
}

/// 3x5 glyphs (bit-rows) — A-z, digits, basic punct.
fn glyph3x5(c: char) -> [u8; 5] {
    match c.to_ascii_uppercase() {
        'A' => [0b010, 0b101, 0b111, 0b101, 0b101],
        'B' => [0b110, 0b101, 0b110, 0b101, 0b110],
        'C' => [0b011, 0b100, 0b100, 0b100, 0b011],
        'D' => [0b110, 0b101, 0b101, 0b101, 0b110],
        'E' => [0b111, 0b100, 0b110, 0b100, 0b111],
        'F' => [0b111, 0b100, 0b110, 0b100, 0b100],
        'G' => [0b011, 0b100, 0b101, 0b101, 0b011],
        'H' => [0b101, 0b101, 0b111, 0b101, 0b101],
        'I' => [0b111, 0b010, 0b010, 0b010, 0b111],
        'J' => [0b001, 0b001, 0b001, 0b101, 0b010],
        'K' => [0b101, 0b101, 0b110, 0b101, 0b101],
        'L' => [0b100, 0b100, 0b100, 0b100, 0b111],
        'M' => [0b101, 0b111, 0b111, 0b101, 0b101],
        'N' => [0b110, 0b101, 0b101, 0b101, 0b101],
        'O' => [0b010, 0b101, 0b101, 0b101, 0b010],
        'P' => [0b110, 0b101, 0b110, 0b100, 0b100],
        'Q' => [0b010, 0b101, 0b101, 0b111, 0b011],
        'R' => [0b110, 0b101, 0b110, 0b101, 0b101],
        'S' => [0b011, 0b100, 0b010, 0b001, 0b110],
        'T' => [0b111, 0b010, 0b010, 0b010, 0b010],
        'U' => [0b101, 0b101, 0b101, 0b101, 0b111],
        'V' => [0b101, 0b101, 0b101, 0b101, 0b010],
        'W' => [0b101, 0b101, 0b111, 0b111, 0b101],
        'X' => [0b101, 0b101, 0b010, 0b101, 0b101],
        'Y' => [0b101, 0b101, 0b010, 0b010, 0b010],
        'Z' => [0b111, 0b001, 0b010, 0b100, 0b111],
        '0' => [0b111, 0b101, 0b101, 0b101, 0b111],
        '1' => [0b010, 0b110, 0b010, 0b010, 0b111],
        '2' => [0b111, 0b001, 0b111, 0b100, 0b111],
        '3' => [0b111, 0b001, 0b011, 0b001, 0b111],
        '4' => [0b101, 0b101, 0b111, 0b001, 0b001],
        '5' => [0b111, 0b100, 0b111, 0b001, 0b111],
        '6' => [0b111, 0b100, 0b111, 0b101, 0b111],
        '7' => [0b111, 0b001, 0b010, 0b010, 0b010],
        '8' => [0b111, 0b101, 0b111, 0b101, 0b111],
        '9' => [0b111, 0b101, 0b111, 0b001, 0b111],
        ' ' => [0; 5],
        '.' => [0, 0, 0, 0, 0b010],
        '-' => [0, 0, 0b111, 0, 0],
        ':' => [0, 0b010, 0, 0b010, 0],
        '!' => [0b010, 0b010, 0b010, 0, 0b010],
        '/' => [0b001, 0b001, 0b010, 0b100, 0b100],
        _ => [0b111, 0b101, 0b111, 0b101, 0b111], // fallback box
    }
}

/// Page -> PNG pipeline (layout + raster + encode).
pub fn render_page_png(page: &crate::Page, width: u32) -> Vec<u8> {
    let root = layout_page(page, width);
    let bg = Color(0xFF, 0xFF, 0xFF);
    let fg = Color(0x10, 0x10, 0x10);
    let mut canvas = Canvas::new(width, root.h.max(60), bg);
    draw_box_tree(&root, &mut canvas, 0, 0, fg);
    canvas.png_bytes()
}

fn draw_box_tree(b: &Box, c: &mut Canvas, ox: i32, oy: i32, fg: Color) {
    let x = ox + b.x;
    let y = oy + b.y;
    // bg hamesha Color hai (subagent Style) — white nahi hai to fill
    if b.style.background != Color::WHITE {
        c.fill_rect(x, y, b.w, b.h, b.style.background);
    }
    let ty = y + b.style.padding as i32;
    let mut line_y = ty;
    for line in &b.lines {
        c.draw_text(x + b.style.padding as i32, line_y, line, fg);
        line_y += (b.style.font_size + 2) as i32;
    }
    for ch in &b.children {
        draw_box_tree(ch, c, x, y, fg);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn png_magic_and_ihdr() {
        let c = Canvas::new(4, 2, Color(255, 0, 0));
        let png = c.png_bytes();
        assert_eq!(&png[..8], &[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]);
        assert_eq!(&png[16..20], &4u32.to_be_bytes());
        assert_eq!(&png[20..24], &2u32.to_be_bytes());
    }

    #[test]
    fn crc32_known() {
        // CRC32("123456789") = 0xCBF43926
        assert_eq!(crc32(b"123456789"), 0xCBF43926);
    }

    #[test]
    fn fill_and_pixel() {
        let mut c = Canvas::new(3, 3, Color(0, 0, 0));
        c.fill_rect(0, 0, 3, 3, Color(255, 255, 255));
        assert_eq!(c.buf[0], 255);
        c.fill_rect(1, 1, 1, 1, Color(10, 20, 30));
        let i = ((1 * 3) + 1) * 3;
        assert_eq!(c.buf[i], 10);
    }

    #[test]
    fn render_pipeline_smoke() {
        let html = "<h1>Hello Ghost</h1><p>Render works fine.</p>";
        let page = crate::Page::parse(html);
        let png = render_page_png(&page, 240);
        assert!(!png.is_empty());
        assert!(png.len() > 100);
        assert_eq!(&png[..4], &[0x89, b'P', b'N', b'G']);
    }

    #[test]
    fn color_parse() {
        assert_eq!(Color::parse("#ff8800"), Some(Color(255, 136, 0)));
        // note: "bad" valid 3-hex chars hain (b,a,d) — parse hota hai;
        // truly-invalid: "zz9" (z hex nahi)
        assert_eq!(Color::parse("zz9"), None);
    }
}
