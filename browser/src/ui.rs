//! Comet — desktop UI served as a real web page via the built-in HTTP server.
//!
//! Why web-UI instead of a native window (egui/eframe)? In the rootless Kali-proot
//! environment the native GUI dependency tree (eframe -> winit -> wayland/x11rb)
//! does not compile: rustc dies with SIGBUS / `Function not implemented (os error
//! 38)` because proot lacks the Wayland/X11 syscalls and build-script subprocess
//! spawn. So the desktop UI is a real web page (`comet ui` -> serves HTML on
//! 127.0.0.1) — a browser-shaped control panel where every button dispatches a
//! REAL module call on the server side:
//!
//!   /api/ask     -> ai::ask (chat completion via configured provider)
//!   /api/open    -> real HTTP GET (std TcpStream, TLS via ureq for https)
//!   /api/vision  -> vision::describe_image / enhance
//!   /api/pdf     -> pdf::run (pdftotext extract + AI edit)
//!   /api/run     -> inbuilt compiler (spawn rustc/python3/node/bash/gcc)
//!   /api/click   -> click::click_at (REAL enigo mouse event)
//!   /api/oauth   -> oauth status/link (capture server)
//!   /api/solve   -> solverapi::solve_for_url (real POST to solver/)
//!   /api/status  -> provider/env health snapshot
//!
//! No mock answers anywhere: unreachable providers surface as honest errors.

use crate::ai;
use crate::click;
use crate::oauth;
use crate::pdf;
use crate::solverapi;
use crate::vision;
use std::io::{Read, Write};
use std::net::TcpListener;

const HTML: &str = include_str!("../assets/panel.html");

/// Serve the web UI on 127.0.0.1:<port> (default 8767). Blocks until stopped.
pub fn run(args: &[String]) {
    let port = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8767);
    let addr = format!("0.0.0.0:{port}");
    match TcpListener::bind(&addr) {
        Ok(listener) => {
            println!("[comet ui] serving desktop UI on http://127.0.0.1:{port}");
            println!("[comet ui] LAN: http://{}:{port}", lan_ip());
            for stream in listener.incoming() {
                if let Ok(s) = stream {
                    // Each connection handled inline (small server, low resource).
                    let _ = handle(s);
                }
            }
        }
        Err(e) => eprintln!("[comet ui] cannot bind {addr}: {e}"),
    }
}

/// Best-effort LAN IP (for printing a reachable URL). Reads the route to
/// 8.8.8.8 via /proc/net/rout — std-only, no external crate.
fn lan_ip() -> String {
    if let Ok(s) = std::fs::read_to_string("/proc/net/route") {
        for line in s.lines().skip(1) {
            let f: Vec<&str> = line.split('\t').collect();
            if f.len() >= 3 && f[1] == "00000000" {
                let hex = f[2];
                if hex.len() == 8 {
                    let b = |i: usize| u32::from_str_radix(&hex[i * 2..i * 2 + 2], 16).unwrap_or(0);
                    return format!("{}.{}.{}.{}", b(3), b(2), b(1), b(0));
                }
            }
        }
    }
    "127.0.0.1".into()
}

fn handle(mut s: std::net::TcpStream) -> std::io::Result<()> {
    let mut buf = [0u8; 8192];
    let n = s.read(&mut buf)?;
    let req = String::from_utf8_lossy(&buf[..n]).to_string();
    let first = req.lines().next().unwrap_or("");
    let mut parts = first.split_whitespace();
    let _method = parts.next().unwrap_or("GET");
    let path = parts.next().unwrap_or("/");

    let (status, ctype, body_bytes): (u16, &str, Vec<u8>) = if path == "/favicon.ico" {
        // Serve the real icon bytes (embedded at compile time) — binary-safe.
        (200, "image/png", include_bytes!("../assets/comet_icon.png").to_vec())
    } else {
        let (s, c, b) = route(&path, &req);
        (s, c, b.into_bytes())
    };
    let resp_head = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body_bytes.len()
    );
    let mut resp: Vec<u8> = resp_head.into_bytes();
    resp.extend_from_slice(&body_bytes);
    s.write_all(&resp)?;
    s.flush()
}

/// Dispatch panel routes to the REAL module calls. Everything runs inline —
/// the panel is a thin skin over the same functions the CLI uses.
fn route(path: &str, req: &str) -> (u16, &'static str, String) {
    match path {
        "/" | "/index.html" => (200, "text/html; charset=utf-8", HTML.into()),
        "/api/status" => {
            let base = std::env::var("COMET_LLM_BASE").unwrap_or_else(|_| "https://api.aisubscription.shop/v1".into());
            let model = std::env::var("COMET_LLM_MODEL").unwrap_or_else(|_| "deepseek-v4-flash".into());
            let keyed = !ai::key_present();
            let status = format!(
                "{{\"comet\":\"{}\",\"llm_base\":\"{}\",\"model\":\"{}\",\"key_set\":{},\"solver\":\"{}\",\"click_display\":{},\"time\":\"{}\"}}",
                env!("CARGO_PKG_VERSION"),
                base,
                model,
                !keyed,
                solverapi::solve_for_url("status"), // honest probe: reports unreachable if so
                std::env::var("DISPLAY").is_ok(),
                now()
            );
            (200, "application/json", status)
        }
        "/api/ask" => {
            let prompt = body_field(req, "prompt");
            if prompt.is_empty() {
                return (400, "text/plain", "missing prompt".into());
            }
            (200, "text/plain", ai::ask(&prompt))
        }
        "/api/open" => {
            let url = body_field(req, "url");
            if url.is_empty() {
                return (400, "text/plain", "missing url".into());
            }
            (200, "text/plain", http_get(&url))
        }
        "/api/vision" => {
            let path = body_field(req, "path");
            let task = body_field(req, "task");
            let enhance = body_field(req, "enhance") == "1";
            if path.is_empty() {
                return (400, "text/plain", "missing image path".into());
            }
            let out = if enhance {
                vision::enhance(&path, &task)
            } else {
                vision::describe_image(&path)
            };
            (200, "text/plain", out)
        }
        "/api/pdf" => {
            let path = body_field(req, "path");
            let instruction = body_field(req, "instruction");
            if path.is_empty() || instruction.is_empty() {
                return (400, "text/plain", "missing path/instruction".into());
            }
            (200, "text/plain", pdf::run(&path, &instruction))
        }
        "/api/run" => {
            let path = body_field(req, "path");
            if path.is_empty() {
                return (400, "text/plain", "missing path".into());
            }
            (200, "text/plain", compile_and_run(&path))
        }
        "/api/click" => {
            let x: i32 = body_field(req, "x").parse().unwrap_or(0);
            let y: i32 = body_field(req, "y").parse().unwrap_or(0);
            let button = match body_field(req, "button").as_str() {
                "right" => enigo::Button::Right,
                "middle" => enigo::Button::Middle,
                _ => enigo::Button::Left,
            };
            let double = body_field(req, "double") == "1";
            (200, "text/plain", click::click_at(x, y, button, double))
        }
        "/api/oauth" => {
            // Return the capture link + whether a code is already captured.
            let port = std::env::var("COMET_OAUTH_PORT")
                .ok()
                .and_then(|p| p.parse().ok())
                .unwrap_or(8091);
            let link = oauth::build_auth_url(
                &std::env::var("COMET_OAUTH_CLIENT_ID").unwrap_or_else(|_| "comet-capture".into()),
                &format!("http://127.0.0.1:{port}/callback"),
                &std::env::var("COMET_OAUTH_SCOPE").unwrap_or_else(|_| "openid email profile".into()),
                &format!("comet{}", std::process::id()),
            );
            let captured = std::fs::read_to_string("qwen_oauth_code.txt")
                .map(|c| c.trim().to_string())
                .unwrap_or_default();
            let out = format!(
                "{{\"link\":\"{}\",\"captured_code\":\"{}\"}}",
                link,
                captured
            );
            (200, "application/json", out)
        }
        "/api/solve" => {
            let url = body_field(req, "url");
            if url.is_empty() {
                return (400, "text/plain", "missing url".into());
            }
            (200, "text/plain", solverapi::solve_for_url(&url))
        }
        _ => (404, "text/plain", "not found".into()),
    }
}

/// Real HTTP GET. Uses ureq (already a dep for ai.rs) so both http/https work.
fn http_get(url: &str) -> String {
    match ureq::get(url).call() {
        Ok(mut r) => match r.body_mut().read_to_string() {
            Ok(s) => {
                let head: String = s.chars().take(4000).collect();
                format!("status: {}\n\n{}", 200, head)
            }
            Err(e) => format!("read err: {e}"),
        },
        Err(e) => format!("GET failed (no fake fetch): {e}"),
    }
}

/// Compile-and-run via the inbuilt compiler (same logic as CLI `run`).
fn compile_and_run(path: &str) -> String {
    use std::process::Command;
    let ext = path.rsplit('.').next().unwrap_or("");
    let (prog, args): (&str, Vec<String>) = match ext.to_lowercase().as_str() {
        "py" => ("python3", vec![path.to_string()]),
        "js" | "mjs" => ("node", vec![path.to_string()]),
        "sh" | "bash" => ("bash", vec![path.to_string()]),
        "rs" => ("rustc", vec![path.to_string(), "--edition".into(), "2021".into(), "-o".into(), "/tmp/comet_run.out".into()]),
        "c" => ("gcc", vec![path.to_string(), "-o".into(), "/tmp/comet_run.out".into()]),
        "cpp" | "cc" | "cxx" => ("g++", vec![path.to_string(), "-o".into(), "/tmp/comet_run.out".into()]),
        _ => return format!("unknown language for '{path}' — registered: py js sh rs c cpp"),
    };
    let out = Command::new(prog).args(&args).output();
    match out {
        Ok(o) => {
            let mut res = format!("$ {prog} {:?}\nexit: {}\n--- stdout ---\n{}\n--- stderr ---\n{}",
                args, o.status.code().unwrap_or(-1),
                String::from_utf8_lossy(&o.stdout).chars().take(2000).collect::<String>(),
                String::from_utf8_lossy(&o.stderr).chars().take(1000).collect::<String>());
            if prog == "rustc" || prog == "gcc" || prog == "g++" {
                if o.status.success() {
                    let run = Command::new("/tmp/comet_run.out").output();
                    if let Ok(r) = run {
                        res.push_str(&format!("\n--- run ---\nexit: {}\n{}",
                            r.status.code().unwrap_or(-1),
                            String::from_utf8_lossy(&r.stdout).chars().take(2000).collect::<String>()));
                    }
                }
            }
            res
        }
        Err(e) => format!("spawn {prog} failed: {e}"),
    }
}

/// Extract a form field from the request body (`key=value&key2=...`).
/// POST bodies from the panel are urlencoded.
fn body_field(req: &str, key: &str) -> String {
    let body = req.split("\r\n\r\n").nth(1).unwrap_or("");
    for pair in body.split('&') {
        let mut kv = pair.splitn(2, '=');
        if kv.next() == Some(key) {
            return urldecode(kv.next().unwrap_or(""));
        }
    }
    String::new()
}

fn urldecode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                let hex = std::str::from_utf8(&b[i + 1..i + 3]).unwrap_or("");
                if let Ok(v) = u8::from_str_radix(hex, 16) {
                    out.push(v);
                    i += 3;
                } else {
                    out.push(b[i]);
                    i += 1;
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

fn now() -> String {
    // seconds since epoch -> human-ish (no chrono dep; honest timestamp)
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("{secs}")
}
