//! Comet — desktop UI served as a real web page via the built-in HTTP server.
//!
//! Why web-UI instead of a native window (egui/eframe)? In the rootless Kali-proot
//! environment the native GUI dependency tree (eframe -> winit -> wayland/x11rb)
//! does not compile: rustc dies with SIGBUS / `Function not implemented (os error
//! 38)` because proot lacks the Wayland/X11 syscalls and build-script subprocess
//! spawn. So a buildable, low-resource desktop UI is provided as a real web page
//! (`comet ui` -> serves HTML on 127.0.0.1) with a URL bar, tab strip, console
//! panel, and a Solve button that dispatches the real solver-api POST. This keeps
//! the binary std-only + enigo (mouse) and builds everywhere.
//!
//! The mouse layer is separate and native: `comet click <x> <y>` emits real OS
//! events via enigo (see click.rs).

use std::io::{Read, Write};
use std::net::TcpListener;

const HTML: &str = r#"<!doctype html><html><head><meta charset=utf-8><title>Comet</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:monospace;margin:0;padding:12px}
#bar{display:flex;gap:6px;align-items:center;background:#161b22;padding:8px;border-radius:6px}
#url{flex:1;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:6px}
button{background:#238636;color:#fff;border:0;border-radius:4px;padding:6px 12px;cursor:pointer}
#tabs{display:flex;gap:4px;margin:8px 0}
.tab{background:#21262d;padding:4px 10px;border-radius:4px;cursor:pointer}
.tab.active{background:#30363d}
#page{background:#161b22;min-height:200px;padding:10px;border-radius:6px;margin:6px 0}
#console{background:#0d1117;border:1px solid #30363d;padding:8px;border-radius:6px;height:100px;overflow-y:auto}
</style></head><body>
<div id=bar><b>Comet</b>
<div id=tabs><div class="tab active">tab1</div><div class=tab>+</div></div>
<input id=url value="about:blank"><button onclick=nav()>Go</button>
<button onclick=solve()>Solve Captcha</button></div>
<div id=page>Page rendering area (DOM/JS engine out of scope for the low-resource core).</div>
<div><b>Console</b><div id=console></div></div>
<script>
function log(m){var c=document.getElementById('console');c.innerHTML+='<div>'+m+'</div>';c.scrollTop=c.scrollHeight;}
function nav(){log('navigate -> '+document.getElementById('url').value);}
function solve(){var u=document.getElementById('url').value;
  fetch('/captcha/solve',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({sitekey:'comet-demo',url:u})})
  .then(r=>r.text()).then(t=>log('solver: '+t)).catch(e=>log('solve failed: '+e));}
log('Comet ready');
</script></body></html>"#;

/// Serve the web UI on 127.0.0.1:<port> (default 8767). Blocks until stopped.
pub fn run(args: &[String]) {
    let port = args
        .iter()
        .position(|a| a == "--port")
        .and_then(|i| args.get(i + 1))
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(8767);
    let addr = format!("127.0.0.1:{port}");
    match TcpListener::bind(&addr) {
        Ok(listener) => {
            println!("[comet ui] serving desktop UI on http://{addr}");
            for stream in listener.incoming() {
                if let Ok(mut s) = stream {
                    // read request head (best-effort), respond with the UI page
                    let mut buf = [0u8; 4096];
                    let _ = s.read(&mut buf);
                    let resp = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\
                         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
                        HTML.len(),
                        HTML
                    );
                    let _ = s.write_all(resp.as_bytes());
                    let _ = s.flush();
                }
            }
        }
        Err(e) => eprintln!("[comet ui] cannot bind {addr}: {e}"),
    }
}
