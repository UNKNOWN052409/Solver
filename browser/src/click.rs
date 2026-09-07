//! Comet — REAL mouse-click synthesis via the `enigo` crate.
//!
//! Emits genuine OS-level mouse events (move + click) on the host display.
//! On Linux uses X11 (x11rb) / Wayland; on Windows/macOS uses the native API.
//! `comet click <x> <y> [--button left|right|middle] [--double]`.
//!
//! NOTE: this is the real input path — it requires an interactive display/session
//! (X11/Wayland) to actually move the pointer. On a headless box without a
//! display it errors out honestly (it does not fake a click). The browser uses
//! this to drive real user-like interaction (human mouse) on the desktop UI.

use enigo::{Button, Coordinate, Direction, Enigo, Mouse, Settings};

pub fn run(args: &[String]) {
    // parse: click <x> <y> [--button left|right|middle] [--double]
    if args.len() < 2 {
        eprintln!("comet click wants <x> <y> [--button left|right|middle] [--double]");
        return;
    }
    let x: i32 = args[0].parse().unwrap_or(0);
    let y: i32 = args[1].parse().unwrap_or(0);
    let mut button = Button::Left;
    let mut double = false;
    let mut i = 2;
    while i < args.len() {
        match args[i].as_str() {
            "--button" => {
                if i + 1 < args.len() {
                    button = match args[i + 1].as_str() {
                        "right" => Button::Right,
                        "middle" => Button::Middle,
                        _ => Button::Left,
                    };
                    i += 2;
                    continue;
                }
            }
            "--double" => {
                double = true;
                i += 1;
                continue;
            }
            _ => {}
        }
        i += 1;
    }

    // Real mouse event via enigo.
    match Enigo::new(&Settings::default()) {
        Ok(mut e) => {
            if e.move_mouse(x, y, Coordinate::Abs).is_err() {
                eprintln!("[comet click] failed to move mouse to ({x},{y}) — no display?");
                return;
            }
            if e.button(button, Direction::Click).is_err() {
                eprintln!("[comet click] failed to click — no display?");
                return;
            }
            if double {
                // second click after a tiny pause for a double-click
                std::thread::sleep(std::time::Duration::from_millis(40));
                let _ = e.button(button, Direction::Click);
            }
            println!("[comet click] moved to ({x},{y}) and clicked {:?}", button);
        }
        Err(err) => {
            eprintln!(
                "[comet click] cannot create input engine: {err} — needs a real display \
                 (X11/Wayland). This box may be headless; no fake click emitted."
            );
        }
    }
}
