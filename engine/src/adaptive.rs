//! adaptive.rs — GhostEngine ka adaptive-base: system tier detect.
//!
//! Low-spec devices pe bhi browser chale — /proc/meminfo se RAM padho,
//! tier nikaalo, render-flags uske hisaab se. GPU-less phone se 4GB
//! desktop tak ek hi code path.

use std::fs;

/// System tier — RAM ke hisaab se.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Low,  // < 1GB — phone proot / tiny VPS
    Mid,  // 1-4GB — normal laptop range
    High, // 4GB+ — desktop
}

/// Render tuning per tier.
#[derive(Debug, Clone, Copy)]
pub struct RenderFlags {
    pub font_scale: u32,     // glyph pixel size multiplier
    pub antialias: bool,     // High pe on, Low pe off (raster cost)
    pub max_canvas_h: u32,   // Low pe chhota canvas cap (memory)
    pub image_downscale: u32, // 1 = full, 2 = half (Low)
    pub chunked_dom: bool,   // Low pe DOM lazy parse hints
}

impl Tier {
    pub fn as_str(&self) -> &'static str {
        match self {
            Tier::Low => "low",
            Tier::Mid => "mid",
            Tier::High => "high",
        }
    }
    pub fn flags(&self) -> RenderFlags {
        match self {
            Tier::Low => RenderFlags {
                font_scale: 4,    // chhote glyphs — 3x5 hi kaafi
                antialias: false,
                max_canvas_h: 800,
                image_downscale: 2,
                chunked_dom: true,
            },
            Tier::Mid => RenderFlags {
                font_scale: 5,
                antialias: true,
                max_canvas_h: 2400,
                image_downscale: 1,
                chunked_dom: false,
            },
            Tier::High => RenderFlags {
                font_scale: 6, // 3x5 scale-2 jaisa readable
                antialias: true,
                max_canvas_h: 6000,
                image_downscale: 1,
                chunked_dom: false,
            },
        }
    }
    /// RAM-tier detect — /proc/meminfo MemTotal (kB).
    /// Fake/missing: test override ya conservative Mid.
    pub fn detect() -> Tier {
        match fs::read_to_string("/proc/meminfo") {
            Ok(s) => {
                let kb = s
                    .lines()
                    .find(|l| l.starts_with("MemTotal:"))
                    .and_then(|l| l.split_whitespace().nth(1))
                    .and_then(|n| n.parse::<u64>().ok())
                    .unwrap_or(0);
                let gb = kb / 1_048_576;
                if gb < 1 {
                    Tier::Low
                } else if gb < 4 {
                    Tier::Mid
                } else {
                    Tier::High
                }
            }
            Err(_) => Tier::Mid,
        }
    }
    /// Test/mock path — override-able detect.
    pub fn from_mem_kb(kb: u64) -> Tier {
        let gb = kb / 1_048_576;
        if gb < 1 {
            Tier::Low
        } else if gb < 4 {
            Tier::Mid
        } else {
            Tier::High
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tier_from_kb() {
        assert_eq!(Tier::from_mem_kb(500_000), Tier::Low); // ~500MB
        assert_eq!(Tier::from_mem_kb(2 * 1_048_576), Tier::Mid); // 2GB
        assert_eq!(Tier::from_mem_kb(8 * 1_048_576), Tier::High); // 8GB
        // boundaries: exactly 1GB = Mid (Low <1 strict)
        assert_eq!(Tier::from_mem_kb(1_048_576), Tier::Mid);
    }

    #[test]
    fn detect_runs() {
        // is host pe chalna chahiye (proot /proc available)
        let t = Tier::detect();
        assert!(matches!(t, Tier::Low | Tier::Mid | Tier::High));
    }

    #[test]
    fn flags_per_tier() {
        assert!(!Tier::Low.flags().antialias);
        assert!(Tier::High.flags().antialias);
        assert!(Tier::Low.flags().image_downscale > 1);
        assert_eq!(Tier::Mid.flags().image_downscale, 1);
        assert!(Tier::Low.flags().max_canvas_h < Tier::High.flags().max_canvas_h);
    }
}
