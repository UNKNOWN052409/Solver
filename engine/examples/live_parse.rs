//! Live proof: GhostEngine real page parse karta hai (GhostMouse se fetch).
use ghostengine::Page;

fn main() {
    let out = std::process::Command::new("../target/release/ghostmouse")
        .args(["get", "https://httpbin.org/html"])
        .output()
        .expect("ghostmouse get chalao");
    let html = String::from_utf8_lossy(&out.stdout).to_string();
    let page = Page::parse(&html);
    println!("== GhostEngine LIVE PARSE ==");
    println!("title: {}", page.title());
    let t = page.text();
    println!("text-sample: {}", &t[..t.len().min(120)]);
    println!("links: {}", page.links().len());
    println!("== APNA ENGINE REAL HTML PARSE OK ==");
}
