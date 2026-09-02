"""Tile harvester — demo pages se grid tiles collect + weak-label.

Zero manual labeling: tiles harvest karo (2captcha demo pages infinite
challenges dete hain), CLIP/Qwen-VL se zero-shot weak-label karo, aur
train-ready dataset banao. (Model distill ka torch path train.py me.)

    python -m solver.vision.harvest --demo recaptcha-v2 --out data/tiles --max 50
"""
import argparse
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from ghostrise.engine import GhostSession


def harvest_recaptcha(out_dir, max_grids=50, profile="harvest-v2"):
    """recaptcha-v2 demo: challenge bframe se tiles + prompt nikaalo."""
    os.makedirs(out_dir, exist_ok=True)
    meta = []
    with GhostSession(profile=profile) as g:
        page = g.page("https://2captcha.com/demo/recaptcha-v2")
        time.sleep(6)
        for round_ in range(max_grids):
            try:
                anchor = page.wait_for_selector(
                    "iframe[src*='google.com/recaptcha']", timeout=8000)
                b = anchor.bounding_box()
                page.mouse.click(b["x"] + 14, b["y"] + b["height"] / 2)
                time.sleep(3.5)
            except Exception:
                break
            fr = None
            for f in page.frames:
                if "bframe" in (f.url or ""):
                    fr = f
                    break
            if fr is None:
                break
            try:
                prompt = fr.eval_on_selector(
                    ".rc-imageselect-instructions", "e => e.innerText")
                prompt = " ".join(prompt.split())[:120]
            except Exception:
                prompt = ""
            try:
                imgs = fr.eval_on_selector_all(
                    ".rc-imageselect-tile img, .rc-imageselect-tile",
                    """els => els.map(e => {
                        const im = e.tagName === 'IMG' ? e : e.querySelector('img');
                        return im ? im.src : null;
                    })""")
                imgs = [u for u in imgs if u]
            except Exception:
                imgs = []
            if not imgs:
                break
            grid_id = f"grid_{round_:04d}_{int(time.time())}"
            gdir = os.path.join(out_dir, grid_id)
            os.makedirs(gdir, exist_ok=True)
            for i, src in enumerate(imgs):
                # data: URLs -> file ; http -> page-context download
                if src.startswith("data:"):
                    b64 = src.split(",", 1)[-1]
                    with open(os.path.join(gdir, f"tile_{i:02d}.png"), "wb") as fh:
                        fh.write(base64.b64decode(b64))
                else:
                    r = page.context.request.get(src) if hasattr(page, "context") else None
                    if r and r.ok:
                        with open(os.path.join(gdir, f"tile_{i:02d}.png"), "wb") as fh:
                            fh.write(r.body())
            meta.append({"grid": grid_id, "prompt": prompt, "tiles": len(imgs)})
            print(f"[{round_+1}] {grid_id}: prompt='{prompt[:40]}' tiles={len(imgs)}")
            # reload challenge (new grid) — footer 'reload/new challenge' btn
            try:
                reload_btn = fr.wait_for_selector(
                    "[title='Get a new challenge'], #recaptcha-reload-button",
                    timeout=3000)
                reload_btn.click()
                time.sleep(3.0)
            except Exception:
                # anchor phir se click
                try:
                    anchor = page.wait_for_selector(
                        "iframe[src*='google.com/recaptcha']", timeout=4000)
                    bb = anchor.bounding_box()
                    page.mouse.click(bb["x"] + 14, bb["y"] + bb["height"] / 2)
                    time.sleep(3.5)
                except Exception:
                    break
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print(f"[done] {len(meta)} grids -> {out_dir}")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", default="recaptcha-v2",
                    choices=["recaptcha-v2", "hcaptcha"])
    ap.add_argument("--out", default="data/tiles")
    ap.add_argument("--max", type=int, default=50)
    args = ap.parse_args()
    if args.demo == "recaptcha-v2":
        harvest_recaptcha(args.out, args.max)
    else:
        print("[!] hcaptcha demo widget load nahi hota abhi — recaptcha-v2 use karo")


if __name__ == "__main__":
    main()
