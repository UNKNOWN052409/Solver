//! Comet — vision layer.
//!
//! LO's requirement: *non-visionary model ko vision dena, visionary model ki
//! vision enhance karna* — (1) give vision to a NON-vision model by routing the
//! image to a vision-capable model (Qwen-VL / Gemini / a local OCR) and feeding
//! back the description; (2) ENHANCE a vision model's perception by fusing OCR
//! text + a structured caption into its prompt so it sees more than raw pixels.
//!
//! Real calls: vision-capable model via ureq (same OpenAI-compatible channel),
//! plus a local OCR fallback (tesseract) when no vision model is configured.

use std::io::Read;

/// Read an image file and return it base64-encoded.
pub fn read_image_b64(path: &str) -> Result<String, String> {
    let data = std::fs::read(path).map_err(|e| format!("read {path}: {e}"))?;
    Ok(base64_encode(&data))
}

fn base64_encode(data: &[u8]) -> String {
    const TBL: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = ((b[0] as u32) << 16) | ((b[1] as u32) << 8) | (b[2] as u32);
        out.push(TBL[(n >> 18) as usize & 63] as char);
        out.push(TBL[(n >> 12) as usize & 63] as char);
        out.push(if chunk.len() > 1 { TBL[(n >> 6) as usize & 63] as char } else { '=' });
        out.push(if chunk.len() > 2 { TBL[n as usize & 63] as char } else { '=' });
    }
    out
}

/// Local OCR via tesseract (works without any vision model). Returns text or empty.
fn ocr_local(path: &str) -> String {
    if let Ok(out) = std::process::Command::new("tesseract").arg(path).arg("stdout").output() {
        if out.status.success() {
            return String::from_utf8_lossy(&out.stdout).trim().to_string();
        }
    }
    String::new()
}

/// Send an image to a vision-capable model (COMET_VISION_BASE/KEY/MODEL, defaults
/// to the same chat channel). Returns the model's description of the image.
pub fn describe_image(path: &str) -> String {
    let b64 = match read_image_b64(path) {
        Ok(b) => b,
        Err(e) => return format!("VISION ERROR: {e}"),
    };
    let base = std::env::var("COMET_VISION_BASE").unwrap_or_else(|_| "https://api.aisubscription.shop/v1".into());
    let key = std::env::var("COMET_VISION_KEY").unwrap_or_else(|_| std::env::var("AISK_API_KEY").unwrap_or_default());
    let model = std::env::var("COMET_VISION_MODEL").unwrap_or_else(|_| "deepseek-v4-flash".into());
    let body = format!(
        r#"{{"model":"{model}","messages":[{{"role":"user","content":[
            {{"type":"text","text":"Describe this image in detail: what text/objects/UI elements are present?"}},
            {{"type":"image_url","image_url":{{"url":"data:image/png;base64,{b64}"}}}}
        ]}}],"max_tokens":400}}"#
    );
    let url = format!("{base}/chat/completions");
    match ureq::post(&url)
        .header("Authorization", &format!("Bearer {key}"))
        .header("Content-Type", "application/json")
        .send(body)
    {
        Ok(mut r) => match r.body_mut().read_to_string() {
            Ok(s) => extract_text(&s),
            Err(e) => format!("vision read err: {e}"),
        },
        Err(e) => {
            // If a vision model isn't configured/reachable, fall back to local OCR.
            let ocr = ocr_local(path);
            if !ocr.is_empty() {
                format!("[vision model unavailable; local OCR fallback] {ocr}")
            } else {
                format!("VISION ERROR (no fake): {e}")
            }
        }
    }
}

/// Build an ENHANCED prompt for a vision-capable model: fuse local OCR text with
/// the raw image so the model sees more than pixels (vision enhancement).
pub fn enhance(path: &str, task: &str) -> String {
    let ocr = ocr_local(path);
    let b64 = read_image_b64(path).unwrap_or_default();
    let base = std::env::var("COMET_VISION_BASE").unwrap_or_else(|_| "https://api.aisubscription.shop/v1".into());
    let key = std::env::var("COMET_VISION_KEY").unwrap_or_else(|_| std::env::var("AISK_API_KEY").unwrap_or_default());
    let model = std::env::var("COMET_VISION_MODEL").unwrap_or_else(|_| "deepseek-v4-flash".into());
    let ocr_escaped = ocr.replace('"', "\\\"");
    let body = format!(
        r#"{{"model":"{model}","messages":[{{"role":"user","content":[
            {{"type":"text","text":"OCR of this image: \"{ocr_escaped}\". Using BOTH the OCR text and the image, {task}."}},
            {{"type":"image_url","image_url":{{"url":"data:image/png;base64,{b64}"}}}}
        ]}}],"max_tokens":500}}"#
    );
    let url = format!("{base}/chat/completions");
    match ureq::post(&url)
        .header("Authorization", &format!("Bearer {key}"))
        .header("Content-Type", "application/json")
        .send(body)
    {
        Ok(mut r) => match r.body_mut().read_to_string() {
            Ok(s) => extract_text(&s),
            Err(e) => format!("enhance err: {e}"),
        },
        Err(e) => format!("enhance err (no fake): {e}"),
    }
}

fn extract_text(resp: &str) -> String {
    if let Some(i) = resp.find("\"content\":\"") {
        let rest = &resp[i + "\"content\":\"".len()..];
        if let Some(j) = rest.find('"') {
            return rest[..j].to_string();
        }
    }
    resp.chars().take(200).collect()
}
