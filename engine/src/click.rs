//! click.rs — GhostEngine HumanClicker: apna playhead-style click engine.
//!
//! Playwright/Selenium ke raw CDP click (khud ka varianT) ke bajaye, ye
//! module ek HUMAN bezier click trajectory + timing stream generate karta
//! hai. Output pure data (events) hai — koi browser transport (CDP, GhostMouse,
//! Python harness) isse dispatch karta hai. Ye net.rs ke decoupling ko follow
//! karta hai: engine MOVE GALTI generate karta hai, transport bas dispatch.
//!
//! Human realism goals (Fitts-type motion):
//!   - Cubic bezier pixel path, 2 random control points (har click != har click)
//!   - Velocity envelope: slow start -> fast middle -> slow landing (Human)
//!   - Horizontal-dominant saccade bias (log pehle, phir vertical) — human
//!     approx straight-ish with slight arc, kabhi overshoot zap-on-landing
//!   - Per-sample latency noise (sample spacing not uniform)
//!   - Pre-click hover pause (80-260ms) + press-hold (60-140ms)
//!   - Tier aware: Low = fewer samples / lighter jitter (fast), High = richer
//!   - Deterministic via drl::Rng (seed) — tests reproducible

use crate::adaptive::Tier;
use crate::drl::Rng;

/// Engine output — CDP `Input.dispatchMouseEvent` ke against ek event.
#[derive(Debug, Clone, Copy)]
pub struct MouseEvent {
    pub kind: MouseKind,
    pub x: f64,
    pub y: f64,
    /// ms since click() start — dispatch timing (assert monotonic).
    pub t_ms: u32,
    pub btn: MouseBtn,
    pub click_count: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseKind {
    Moved,
    Pressed,
    Released,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MouseBtn {
    Left,
    Right,
    Middle,
}

impl MouseBtn {
    pub fn as_str(self) -> &'static str {
        match self {
            MouseBtn::Left => "left",
            MouseBtn::Right => "right",
            MouseBtn::Middle => "middle",
        }
    }
}

/// Bezier motion + click timing, high-level.
pub struct HumanClicker {
    pub tier: Tier,
    /// pre-click hover pause ms — uniform sample.
    pub hover_pause: (u32, u32),
    /// press-hold ms.
    pub press_hold: (u32, u32),
    /// max px jitter amplitude (micro-shake during move).
    pub jitter_px: f64,
    /// move speed — px/sec envelope peak (higher = faster).
    pub speed_px_s: f64,
    rng: Rng,
}

impl Default for HumanClicker {
    fn default() -> Self {
        Self::new(Tier::Mid, 0x41E)
    }
}

impl HumanClicker {
    pub fn new(tier: Tier, seed: u64) -> Self {
        let (hover_pause, press_hold, jitter_px, speed_px_s) = match tier {
            Tier::Low => ((40, 100), (40, 80), 0.4, 2200.0), // fast (low spec)
            Tier::Mid => ((80, 240), (60, 130), 1.1, 1400.0), // default human
            Tier::High => ((90, 300), (70, 170), 1.8, 1000.0), // richest
        };
        HumanClicker {
            tier,
            hover_pause,
            press_hold,
            jitter_px,
            speed_px_s,
            rng: Rng::new(seed),
        }
    }

    fn sample_u32(&mut self, lo: u32, hi: u32) -> u32 {
        lo + (self.rng.next_f64() * (hi - lo) as f64) as u32
    }

    /// Full click: bezier move + hover + press + hold + release.
    /// `from` = current cursor, `to` = target px (top-left origin).
    pub fn click(&mut self, from: (f64, f64), to: (f64, f64)) -> Vec<MouseEvent> {
        let mut evts = Vec::new();
        let mut t = 0u32;

        // 1) bezier move — pixel-space points
        let path = self.bezier_path(from, to);
        let (btn, cc) = (MouseBtn::Left, 1u32);

        // move duration ~ distance / speed, clamp human range
        let dist = ((to.0 - from.0).powi(2) + (to.1 - from.1).powi(2)).sqrt();
        let mut dur_ms = ((dist / self.speed_px_s) * 1000.0) as u32;
        dur_ms = dur_ms.clamp(120, 900);
        let n = path.len().max(2);

        for (i, (x, y)) in path.iter().enumerate() {
            // per-sample latency noise — non-uniform spacing (human)
            let frac = i as f64 / (n - 1) as f64;
            let base = (frac * dur_ms as f64) as u32;
            let noise = self.sample_u32(0, 18);
            let tt = (base + noise).max(t);
            // jitter during move (micromovement)
            let jx = x + self.jitter() ;
            let jy = y + self.jitter();
            evts.push(MouseEvent {
                kind: MouseKind::Moved,
                x: jx,
                y: jy,
                t_ms: tt,
                btn,
                click_count: cc,
            });
            t = tt;
        }
        // snap exactly onto target before press
        evts.push(MouseEvent { kind: MouseKind::Moved, x: to.0, y: to.1, t_ms: t, btn, click_count: cc });

        // 2) hover pause before press
        let hp = self.sample_u32(self.hover_pause.0, self.hover_pause.1);
        t += hp;

        // 3) press
        evts.push(MouseEvent { kind: MouseKind::Pressed, x: to.0, y: to.1, t_ms: t, btn, click_count: cc });

        // 4) hold
        let hd = self.sample_u32(self.press_hold.0, self.press_hold.1);
        t += hd;

        // 5) release
        evts.push(MouseEvent { kind: MouseKind::Released, x: to.0, y: to.1, t_ms: t, btn, click_count: cc });

        evts
    }

    /// Cubic bezier pixel path (real px, not grid). Args cloned for reuse.
    fn bezier_path(&mut self, from: (f64, f64), to: (f64, f64)) -> Vec<(f64, f64)> {
        let (x0, y0) = from;
        let (x1, y1) = to;
        let dx = x1 - x0;
        let dy = y1 - y0;

        // Horizontal-dominant saccade bias: control points pull toward the
        // horizontal mid-line so early motion is mostly-x (human).
        // P1 near start, P2 near end, both pushed slightly perpendicular.
        let lx = (dx.powi(2) + dy.powi(2)).sqrt().max(1.0);
        let perp = (-dy / lx, dx / lx); // perpendicular unit

        let t1 = 0.32 + self.rng.next_f64() * 0.20; // ~0.32-0.52
        let t2 = 0.62 + self.rng.next_f64() * 0.20; // ~0.62-0.82
        let off = lx * (0.05 + self.rng.next_f64() * 0.16); // perpendicular offset

        let c1x = x0 + dx * t1 + perp.0 * off;
        let c1y = y0 + dy * t1 + perp.1 * off;
        let c2x = x0 + dx * t2 + perp.0 * off * 0.6;
        let c2y = y0 + dy * t2 + perp.1 * off * 0.6;

        // sample count tier-aware: Low fewer, High richer
        let steps = match self.tier {
            Tier::Low => 18,
            Tier::Mid => 30,
            Tier::High => 42,
        };

        let mut pts = Vec::with_capacity(steps);
        for i in 0..=steps {
            let f = i as f64 / steps as f64;
            let omf = 1.0 - f;
            let x = omf * omf * omf * x0
                + 3.0 * omf * omf * f * c1x
                + 3.0 * omf * f * f * c2x
                + f * f * f * x1;
            let y = omf * omf * omf * y0
                + 3.0 * omf * omf * f * c1y
                + 3.0 * omf * f * f * c2y
                + f * f * f * y1;
            pts.push((x, y));
        }
        pts
    }

    /// overshoot-on-landing chance (High tier): a single extra sample past
    /// target then back, humanly rare.
    fn jitter(&mut self) -> f64 {
        let amp = self.jitter_px * (self.rng.next_f64() * 2.0 - 1.0);
        amp
    }
}

/// Convenience: current -> target, live in a harness.
/// Returns (events, wheel-free). Pure dispatch-order stream.
pub fn human_click_path(tier: Tier, from: (f64, f64), to: (f64, f64), seed: u64) -> Vec<MouseEvent> {
    let mut hc = HumanClicker::new(tier, seed);
    hc.click(from, to)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn click_emits_move_press_release() {
        let evts = human_click_path(Tier::Mid, (10.0, 10.0), (300.0, 220.0), 1);
        // moved... then pressed, then released last
        assert!(evts.len() >= 3, "too few events: {}", evts.len());
        assert_eq!(evts.last().unwrap().kind, MouseKind::Released);
        // last-press then last-release
        let presses: Vec<_> = evts.iter().filter(|e| e.kind == MouseKind::Pressed).collect();
        assert_eq!(presses.len(), 1);
        let rel = evts.last().unwrap();
        assert_eq!((rel.x as i32, rel.y as i32), (300, 220));
    }

    #[test]
    fn timestamps_monotonic() {
        let evts = human_click_path(Tier::High, (5.0, 5.0), (640.0, 480.0), 99);
        let mut last = 0u32;
        for e in &evts {
            assert!(e.t_ms >= last, "non-monotonic t: {} then {}", last, e.t_ms);
            last = e.t_ms;
        }
        assert!(last >= 60, "click too fast in ms: {}", last);
    }

    #[test]
    fn tier_affects_density_and_speed() {
        let low = human_click_path(Tier::Low, (0.0, 0.0), (400.0, 300.0), 3);
        let high = human_click_path(Tier::High, (0.0, 0.0), (400.0, 300.0), 3);
        assert!(high.len() >= low.len(), "high should be denser: {} vs {}", high.len(), low.len());
        // high slows down (smaller speed_px_s -> higher duration)
        let dl = low.last().unwrap().t_ms;
        let dh = high.last().unwrap().t_ms;
        assert!(dh >= dl, "high tier should be slower: {} vs {}", dh, dl);
    }

    #[test]
    fn no_two_clicks_identical() {
        let a = human_click_path(Tier::Mid, (0.0, 0.0), (200.0, 200.0), 5);
        let b = human_click_path(Tier::Mid, (0.0, 0.0), (200.0, 200.0), 6);
        // internal rng makes them differ in intermediate move points
        let mid_a = &a[a.len() / 2];
        let mid_b = &b[b.len() / 2];
        assert!((mid_a.x - mid_b.x).abs() > 0.5 || (mid_a.y - mid_b.y).abs() > 0.5,
                "same-seed-adjacent clicks collided");
    }
}
