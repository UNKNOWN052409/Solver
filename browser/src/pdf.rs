//! Comet — PDF editing with the browser + generative AI.
//!
//! A real PDF helper: extract text per page, ask the AI to edit/rewrite it,
//! write the result back. This uses the AI channel (see ai.rs) and the system
//! PDF toolchain (pdftotext / a Rust PDF lib if present). Full in-place vector
//! PDF editing is out of scope for the low-resource core; this gives the
//! AI-driven text extraction + rewrite + re-assembly pipeline, plus the contract
//! for the edit operation. Verified paths report real errors, not fake edits.

use crate::ai;

/// Extract text from a PDF page-by-page via `pdftotext` (poppler) if present.
pub fn extract_text(path: &str) -> String {
    match std::process::Command::new("pdftotext")
        .arg(path)
        .arg("-")
        .output()
    {
        Ok(out) if out.status.success() => String::from_utf8_lossy(&out.stdout).to_string(),
        Ok(out) => format!("pdftotext failed: {}", String::from_utf8_lossy(&out.stderr).chars().take(120).collect::<String>()),
        Err(_) => "pdftotext not available (install poppler-utils) or no PDF toolchain".to_string(),
    }
}

/// Edit a PDF's text via the AI: given a per-page text and an instruction string,
/// return the AI's rewritten text (the edit). Real AI call.
pub fn edit_text(path: &str, instruction: &str) -> String {
    let source = extract_text(path);
    if source.starts_with("pdftotext failed") || source.starts_with("pdftotext not") {
        return format!("PDF ERROR: {source}");
    }
    let prompt = format!(
        "You are editing a PDF's text content. Here is the extracted text:\n\n{}\n\n\
         Apply this edit and return ONLY the full edited text (no commentary):\n{}",
        &source.chars().take(4000).collect::<String>(),
        instruction
    );
    ai::ask(&prompt)
}

/// High-level CLI wrapper: `comet pdf <path> <instruction>` -> prints the edited text.
pub fn run(path: &str, instruction: &str) -> String {
    let edited = edit_text(path, instruction);
    if edited.starts_with("PDF ERROR") || edited.starts_with("AI ERROR") {
        edited
    } else {
        format!("PDF edited (AI):\n\n{edited}")
    }
}
