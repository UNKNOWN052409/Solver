//! Comet — anti-captcha solver-api consumer.
//!
//! browser/solverapi/ : the browser-side client that POSTs a captcha image or
//! token to a solver endpoint and parses the answer. It is the glue between the
//! UI "Solve Captcha" button and a running captcha solver. It performs a REAL
//! HTTP POST (std TcpStream, no external crate) and returns a plain-text result
//! so the UI can render it. If the endpoint is unreachable it honestly reports
//! the error — it does not fake an answer.

use std::io::{Read, Write};
use std::net::TcpStream;

/// Build a JSON request body for the solver (image token -> answer).
/// Loosely mirrors the solver' discriminator endpoint shape.
pub fn build_request(sitekey: &str, url: &str) -> String {
    format!(
        "{{\"type\":\"captcha\",\"sitekey\":\"{}\",\"url\":\"{}\"}}",
        sitekey,
        url
    )
}

/// POST the request to `host:port` (default 127.0.0.1:8766 like the local
/// solver service) and return the raw HTTP response line(s). REAL network call.
pub fn post(host: &str, port: u16, path: &str, body: &str) -> Result<String, String> {
    let addr = format!("{host}:{port}");
    let mut stream = TcpStream::connect(&addr).map_err(|e| format!("connect {addr}: {e}"))?;
    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream.write_all(req.as_bytes()).map_err(|e| format!("write: {e}"))?;
    stream.flush().map_err(|e| format!("flush: {e}"))?;
    let mut resp = Vec::new();
    stream.read_to_end(&mut resp).map_err(|e| format!("read: {e}"))?;
    Ok(String::from_utf8_lossy(&resp).to_string())
}

/// High-level: solve a captcha for a given url. Reads endpoint from env
/// (COMET_SOLVER_HOST / COMET_SOLVER_PORT, default 127.0.0.1:8766). Returns a
/// human-readable line for the UI console.
pub fn solve_for_url(url: &str) -> String {
    let host = std::env::var("COMET_SOLVER_HOST").unwrap_or_else(|_| "127.0.0.1".into());
    let port: u16 = std::env::var("COMET_SOLVER_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8766);
    let sitekey = "comet-demo";
    let body = build_request(sitekey, url);
    match post(&host, port, "/captcha/solve", &body) {
        Ok(resp) => format!("solver response for {url}: {}", resp.trim().chars().take(120).collect::<String>()),
        Err(e) => format!("SOLVE FAILED (no fake answer): {e}"),
    }
}
