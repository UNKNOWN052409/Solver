//! Comet — Qwen (and OpenAI-compatible provider) OAuth capture helper.
//!
//! LO wants the browser to connect to Qwen / other AI providers via an OAuth
//! capture link. This module starts a tiny localhost listener that hosts the
//! provider's OAuth authorization URL; when the user completes the flow, the
//! provider redirects to `http://127.0.0.1:<port>/callback` and we capture the
//! `code`. The code is then exchanged (or shown) so Comet can hold a Qwen token.
//!
//! Env:  COMET_OAUTH_CLIENT_ID / COMET_OAUTH_REDIRECT / COMET_OAUTH_SCOPE.
//! For a generic OpenAI-compatible gateway the "oauth" is often just an API key
//! in the config; this capture now targets real OAuth providers (e.g. Qwen
//! DashScope-style codes, or a self-hosted OAuth gateway) and the redirect-capture
//! flow shown in the UI.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

/// Default OAuth authorize endpoint for Qwen (DashScope / Alibaba) style flows.
const QWEN_AUTH: &str = "https://oauth.example.com/authorize";

/// Build the provider's authorization URL with the given client id + scope.
/// Real: constructs the query so the user can open it and complete login.
pub fn build_auth_url(client_id: &str, redirect: &str, scope: &str, state: &str) -> String {
    let esc = |s: &str| urlencode(s);
    format!(
        "{}?response_type=code&client_id={}&redirect_uri={}&scope={}&state={}&access_type=offline&prompt=consent",
        env_unwrap_or("COMET_OAUTH_AUTH", QWEN_AUTH),
        esc(client_id),
        esc(redirect),
        esc(scope),
        esc(state)
    )
}

fn urlencode(s: &str) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(b as char),
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}

fn env_unwrap_or(k: &str, d: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| d.into())
}

/// Start a local capture server on <port>. It serves the auth link and, on GET
/// /callback?code=..., saves the code to a file (COMET_OAUTH_STATE dir) and
/// prints it. This is the REAL capture path.
pub fn run(port: u16) {
    let client_id = env_unwrap_or("COMET_OAUTH_CLIENT_ID", "comet-capture");
    let redirect = format!("http://127.0.0.1:{port}/callback");
    let scope = env_unwrap_or("COMET_OAUTH_SCOPE", "openid email profile");
    let state = format!("comet{}", std::process::id());
    let url = build_auth_url(&client_id, &redirect, &scope, &state);

    let addr = format!("127.0.0.1:{port}");
    match TcpListener::bind(&addr) {
        Ok(listener) => {
            println!("[comet oauth] CAPTURE LINK (open in browser):\n  {url}");
            println!("[comet oauth] listening on http://{addr}  (redirect + code capture)");
            for stream in listener.incoming() {
                if let Ok(mut s) = stream {
                    let mut buf = [0u8; 4096];
                    let n = s.read(&mut buf).unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();
                    let code = extract_code(&req);
                    if let Some(c) = code {
                        let dir = std::env::var("COMET_OAUTH_STATE").unwrap_or_else(|_| ".".into());
                        let _ = std::fs::create_dir_all(&dir);
                        let f = format!("{dir}/qwen_oauth_code.txt");
                        let _ = std::fs::write(&f, &c);
                        println!("[comet oauth] CAPTURED code (len {}): {}\n  saved -> {f}", c.len(), c);
                        let body = "<h3>Comet OAuth capture complete.</h3><p>Code saved. You can close this tab.</p>";
                        let resp = http_resp(body);
                        let _ = s.write_all(resp.as_bytes());
                        let _ = s.flush();
                        // stop after one successful capture
                        break;
                    } else {
                        let body = "<h3>Comet OAuth capture</h3><p>Waiting for provider redirect with <code>?code=...</code>.</p>";
                        let resp = http_resp(body);
                        let _ = s.write_all(resp.as_bytes());
                        let _ = s.flush();
                    }
                }
            }
        }
        Err(e) => eprintln!("[comet oauth] cannot bind {addr}: {e}"),
    }
}

fn extract_code(req: &str) -> Option<String> {
    // GET /callback?code=XXXX HTTP/1.1
    let line = req.lines().next()?;
    let q = line.split_whitespace().nth(1)?;
    let mut after = q.split("code=").nth(1)?;
    after = after.split('&').next().unwrap_or(after);
    if after.is_empty() { None } else { Some(after.to_string()) }
}

fn http_resp(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    )
}
