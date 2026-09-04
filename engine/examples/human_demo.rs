//! Live: real page fetch -> human render + anti-captcha detect.
use ghostengine::{anti, render, Page};

fn main() {
    // 2captcha demo — captcha wala real page
    let out = std::process::Command::new("../target/release/ghostmouse")
        .args(["get", "https://2captcha.com/demo/recaptcha-v2"])
        .output()
        .expect("fetch");
    let html = String::from_utf8_lossy(&out.stdout).to_string();
    let page = Page::parse(&html);

    println!("═══ HUMAN RENDER (sample) ═══");
    let r = render(&page, 80);
    println!("{}", &r[..r.len().min(700)]);

    println!("\n═══ ANTI-CAPTCHA DETECT ═══");
    let v = anti::verdict(&page);
    println!("walled: {} | interstitial: {}", v.walled, v.interstitial);
    for w in &v.walls {
        println!(
            "  tech={} sitekey={:?} keyless={} hint={}",
            w.tech.name(),
            w.sitekey.as_deref().map(|s| &s[..s.len().min(14)]),
            w.tech.keyless(),
            &w.hint[..w.hint.len().min(40)]
        );
    }
    // clean page control
    let out2 = std::process::Command::new("../target/release/ghostmouse")
        .args(["get", "https://httpbin.org/html"])
        .output()
        .expect("fetch2");
    let clean = Page::parse(&String::from_utf8_lossy(&out2.stdout));
    let v2 = anti::verdict(&clean);
    println!(
        "\nclean-page control: walled={} (false hona chahiye)",
        v2.walled
    );
}
