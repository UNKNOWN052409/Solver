//! GhostBrowse TUI — Rust terminal UI browser (ratatui).
//!
//! Layout:
//!   ┌ URL bar (editable) ──────────────┐
//!   ├ page view (human render) ────────┤   j/k scroll, g/G top/bottom
//!   ├ walls/anti-captcha verdict ─────┤   auto panel
//!   └ links [n] — enter number + Enter ┘   Tab focus switch
//!
//! Fetch: ghostmouse get (stealth HTTP) -> GhostEngine parse/render.
//! q quit, r reload, : focus URL, digits+Enter open link.

use std::io;
use std::process::Command;

use crossterm::event::{self, Event, KeyCode, KeyModifiers};
use crossterm::execute;
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListState, Paragraph};
use ratatui::Frame;

use ghostengine::Page;
use ghostengine::anti;
use ghostengine::human;

struct App {
    url: String,
    url_edit: String,
    editing: bool,
    page_text: String,
    verdict_line: String,
    links: Vec<(String, String)>,   // (url, label)
    scroll: u16,
    history: Vec<String>,
    status: String,
}

impl App {
    fn new() -> Self {
        App {
            url: String::new(),
            url_edit: String::new(),
            editing: false,
            page_text: String::new(),
            verdict_line: String::new(),
            links: vec![],
            scroll: 0,
            history: vec![],
            status: "ready — : URL edit, digits+Enter link, q quit".into(),
        }
    }

    fn fetch(&mut self, url: &str) {
        self.status = format!("fetching {url} …");
        // ghostmouse binary — repo root ke target me (tui cwd se 3 upar)
        let gm = [
            "/home/kali/Solver/target/release/ghostmouse",
            "../../target/release/ghostmouse",
            "../target/release/ghostmouse",
        ]
        .iter()
        .find(|p| std::path::Path::new(p).exists())
        .map(|s| s.to_string())
        .unwrap_or_else(|| "ghostmouse".to_string());
        let out = Command::new(gm).args(["get", url]).output();
        match out {
            Ok(o) if o.status.success() => {
                let html = String::from_utf8_lossy(&o.stdout).to_string();
                let page = Page::parse(&html);
                // render + links
                self.page_text = human::render(&page, 90);
                self.links = page.links().into_iter().take(60).collect();
                // anti-captcha verdict
                let v = anti::verdict(&page);
                let techs: Vec<String> = v.walls.iter()
                    .map(|w| format!("{}{}", w.tech.name(),
                        w.sitekey.as_ref().map(|k| format!(":{}", &k[..k.len().min(10)])).unwrap_or_default()))
                    .collect();
                self.verdict_line = if v.walled {
                    format!("⚠ WALLED [{}] {}", techs.join(", "),
                        if v.interstitial { "| interstitial" } else { "" })
                } else {
                    "✓ no walls detected".to_string()
                };
                self.url = url.to_string();
                self.history.push(url.to_string());
                self.scroll = 0;
                self.status = format!("ok — {} links, {}", self.links.len(),
                    if v.walled { "WALLED" } else { "clean" });
            }
            Ok(o) => {
                self.status = format!("fetch failed rc={}", o.status.code().unwrap_or(-1));
            }
            Err(e) => self.status = format!("spawn fail: {e}"),
        }
    }

    fn link_open(&mut self, n: usize) {
        if n == 0 || n > self.links.len() {
            return;
        }
        let href = self.links[n - 1].0.clone();   // clone pehle — borrow khatam
        let abs = human::resolve_url(&self.url, &href);
        self.fetch(&abs);
    }
}

fn main() -> io::Result<()> {
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut term = ratatui::Terminal::new(backend)?;

    let mut app = App::new();
    // start URL CLI arg se
    let start = std::env::args().nth(1);
    if let Some(u) = start {
        app.fetch(&u);
    }

    let mut number_buf = String::new();

    let res = run(&mut term, &mut app, &mut number_buf);

    disable_raw_mode()?;
    execute!(term.backend_mut(), LeaveAlternateScreen)?;
    term.show_cursor()?;
    res
}

fn run<B: ratatui::backend::Backend>(
    term: &mut ratatui::Terminal<B>,
    app: &mut App,
    number_buf: &mut String,
) -> io::Result<()> {
    loop {
        // snapshot UI state — borrow conflict avoid (draw immutable, then mutate)
        {
            let snap_url = app.url.clone();
            let snap_edit = app.url_edit.clone();
            let snap_editing = app.editing;
            let snap_page = app.page_text.clone();
            let snap_verdict = app.verdict_line.clone();
            let snap_links = app.links.clone();
            let snap_status = if number_buf.is_empty() {
                app.status.clone()
            } else {
                format!("open link #{} … (Enter)", number_buf)
            };
            let snap_scroll = app.scroll;
            term.draw(|f| {
                ui(f, &UiSnap {
                    url: snap_url, url_edit: snap_edit, editing: snap_editing,
                    page_text: snap_page, verdict_line: snap_verdict,
                    links: snap_links, status: snap_status, scroll: snap_scroll,
                })
            })?;
        }
        if let Event::Key(k) = event::read()? {
            if app.editing {
                match k.code {
                    KeyCode::Enter => {
                        let u = app.url_edit.trim().to_string();
                        if !u.is_empty() {
                            let full = if u.contains("://") { u } else { format!("https://{u}") };
                            app.fetch(&full);
                        }
                        app.editing = false;
                    }
                    KeyCode::Esc => app.editing = false,
                    KeyCode::Backspace => { app.url_edit.pop(); }
                    KeyCode::Char(c) => app.url_edit.push(c),
                    _ => {}
                }
                continue;
            }
            match k.code {
                KeyCode::Char('q') => return Ok(()),
                KeyCode::Char('c') if k.modifiers.contains(KeyModifiers::CONTROL) => return Ok(()),
                KeyCode::Char(':') => {
                    app.url_edit.clear();       // fresh edit — purana URL append bug fix
                    app.editing = true;
                }
                KeyCode::Char('r') if !app.url.is_empty() => {
                    let u = app.url.clone();
                    app.fetch(&u);
                }
                KeyCode::Char('j') => app.scroll = app.scroll.saturating_add(3),
                KeyCode::Char('k') => app.scroll = app.scroll.saturating_sub(3),
                KeyCode::Char('g') => app.scroll = 0,
                KeyCode::Char('G') => app.scroll = 600,
                KeyCode::Char('b') if !app.history.is_empty() => {
                    // back
                    if app.history.len() > 1 {
                        app.history.pop();
                        let prev = app.history.last().cloned().unwrap_or_default();
                        if !prev.is_empty() { app.fetch(&prev); }
                    }
                }
                KeyCode::Enter if !number_buf.is_empty() => {
                    if let Ok(n) = number_buf.parse::<usize>() {
                        app.link_open(n);
                    }
                    number_buf.clear();
                }
                KeyCode::Backspace => { number_buf.pop(); }
                KeyCode::Char(c) if c.is_ascii_digit() => {
                    number_buf.push(c);
                }
                _ => {}
            }
        }
    }
}

struct UiSnap {
    url: String,
    url_edit: String,
    editing: bool,
    page_text: String,
    verdict_line: String,
    links: Vec<(String, String)>,
    status: String,
    scroll: u16,
}

fn ui(f: &mut Frame, app: &UiSnap) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),   // URL bar
            Constraint::Length(1),   // verdict line
            Constraint::Min(10),     // page
            Constraint::Length(3),  // links
            Constraint::Length(1),  // status
        ])
        .split(f.area());

    // URL bar
    let url_display = if app.editing {
        format!("▶ {}", app.url_edit)
    } else {
        app.url.clone()
    };
    let url_style = Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD);
    f.render_widget(
        Paragraph::new(url_display)
            .style(url_style)
            .block(Block::default().borders(Borders::ALL).title(" URL — : edit, Enter go ")),
        chunks[0],
    );

    // verdict line
    let vstyle = if app.verdict_line.starts_with('⚠') {
        Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)
    } else {
        Style::default().fg(Color::Green)
    };
    f.render_widget(Paragraph::new(app.verdict_line.clone()).style(vstyle), chunks[1]);

    // page view (scrolled)
    let lines: Vec<Line> = app
        .page_text
        .lines()
        .skip(app.scroll as usize)
        .take(chunks[2].height as usize)
        .map(|l| Line::from(Span::raw(l)))
        .collect();
    let scroll_hint = if app.page_text.lines().count() > (app.scroll as usize + chunks[2].height as usize) {
        " ↓ more"
    } else { "" };
    f.render_widget(
        Paragraph::new(lines)
            .block(Block::default().borders(Borders::ALL)
                .title(format!(" PAGE (j/k scroll){scroll_hint} "))),
        chunks[2],
    );

    // links panel
    let mut ls = ListState::default();
    ls.select(None);
    let link_lines: Vec<Line> = app
        .links
        .iter()
        .enumerate()
        .take(chunks[3].height as usize - 2)
        .map(|(i, (href, label))| {
            Line::from(vec![
                Span::styled(format!("[{}] ", i + 1), Style::default().fg(Color::Yellow)),
                Span::raw(if label.is_empty() { href } else { label }),
            ])
        })
        .collect();
    f.render_widget(
        List::new(link_lines)
            .block(Block::default().borders(Borders::ALL).title(" LINKS — number + Enter ")),
        chunks[3],
    );

    // status (number-buffer state snap_status me pehle se baked hai)
    f.render_widget(
        Paragraph::new(app.status.clone()).style(Style::default().fg(Color::Gray)),
        chunks[4],
    );
}
