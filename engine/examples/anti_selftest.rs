//! RE self-test: engine apne hi demo HTML pe anti-detect chalata hai.
use ghostengine::{Page, anti};
fn main() {
    let path = std::env::args().nth(1).expect("html path");
    let html = std::fs::read_to_string(path).expect("read");
    let page = Page::parse(&html);
    let v = anti::verdict(&page);
    println!("walled={} interstitial={}", v.walled, v.interstitial);
    for w in &v.walls {
        println!("  {} sitekey={:?} keyless={} hint={}", w.tech.name(), w.sitekey.as_deref(), w.tech.keyless(), w.hint);
    }
    println!("title={}", page.title());
    println!("links={:?}", page.links().iter().map(|(h, _)| h).take(2).collect::<Vec<_>>());
    println!("forms={:?}", page.forms().iter().map(|(a, m, f)| (a, m, f.len())).collect::<Vec<_>>());
}
