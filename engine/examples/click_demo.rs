//! click_demo — HumanClicker ka live demo: interpolate a full human click and
//! print the CDP-ready event stream. Transport (CDP/GhostMouse) ise dispatch
//! karta hai; yahan sirf stdout pe dikhate hain kya generate hota hai.
//!
//!     cargo run --example click_demo -- 10 10 300 220          # mid click
//!     cargo run --example click_demo -- 0 0 640 480 low        # low-tier
//!     cargo run --example click_demo -- 0 0 640 480 high       # high-tier
use ghostengine::click::{human_click_path, MouseKind};
use ghostengine::adaptive::Tier;

fn main() {
    let mut args = std::env::args().skip(1);
    let x0: f64 = args.next().and_then(|s| s.parse().ok()).unwrap_or(20.0);
    let y0: f64 = args.next().and_then(|s| s.parse().ok()).unwrap_or(20.0);
    let x1: f64 = args.next().and_then(|s| s.parse().ok()).unwrap_or(320.0);
    let y1: f64 = args.next().and_then(|s| s.parse().ok()).unwrap_or(240.0);
    let tier = match args.next().as_deref() {
        Some("low") => Tier::Low,
        Some("high") => Tier::High,
        _ => Tier::Mid,
    };

    println!("HumanClicker {:?}  ({x0},{y0}) -> ({x1},{y1})", tier);
    println!("{:<7} {:>7} {:>7} {:>6}  {}", "kind", "x", "y", "t_ms", "btn");
    let evts = human_click_path(tier, (x0, y0), (x1, y1), 42);
    for e in &evts {
        let k = match e.kind {
            MouseKind::Moved => "moved",
            MouseKind::Pressed => "PRESS",
            MouseKind::Released => "RELEASE",
        };
        println!(
            "{:<7} {:>7.1} {:>7.1} {:>6}  {}",
            k, e.x, e.y, e.t_ms, e.btn.as_str()
        );
    }
    let moves = evts.iter().filter(|e| e.kind == MouseKind::Moved).count();
    let dur = evts.last().map(|e| e.t_ms).unwrap_or(0);
    println!("\n{} move samples, total {dur} ms", moves - 1);
}
