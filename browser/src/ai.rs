//! Comet — AI assistant integration (Qwen / DeepSeek / any OpenAI-compatible).
//!
//! Lets the browser call a chat model for browser tasks, PDF editing, click
//! suggestions, etc. Endpoint/model/key are read from env so it works with any
//! of LO's providers (Qwen/DeepSeek/Claude via prexzy/tokenrouter/subscription).
//! Real HTTPS call via ureq. Parses the JSON response and returns the text.
//!
//! Env:
//!   COMET_LLM_BASE   e.g. https://api.aisubscription.shop/v1  (default)
//!   COMET_LLM_KEY    bearer key
//!   COMET_LLM_MODEL  model id (default deepseek-v4-flash; set to qwen3.8-max etc.)

use std::io::Read;

fn base() -> String {
    std::env::var("COMET_LLM_BASE").unwrap_or_else(|_| "https://api.aisubscription.shop/v1".into())
}

/// Escape a string for safe embedding inside a JSON string literal.
fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}
fn key() -> String {
    std::env::var("COMET_LLM_KEY").unwrap_or_else(|_| std::env::var("AISK_API_KEY").unwrap_or_default())
}
pub fn model() -> String {
    std::env::var("COMET_LLM_MODEL").unwrap_or_else(|_| "deepseek-v4-flash".into())
}

/// True if a provider key is configured (for the UI status endpoint).
pub fn key_present() -> bool {
    let direct = std::env::var("COMET_LLM_KEY").unwrap_or_default();
    let fallback = std::env::var("AISK_API_KEY").unwrap_or_default();
    !direct.is_empty() || !fallback.is_empty()
}

/// Build an OpenAI-compatible chat completion JSON body (with optional image).
pub fn build_body(msgs: &[(&str, &str)], image_b64: Option<&str>, max_tokens: u32) -> String {
    let mut parts = Vec::new();
    for (role, content) in msgs {
        let c = escape_json(content);
        parts.push(format!(r#"{{"role":"{role}","content":"{c}"}}"#));
    }
    // If an image is given, append it as an image content part to the last (user) message.
    if let Some(b64) = image_b64 {
        let last = parts.pop().unwrap_or_default();
        // replace last user message to include image_url data
        parts.push(format!(
            r#"{{"role":"user","content":[{{"type":"text","text":"Describe/answer about this image"}},{{"type":"image_url","image_url":{{"url":"data:image/png;base64,{b64}"}}}}]}}"#
        ));
        let _ = last;
    }
    let joined = parts.join(",");
    format!(
        r#"{{"model":"{}","messages":[{}],"max_tokens":{}}}"#,
        model(),
        joined,
        max_tokens
    )
}

/// Send a chat completion and return the assistant text. REAL HTTPS call.
pub fn chat(msgs: &[(&str, &str)], image_b64: Option<&str>) -> Result<String, String> {
    let body = build_body(msgs, image_b64, 600);
    let url = format!("{}/chat/completions", base());
    match ureq::post(&url)
        .header("Authorization", &format!("Bearer {}", key()))
        .header("Content-Type", "application/json")
        .send(body)
    {
        Ok(mut r) => match r.body_mut().read_to_string() {
            Ok(s) => Ok(extract_text(&s)),
            Err(e) => Err(format!("read body: {e}")),
        },
        Err(e) => Err(format!("request failed: {e}")),
    }
}

/// Pull `choices[0].message.content` out of the JSON (handles null/refusal + the
/// non-streaming tool_calls quirk by scanning for a string content).
fn extract_text(resp: &str) -> String {
    // Try the standard content field; if null, look for a non-null "content": "..."
    if let Some(i) = resp.find("\"content\":\"") {
        let rest = &resp[i + "\"content\":\"".len()..];
        if let Some(j) = rest.find('"') {
            return rest[..j].to_string();
        }
    }
    // fallback: anything after "refusal" or raw
    resp.chars().take(200).collect()
}

/// High-level: ask the AI a question, return the answer for printing/UI.
pub fn ask(prompt: &str) -> String {
    match chat(&[("system", "You are Comet, a sharp, concise browser assistant. Answer directly."), ("user", prompt)], None) {
        Ok(t) => t,
        Err(e) => format!("AI ERROR (no fake answer): {e}"),
    }
}
