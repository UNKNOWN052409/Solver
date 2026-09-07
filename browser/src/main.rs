//! Comet — lightweight Rust browser core (scaffold crate).
//!
//! This is the std-only seed of the `browser/` half of LO's one-repo /
//! two-folder model. It deliberately uses ONLY the Rust standard library so
//! `cargo build` succeeds everywhere (including offline / Kali-proot without
//! network) and the binary stays tiny. Real functionality:
//!
//!   * `run`   — inbuilt compiler: detect language from file, spawn the system
//!               toolchain (rustc / python3 / node / bash), stream stdout+stderr
//!               to the console and report the exit code.  (Design doc §4.3)
//!   * `serve` — start a minimal HTTP server that answers `/health` and a
//!               MCP-shaped `/tools` JSON. Skeleton for the MCP integration.
//!   * `open`  — real HTTP GET via std TcpStream (no external HTTP crate): fetch
//!               the page, print status + text-ish head.
//!   * `whoami`— print the persona/identity line (design: stable per-user).
//!   * `solve` — placeholder for the solver-api consumer: computes the request
//!               body that browser/solverapi would POST to solver/ (§4.6). It
//!               does NOT fake a network solve; it prints the contract.
//!
//! No DOM/JS engine. No WebKit. No browser binary. That is the design.

use std::env;
use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Command, Stdio};

mod ai;
mod click;
mod oauth;
mod pdf;
mod solverapi;
mod ui;
mod vision;

const VERSION: &str = env!("CARGO_PKG_VERSION");

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        usage();
        return;
    }
    match args[1].as_str() {
        "run" => cmd_run(&args[2..]),
        "serve" => cmd_serve(&args[2..]),
        "open" => cmd_open(&args[2..]),
        "click" => click::run(&args[2..]),
        "ui" => ui::run(&args[2..]),
        "ask" => cmd_ask(&args[2..]),
        "vision" => cmd_vision(&args[2..]),
        "pdf" => cmd_pdf(&args[2..]),
        "oauth" => cmd_oauth(&args[2..]),
        "whoami" => cmd_whoami(&args[2..]),
        "solve" => cmd_solve(&args[2..]),
        // Proxy mode + solver config are read from env; expose them here.
        "config" => cmd_config(),
        "version" => println!("comet {VERSION}"),
        _ => usage(),
    }
}

fn usage() {
    println!(
        "Comet {VERSION} — lightweight Rust browser core\n\n\
         USAGE: comet <subcommand>\n\n\
         SUBCOMMANDS:\n\
           run <lang|path> [srcfile]   inbuilt compiler: compile+run, print output\n\
           serve [--port N]            start HTTP/MCP-ready server on 127.0.0.1\n\
           open <url>                  HTTP GET via std TcpStream (no external crate)\n\
           click <x> <y> [--button …]  REAL OS mouse click via enigo\n\
           ui                          launch the egui desktop window\n\
           whoami                       print the stable per-user persona line\n\
           solve <sitekey> [url]       print the solver-api request contract\n\
           config                       print proxy/solver/env config state\n\
           version                      print version\n"
    );
}

/// Detect the compiler for a source file from its extension.
/// Returns (program, args-prefix). Only shells out to toolchains known to be
/// installed; the browser never embeds a language.
fn detect_lang(path: &str) -> Result<(&'static str, Vec<String>), String> {
    let ext = path.rsplit('.').next().unwrap_or("");
    match ext.to_lowercase().as_str() {
        "rs" => Ok(("rustc", vec!["--edition".into(), "2021".into()])),
        "py" => Ok(("python3", vec![])),
        "js" | "mjs" => Ok(("node", vec![])),
        "sh" | "bash" => Ok(("bash", vec![])),
        "c" => Ok(("gcc", vec![])),
        "cpp" | "cc" | "cxx" => Ok(("g++", vec![])),
        "go" => Ok(("go", vec!["run".into()])),
        _ => Err(format!(
            "unknown language for '{path}' — the inbuilt compiler shells out to an \
             installed toolchain; registered: rs, py, js, sh, c, cpp, go"
        )),
    }
}

/// Run a source file through the detected toolchain, streaming output.
/// This is the REAL inbuilt-compiler path: it spawns the process, inherits
/// stderr/stdout to the console (so output appears live), and exits with the
/// child's code. Nothing is mocked.
fn cmd_run(args: &[String]) {
    // Accept either `comet run <path>` (lang from extension) or
    // `comet run <lang> <path>` where lang selects an explicit override.
    let (lang_override, src_path) = match args {
        [a] => (None, a.clone()),
        [lang, path] => (Some(lang.as_str()), path.clone()),
        _ => {
            eprintln!("comet run wants a file path (lang auto-detected) or `<lang> <path>`");
            return;
        }
    };

    let (prog, mut pre) = match lang_override {
        Some(lang) => match lang {
            "rs" => ("rustc", vec!["--edition".into(), "2021".into()]),
            "py" => ("python3", vec![]),
            "js" => ("node", vec![]),
            "sh" => ("bash", vec![]),
            "c" => ("gcc", vec![]),
            other => {
                eprintln!("unknown explicit language: {other}");
                return;
            }
        },
        None => match detect_lang(&src_path) {
            Ok((p, pre)) => (p, pre),
            Err(e) => {
                eprintln!("{e}");
                return;
            }
        },
    };

    // For compiled languages (rustc/gcc/g++), append output binary path.
    if prog == "rustc" || prog == "gcc" || prog == "g++" {
        let out_bin = format!("{}.out", src_path.rsplit('/').next().unwrap_or("comet_run"));
        pre.push(src_path.clone());
        pre.push("-o".into());
        // Leading "./" gives the executable a real path so spawning works even
        // though "." is not on PATH. (Real bug found by exercising the scaffold:
        // a bare-name spawn fails with exit 1 and no output.)
        let runpath = format!("./{out_bin}");
        pre.push(runpath.clone());
        println!("[comet] {prog} {:?}", pre);
        // compile, then run the produced binary if compile succeeded
        let status = Command::new(prog)
            .args(&pre)
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .status();
        match status {
            Ok(s) if s.success() => {
                println!("[comet] compile ok, running {runpath}");
                let run_status = Command::new(&runpath)
                    .stdout(Stdio::inherit())
                    .stderr(Stdio::inherit())
                    .status();
                let code = run_status.map(|s| s.code().unwrap_or(1)).unwrap_or(1);
                println!("[comet] exit code: {code}");
            }
            Ok(s) => {
                eprintln!("[comet] compile failed (exit {:?})", s.code());
            }
            Err(e) => eprintln!("[comet] could not spawn {prog}: {e}"),
        }
        return;
    }

    // Interpreted / runnable languages (python3, node, bash, go run).
    pre.push(src_path.clone());
    println!("[comet] {prog} {:?}", pre);
    match Command::new(prog)
        .args(&pre)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
    {
        Ok(s) => println!("[comet] exit code: {:?}", s.code()),
        Err(e) => eprintln!("[comet] could not spawn {prog}: {e}"),
    }
}

/// Minimal HTTP server skeleton. Answers:
///   GET /health -> {"ok":true,"comet":"0.1.0"}
///   GET /tools  -> MCP-shaped tool list JSON (the browser's MCP surface)
///   GET /        -> small index text
/// Uses std TcpListener only — no external HTTP stack.
fn cmd_serve(args: &[String]) {
    let port = args
        .windows(2)
        .find(|w| w[0] == "--port")
        .and_then(|w| w[1].parse::<u16>().ok())
        .unwrap_or(8765);
    let addr = format!("127.0.0.1:{port}");
    let listener = match TcpListener::bind(&addr) {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[comet] bind {addr}: {e}");
            return;
        }
    };
    println!("[comet] serving on http://{addr}  (Ctrl-C to stop)");
    for stream in listener.incoming() {
        match stream {
            Ok(mut s) => {
                let _ = handle_conn(&mut s);
            }
            Err(e) => eprintln!("[comet] conn error: {e}"),
        }
    }
}

fn handle_conn(stream: &mut TcpStream) -> io::Result<()> {
    let mut buf = [0u8; 2048];
    let n = stream.read(&mut buf)?;
    let req = String::from_utf8_lossy(&buf[..n]).to_string();
    let path = req
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .unwrap_or("/");

    let (status, body) = match path {
        "/health" => (200, format!("{{\"ok\":true,\"comet\":\"{VERSION}\"}}")),
        "/tools" => (
            200,
            r#"{"tools":[{"name":"open_page"},{"name":"describe_page"},{"name":"run_code"},{"name":"solve_captcha"},{"name":"search"}]}"#
                .to_string(),
        ),
        _ => (200, format!("Comet {VERSION} — see /health and /tools")),
    };

    let resp = format!(
        "HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream.write_all(resp.as_bytes())?;
    stream.flush()
}

/// Real HTTP GET via std::net::TcpStream (no external crate). Resolves the host
/// port and sends a minimal HTTP/1.1 request, then prints status line + head.
/// Used to demonstrate the browser's "open a page" without a web engine.
fn cmd_open(args: &[String]) {
    if args.is_empty() {
        eprintln!("comet open <url>");
        return;
    }
    let raw = &args[0];
    let (host, port, path) = parse_url(raw);
    let addr = format!("{host}:{port}");
    let mut stream = match TcpStream::connect(&addr) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("[comet] connect {addr}: {e}");
            return;
        }
    };
    let req = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Comet/{VERSION}\r\nAccept: text/html,application/json\r\nConnection: close\r\n\r\n"
    );
    if let Err(e) = stream.write_all(req.as_bytes()) {
        eprintln!("[comet] write: {e}");
        return;
    }
    let mut resp = String::new();
    let _ = stream.read_to_string(&mut resp);
    // Print status line + a trimmed sample of the body.
    let (status, rest) = resp.split_once("\r\n").unwrap_or(("", ""));
    println!("[comet] status: {status}");
    let head = rest.find("\r\n\r\n").map(|i| &rest[..i]).unwrap_or("");
    println!("--- headers ---\n{head}");
    println!(
        "--- body (head) ---\n{}",
        &rest[rest.len().min(1200)..]
            .lines()
            .take(5)
            .collect::<Vec<_>>()
            .join("\n")
    );
}

/// Print the proxy / solver / persona env state. Reflects §4.5 + §7 config.
fn cmd_config() {
    println!(
        "[comet] proxy mode  : {}",
        env::var("COMET_PROXY_MODE").unwrap_or_else(|_| "same-ip".into())
    );
    println!(
        "[comet] system ip   : {}",
        env::var("COMET_SYSTEM_IP").unwrap_or_else(|_| "auto-detect".into())
    );
    println!(
        "[comet] resi pool   : {} entries",
        env::var("COMET_RESI_POOL")
            .unwrap_or_default()
            .split(',')
            .filter(|s| !s.is_empty())
            .count()
    );
    println!(
        "[comet] resi route  : {}",
        env::var("COMET_RESI_ROUTE").unwrap_or_else(|_| "last".into())
    );
    println!(
        "[comet] solver url  : {}",
        env::var("COMET_SOLVER_URL").unwrap_or_else(|_| "http://127.0.0.1:8081/solve".into())
    );
}

/// Print the solver-api request contract that browser/solverapi POSTs to the
/// solver. This is scaffolding for the folder in §4.6 — it shows the real JSON body
/// but does NOT pretend to solve without the solver (no mock network solve).
fn cmd_solve(args: &[String]) {
    let sitekey = args
        .first()
        .map(|s| s.as_str())
        .unwrap_or("0xMISSING_SITEKEY");
    let url = args
        .get(1)
        .map(|s| s.as_str())
        .unwrap_or("https://example.com");
    println!("[comet] solver-api request contract (browser/solverapi -> solver/):");
    println!(
        "  POST {}\n  {{ \"sitekey\": \"{sitekey}\", \"type\": \"recaptcha_v2\", \"url\": \"{url}\", \"image_b64\": null }}",
        env::var("COMET_SOLVER_URL").unwrap_or_else(|_| "http://127.0.0.1:8081/solve".into())
    );
    println!("  Note: proving the solve requires the running solver/ HTTP service (POST /solve).");
}

/// Print the stable per-user persona line (design §4.2 identity).
fn cmd_whoami(_args: &[String]) {
    let user = env::var("COMET_USER").unwrap_or_else(|_| "lo".into());
    // FNV-1a over user -> deterministic persona index.
    let mut h: u64 = 0xcbf29ce484222325;
    for b in user.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    let persona = [
        "win-chrome-136",
        "mac-chrome-135",
        "linux-chrome-134",
        "win-chrome-134",
        "mac-chrome-136",
    ][(h as usize) % 5];
    println!(
        "[comet] user={user} persona={persona} egress={}",
        env::var("COMET_SYSTEM_IP").unwrap_or_else(|_| "auto".into())
    );
}

fn parse_url(raw: &str) -> (String, u16, String) {
    let rest = raw
        .trim_start_matches("https://")
        .trim_start_matches("http://");
    let (hostpart, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    let path = if path.is_empty() { "/" } else { path };
    let (host, port) = match hostpart.rsplit_once(':') {
        Some((h, p)) => (h.to_string(), p.parse::<u16>().unwrap_or(443)),
        None => (hostpart.to_string(), 443),
    };
    (host, port, path.to_string())
}

/// `comet ask <prompt...>` — send a real chat completion and print the answer.
/// Env: COMET_LLM_BASE / COMET_LLM_KEY / COMET_LLM_MODEL (default AISK deepseek).
fn cmd_ask(args: &[String]) {
    let prompt = args.join(" ");
    if prompt.trim().is_empty() {
        eprintln!("comet ask wants a prompt (e.g. `comet ask \"summarize this page\"`)");
        return;
    }
    let answer = ai::ask(&prompt);
    println!("{answer}");
}

/// `comet vision <image> [--enhance]<task>` — give the model vision of an image.
/// Non-vision model -> route image to the vision channel; --enhance fuses OCR.
fn cmd_vision(args: &[String]) {
    if args.is_empty() {
        eprintln!("comet vision wants an image path (e.g. `comet vision shot.png`)");
        return;
    }
    let mut path = args[0].clone();
    let mut enhance = false;
    let mut task = "describe this image".to_string();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--enhance" => { enhance = true; i += 1; }
            "--task" => { if i + 1 < args.len() { task = args[i + 1].clone(); i += 2; } else { i += 1; } }
            _ => { task = args[i..].join(" "); i = args.len(); }
        }
    }
    if enhance {
        println!("{}", vision::enhance(&path, &task));
    } else {
        println!("{}", vision::describe_image(&path));
    }
}

/// `comet pdf <path> <instruction...>` — extract PDF text, AI-edit it, print result.
fn cmd_pdf(args: &[String]) {
    if args.len() < 2 {
        eprintln!("comet pdf wants `<path> <instruction>` (e.g. `comet pdf contract.pdf \"fix the date to 2026\"`)");
        return;
    }
    let path = args[0].clone();
    let instruction = args[1..].join(" ");
    println!("{}", pdf::run(&path, &instruction));
}

/// `comet oauth [--port N]` — start the Qwen/provider OAuth capture server and
/// print the capture link. Redirect with ?code=... is captured to qwen_oauth_code.txt.
fn cmd_oauth(args: &[String]) {
    let port = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8091);
    oauth::run(port);
}
