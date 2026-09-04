//! GhostEngine anti-captcha layer — DOM-level wall detection.
//!
//! Engine khud page me captcha/wall patterns dhoondhta hai (script
//! srcs, iframes, hidden challenge inputs, meta-refresh interstitial)
//! aur structured verdict deta hai: {tech, sitekey, widget_el}.
//! Solve routing (captcha_agent / solver API) upper layer karta hai —
//! ye layer DETECT + hint extract karti hai, engine-level fast.

use crate::{Node, Page};

#[derive(Debug, Clone, PartialEq)]
pub enum CapTech {
    RecaptchaV2,
    RecaptchaEnterprise,
    Hcaptcha,
    Turnstile,
    AwsWaf,
    GeeTest,
    FunCaptcha,
    DataDome,
    Slider,
    TextImage,
    CloudflareManaged,
    Interstitial, // meta-refresh / just-a-moment class walls
}

impl CapTech {
    pub fn name(&self) -> &'static str {
        match self {
            CapTech::RecaptchaV2 => "recaptcha-v2",
            CapTech::RecaptchaEnterprise => "recaptcha-enterprise",
            CapTech::Hcaptcha => "hcaptcha",
            CapTech::Turnstile => "turnstile",
            CapTech::AwsWaf => "aws-waf",
            CapTech::GeeTest => "geetest",
            CapTech::FunCaptcha => "funcaptcha",
            CapTech::DataDome => "datadome",
            CapTech::Slider => "slider",
            CapTech::TextImage => "text-image",
            CapTech::CloudflareManaged => "cf-managed-challenge",
            CapTech::Interstitial => "interstitial",
        }
    }
    /// keyless solvable (ghostrise stack) ya external chahiye
    pub fn keyless(&self) -> bool {
        matches!(
            self,
            CapTech::RecaptchaV2 | CapTech::TextImage | CapTech::Slider
        )
    }
}

#[derive(Debug, Clone)]
pub struct WallInfo {
    pub tech: CapTech,
    pub sitekey: Option<String>,
    pub gt_key: Option<String>,     // geetest
    pub public_key: Option<String>, // funcaptcha
    pub hint: String,               // widget selector/data hint
}

/// Page pe captcha tech scan — DOM traversal, zero network.
pub fn detect(page: &Page) -> Vec<WallInfo> {
    let mut out = Vec::new();
    walk(&page.root, &mut out);
    // de-dup by tech — LATEST (sitekey-carrying) wall jeete:
    // same tech ke multiple walls me jo sitekey rakhta hai wo rakho.
    let mut best: Vec<WallInfo> = Vec::new();
    for w in out {
        match best.iter().position(|b| b.tech == w.tech) {
            Some(i) => {
                let cur = &mut best[i];
                if cur.sitekey.is_none() && w.sitekey.is_some() {
                    cur.sitekey = w.sitekey.clone();
                }
                if cur.gt_key.is_none() && w.gt_key.is_some() {
                    cur.gt_key = w.gt_key.clone();
                }
                if cur.public_key.is_none() && w.public_key.is_some() {
                    cur.public_key = w.public_key.clone();
                }
                if cur.hint.len() < w.hint.len() {
                    cur.hint = w.hint.clone();
                }
            }
            None => best.push(w),
        }
    }
    best
}

fn walk(n: &Node, out: &mut Vec<WallInfo>) {
    let tag = n.tag.as_deref().unwrap_or("");
    let src = n.attr("src").unwrap_or("");
    let cls = n.attr("class").unwrap_or("");
    let data_sitekey = n.attr("data-sitekey");

    // <script src=...> + iframe srcs — primary detectors
    if tag == "script" || tag == "iframe" {
        let s = src.to_ascii_lowercase();
        let full = src.to_string();
        if s.contains("recaptcha") && s.contains("enterprise") {
            out.push(WallInfo {
                tech: CapTech::RecaptchaEnterprise,
                sitekey: data_sitekey.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("recaptcha") {
            out.push(WallInfo {
                tech: CapTech::RecaptchaV2,
                sitekey: data_sitekey.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("hcaptcha") {
            out.push(WallInfo {
                tech: CapTech::Hcaptcha,
                sitekey: data_sitekey.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("challenges.cloudflare.com") {
            out.push(WallInfo {
                tech: CapTech::Turnstile,
                sitekey: data_sitekey.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("challenge.amazonaws") {
            out.push(WallInfo {
                tech: CapTech::AwsWaf,
                sitekey: None,
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("geetest") {
            out.push(WallInfo {
                tech: CapTech::GeeTest,
                sitekey: None,
                gt_key: n.attr("data-gt").map(|g| g.to_string()),
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("funcaptcha") || s.contains("arkoselabs") {
            out.push(WallInfo {
                tech: CapTech::FunCaptcha,
                sitekey: None,
                gt_key: None,
                public_key: data_sitekey.map(|k| k.to_string()),
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        } else if s.contains("datadome") {
            out.push(WallInfo {
                tech: CapTech::DataDome,
                sitekey: None,
                gt_key: None,
                public_key: None,
                hint: format!("script/iframe: {}", &full[..full.len().min(60)]),
            });
        }
    }
    // turnstile widget div (.cf-turnstile) + recaptcha/hcaptcha widget divs
    if tag == "div" {
        let sk = n.attr("data-sitekey");
        if cls.contains("cf-turnstile") {
            out.push(WallInfo {
                tech: CapTech::Turnstile,
                sitekey: sk.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: "div.cf-turnstile".into(),
            });
        } else if cls.contains("g-recaptcha") {
            out.push(WallInfo {
                tech: CapTech::RecaptchaV2,
                sitekey: sk.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: "div.g-recaptcha".into(),
            });
        } else if cls.contains("h-captcha") || cls.contains("hcaptcha") {
            out.push(WallInfo {
                tech: CapTech::Hcaptcha,
                sitekey: sk.map(|k| k.to_string()),
                gt_key: None,
                public_key: None,
                hint: "div.h-captcha".into(),
            });
        }
    }
    // hidden challenge-response inputs (managed mode markers)
    if tag == "input" {
        let name = n.attr("name").unwrap_or("");
        if name == "cf-turnstile-response"
            || name == "g-recaptcha-response"
            || name == "h-captcha-response"
            || name == "arkose_token"
        {
            let tech = if name.starts_with("cf-") {
                CapTech::CloudflareManaged
            } else if name.starts_with("g-") {
                CapTech::RecaptchaV2
            } else if name.starts_with("h-") {
                CapTech::Hcaptcha
            } else {
                CapTech::FunCaptcha
            };
            out.push(WallInfo {
                tech,
                sitekey: None,
                gt_key: None,
                public_key: None,
                hint: format!("hidden input: {}", name),
            });
        }
    }
    // meta-refresh interstitial
    if tag == "meta" {
        let http = n.attr("http-equiv").unwrap_or("");
        let content = n.attr("content").unwrap_or("");
        if http.eq_ignore_ascii_case("refresh") && content.contains("captcha") {
            out.push(WallInfo {
                tech: CapTech::Interstitial,
                sitekey: None,
                gt_key: None,
                public_key: None,
                hint: format!("meta-refresh: {}", &content[..content.len().min(50)]),
            });
        }
    }
    // classic text/image captcha (img with captcha hint in src/alt)
    if tag == "img" {
        let hay = format!("{} {}", src, n.attr("alt").unwrap_or("")).to_ascii_lowercase();
        if hay.contains("captcha") && !hay.contains("recaptcha") {
            out.push(WallInfo {
                tech: CapTech::TextImage,
                sitekey: None,
                gt_key: None,
                public_key: None,
                hint: format!("img: {}", &src[..src.len().min(50)]),
            });
        }
    }
    // slider pattern (class heuristics)
    if cls.contains("slider") && (cls.contains("captcha") || cls.contains("verify")) {
        out.push(WallInfo {
            tech: CapTech::Slider,
            sitekey: None,
            gt_key: None,
            public_key: None,
            hint: format!("class: {}", cls),
        });
    }

    for c in &n.children {
        walk(c, out);
    }
}

/// Full page verdict: kya wall hai? konsi tech? sab structured.
#[derive(Debug, Clone)]
pub struct Verdict {
    pub walled: bool,
    pub interstitial: bool,
    pub walls: Vec<WallInfo>,
}

pub fn verdict(page: &Page) -> Verdict {
    let walls = detect(page);
    let title = page.title().to_ascii_lowercase();
    let text = page.text().to_ascii_lowercase();
    let interstitial = title.contains("just a moment")
        || title.contains("attention required")
        || text.contains("verifying you are human")
        || text.contains("checking your browser");
    crate::anti::Verdict {
        walled: !walls.is_empty() || interstitial,
        interstitial,
        walls,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Page;

    #[test]
    fn detect_all_techs() {
        let html = "<html><head><title>Join</title></head><body>\
            <script src='https://www.google.com/recaptcha/api.js'></script>\
            <div class='g-recaptcha' data-sitekey='KEY123'></div>\
            <iframe src='https://newassets.hcaptcha.com/captcha/v1/abc'></iframe>\
            <div class='cf-turnstile' data-sitekey='TS456'></div>\
            <script src='https://static.geetest.com/gt.js'></script>\
            <img src='/captcha/gen.png' alt='captcha code'>\
            <input type='hidden' name='cf-turnstile-response'>\
            </body></html>";
        let p = Page::parse(html);
        let d = detect(&p);
        let names: Vec<&str> = d.iter().map(|w| w.tech.name()).collect();
        for expect in [
            "recaptcha-v2",
            "hcaptcha",
            "turnstile",
            "geetest",
            "text-image",
            "cf-managed-challenge",
        ] {
            assert!(names.contains(&expect), "missing {} in {:?}", expect, names);
        }
        // sitekey extraction — script wala + div wala dono me se koi ek
        // (dedup tech-level pe hota hai, div ka sitekey ya script ka)
        let rc_keys: Vec<Option<&str>> = d
            .iter()
            .filter(|w| w.tech == CapTech::RecaptchaV2)
            .map(|w| w.sitekey.as_deref())
            .collect();
        assert!(
            rc_keys.contains(&Some("KEY123")),
            "no KEY123 in {:?}",
            rc_keys
        );
        let ts_keys: Vec<Option<&str>> = d
            .iter()
            .filter(|w| w.tech == CapTech::Turnstile)
            .map(|w| w.sitekey.as_deref())
            .collect();
        assert!(
            ts_keys.contains(&Some("TS456")),
            "no TS456 in {:?}",
            ts_keys
        );
    }

    #[test]
    fn interstitial_verdict() {
        let html = "<title>Just a moment...</title><div>Verifying you are human. This may take a few seconds.</div>";
        let p = Page::parse(html);
        let v = verdict(&p);
        assert!(v.walled);
        assert!(v.interstitial);
    }

    #[test]
    fn clean_page_no_wall() {
        let html = "<title>Home</title><p>Welcome to the shop. Items daily.</p>";
        let p = Page::parse(html);
        assert!(!verdict(&p).walled);
    }
}
