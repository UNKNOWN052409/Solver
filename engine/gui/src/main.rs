//! GhostBrowse GUI — Brave-style desktop browser (egui).
//!
//! Chrome: tab strip (multiple tabs, close buttons), nav bar (back/
//! reload), address bar (editable), walls badge (red WALLED / green
//! clean), page view (scrollable readable text), links sidebar
//! (clickable), status bar. Engine: GhostMouse fetch -> GhostEngine
//! parse/render -> anti verdict. Software rendering (softbuffer path
//! via eframe x11) — GPU nahi chahiye.

use eframe::egui;
use egui::{Color32, RichText};

use ghostengine::Page;
use ghostengine::anti;
use ghostengine::human;

// ---------------------------------------------------------------- tab --
struct Tab {
    url: String,
    addr_edit: String,
    page_text: String,
    walls: Vec<String>,
    walled: bool,
    links: Vec<(String, String)>,
    scroll: f32,
    status: String,
    history: Vec<String>,
}

impl Tab {
    fn new(url: &str) -> Self {
        let mut t = Tab {
            url: String::new(),
            addr_edit: String::new(),
            page_text: String::new(),
            walls: vec![],
            walled: false,
            links: vec![],
            scroll: 0.0,
            status: String::new(),
            history: vec![],
        };
        t.navigate(url);
        t
    }

    fn navigate(&mut self, url: &str) {
        self.status = format!("Loading {url}…");
        let gm = [
            "/home/kali/Solver/target/release/ghostmouse",
            "../../target/release/ghostmouse",
        ]
        .iter()
        .find(|p| std::path::Path::new(p).exists())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "ghostmouse".to_string());
        match std::process::Command::new(gm).args(["get", url]).output() {
            Ok(o) if o.status.success() => {
                let html = String::from_utf8_lossy(&o.stdout).to_string();
                let page = Page::parse(&html);
                self.page_text = human::render(&page, 100);
                self.links = page.links().into_iter().take(80).collect();
                let v = anti::verdict(&page);
                self.walled = v.walled;
                self.walls = v.walls.iter().map(|w| w.tech.name().to_string()).collect();
                self.status = if v.walled {
                    format!("Walled: {}", self.walls.join(", "))
                } else {
                    format!("Done — {} links", self.links.len())
                };
            }
            Ok(o) => self.status = format!("Load failed rc={}", o.status.code().unwrap_or(-1)),
            Err(e) => self.status = format!("Error: {e}"),
        }
        self.url = url.to_string();
        self.addr_edit = url.to_string();
        self.history.push(url.to_string());
        self.scroll = 0.0;
    }

    fn open_link(&mut self, n: usize) {
        if n == 0 || n > self.links.len() {
            return;
        }
        let href = self.links[n - 1].0.clone();
        let abs = human::resolve_url(&self.url, &href);
        self.navigate(&abs);
    }
}

struct App {
    tabs: Vec<Tab>,
    active: usize,
}

impl Default for App {
    fn default() -> Self {
        let start = std::env::args().nth(1)
            .unwrap_or_else(|| "https://example.com".to_string());
        App { tabs: vec![Tab::new(&start)], active: 0 }
    }
}

impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        let mut close_tab = None;
        let mut switch_tab = None;

        // ---------------- top bar: [logo] [tabs x N] [+] --------------
        egui::TopBottomPanel::top("chrome").show(ctx, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new("◉").size(18.0).color(Color32::from_rgb(120, 90, 240)));
                ui.separator();
                for (i, t) in self.tabs.iter().enumerate() {
                    let title: String = {
                        let raw = t.url.trim_start_matches("https://")
                            .trim_start_matches("http://");
                        let host = raw.split('/').next().unwrap_or(raw);
                        host.chars().take(18).collect()
                    };
                    let is_active = i == self.active;
                    let tab_btn = ui.selectable_label(is_active, format!(" {} ", title));
                    if tab_btn.clicked() {
                        switch_tab = Some(i);
                    }
                    // close X
                    let x = ui.small_button("✕");
                    if x.clicked() {
                        close_tab = Some(i);
                    }
                }
                if ui.small_button("+").clicked() {
                    self.tabs.push(Tab::new("https://example.com"));
                    self.active = self.tabs.len() - 1;
                }
            });
        });

        // ---------------- nav + address bar ----------------------------
        let mut go_url: Option<String> = None;
        let mut nav_back = false;
        let mut nav_reload = false;

        egui::TopBottomPanel::top("navbar").show(ctx, |ui| {
            ui.horizontal(|ui| {
                if ui.button("←").clicked() {
                    nav_back = true;
                }
                if ui.button("⟳").clicked() {
                    nav_reload = true;
                }
                let t = self.tabs.get_mut(self.active).unwrap();
                ui.add(egui::TextEdit::singleline(&mut t.addr_edit)
                    .desired_width(ui.available_width() - 90.0)
                    .hint_text("Search or enter address"));
                if ui.small_button("Go ⏎").clicked() {
                    let u = t.addr_edit.trim().to_string();
                    if !u.is_empty() {
                        let full = if u.contains("://") { u } else { format!("https://{u}") };
                        go_url = Some(full);
                    }
                }
                // walls badge
                if t.walled {
                    ui.label(RichText::new(format!("⚠ {}", t.walls.join(",")))
                        .strong().color(Color32::from_rgb(220, 50, 50)));
                } else {
                    ui.label(RichText::new("✓").color(Color32::from_rgb(50, 180, 90)));
                }
            });
        });

        // ---------------- status bar -----------------------------------
        egui::TopBottomPanel::bottom("status").show(ctx, |ui| {
            ui.horizontal(|ui| {
                let t = &self.tabs[self.active];
                ui.label(RichText::new(&t.status).small().color(Color32::GRAY));
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    ui.label(RichText::new("GhostEngine ◉ Rust").small().color(Color32::from_rgb(120, 90, 240)));
                });
            });
        });

        // ---------------- main: page view + links sidebar ---------------
        let has_links = self.tabs[self.active].links.len() > 0;
        egui::SidePanel::right("links")
            .default_width(220.0)
            .show_animated(ctx, has_links, |ui| {
            ui.heading("Links");
            let mut open = None;
            let links = self.tabs[self.active].links.clone();
            for (i, (href, label)) in links.iter().enumerate() {
                let txt = if label.is_empty() { href } else { label };
                let txt: String = txt.chars().take(24).collect();
                if ui.selectable_label(false, format!("[{}] {}", i + 1, txt)).clicked() {
                    open = Some(i + 1);
                }
            }
            if let Some(n) = open {
                self.tabs.get_mut(self.active).unwrap().open_link(n);
            }
        });

        egui::CentralPanel::default().show(ctx, |ui| {
            let t = self.tabs.get_mut(self.active).unwrap();
            egui::ScrollArea::vertical().show(ui, |ui| {
                // readable page text — headings/l-links styled
                for line in t.page_text.lines() {
                    let trimmed = line.trim();
                    if trimmed.starts_with("◈") {
                        ui.label(RichText::new(line).heading().strong());
                    } else if trimmed.starts_with('[') {
                        ui.label(RichText::new(line).color(Color32::from_rgb(70, 130, 240)));
                    } else if trimmed.starts_with("▭") || trimmed.starts_with("⬡") {
                        ui.label(RichText::new(line).weak());
                    } else {
                        ui.label(line);
                    }
                }
            });
        });

        // actions after panels (borrow safety)
        if let Some(i) = close_tab {
            if self.tabs.len() > 1 {
                self.tabs.remove(i);
                if self.active >= self.tabs.len() {
                    self.active = self.tabs.len() - 1;
                }
            }
        }
        if let Some(i) = switch_tab {
            self.active = i;
        }
        if nav_back {
            let t = self.tabs.get_mut(self.active).unwrap();
            if t.history.len() > 1 {
                t.history.pop();
                let prev = t.history.last().cloned().unwrap_or_default();
                if !prev.is_empty() {
                    t.navigate(&prev);
                }
            }
        }
        if nav_reload {
            let u = self.tabs[self.active].url.clone();
            self.tabs.get_mut(self.active).unwrap().navigate(&u);
        }
        if let Some(u) = go_url {
            self.tabs.get_mut(self.active).unwrap().navigate(&u);
        }
    }
}

fn main() -> eframe::Result<()> {
    let opts = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([1024.0, 700.0])
            .with_title("GhostBrowse — Rust Engine Browser"),
        ..Default::default()
    };
    eframe::run_native("GhostBrowse", opts, Box::new(|_cc| Ok(Box::new(App::default()))))
}
