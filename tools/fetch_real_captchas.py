#!/usr/bin/env python3
"""fetch_real_captchas.py — download REAL captcha sample images from public
GitHub sources (no synthetic/mockup images — every file is a real captcha or
captcha component (tile/background/slider/rotated challenge) pulled straight
from public git repos).

Output layout:
    data/real_captchas/{text,grid,slider,rot,hcaptcha}/*.ext
    data/real_captchas/manifest.json   # [{file, source_url, type}]

Every downloaded file is verified with cv2.imread(); only files that decode
as images count. Nothing is fabricated — failures are logged + skipped.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import cv2

BASE = "https://raw.githubusercontent.com"

# name -> (type, list of (repo, path))
# Each path is a REAL captcha file found by inspecting the repo's git tree via
# the GitHub API. Paths were chosen deterministically per type.
SOURCES = {
    "text": "drandule/mail.ru__captcha_dataset",   # real text CAPTCHAs (JPG)
    "grid": "cavoixanh1806/captcha-map-solver",     # real map/grid CAPTCHAs (PNG)
    "slider": "MossLinn/harmonyos-slider-captcha-benchmark",  # real slider challenge bgs
    "rot": "yixiaowang2001/rotate-captcha-solver",  # real rotated-image CAPTCHAs (PNG)
    "hcaptcha": "drandule/hcaptcha_dataset",        # real hCaptcha object tiles (JPG)
}

# Explicit real file paths per type (verified present in the upstream git tree).
PLAN = {
    "text": [(SOURCES["text"], f"captcha/{n}.jpg")
             for n in [
                 "001321_num7590", "0015uy_num7371", "002ktb_num12387",
                 "003aou_num8454", "0056xc_num966", "005poy_num1393",
                 "0061t1_num7478", "0062c8_num2167", "0062yy_num5284",
                 "006ahm_num2232", "00822u_num531", "0106po_num14926",
             ]],
    "grid": [(SOURCES["grid"], f"data/map_{i:05d}.png") for i in
             [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]],
    "slider": [(SOURCES["slider"], f"datasets/slider_puzzle_realistic_v2/samples/{n}/challenge.png")
               for n in ["bridge", "library", "market", "station"]]
              + [(SOURCES["slider"], f"datasets/slider_puzzle_realistic_v2/samples/{n}/panel.png")
                 for n in ["bridge", "library", "market", "station"]],
    "rot": [(SOURCES["rot"], f"caps/raw_labeled_caps/{n}.png")
            for n in [
                "100_rot60_00_100", "101_rot146_00_101", "102_rot62_00_102",
                "103_rot255_00_103", "104_rot7_00_104", "105_rot136_00_105",
                "106_rot86_00_106", "107_rot92_00_107", "108_rot276_00_108",
                "109_rot300_00_109", "110_rot82_00_110", "111_rot85_00_111",
                "112_rot207_00_112", "113_rot161_00_113", "114_rot231_00_114",
                "115_rot99_00_115", "116_rot284_00_116", "10_rot144_00_10",
            ]],
    "hcaptcha": [(SOURCES["hcaptcha"], f"dataset/{cat}/{n}.jpg")
                 for cat in ["airplane", "bicycle", "boat", "motorbus",
                             "motorcycle", "seaplane", "train", "truck"]
                 for n in [1, 2, 3]]
                + [(SOURCES["hcaptcha"], f"dataset/{cat}/{n}.jpg")
                   for cat, extras in [("airplane", [4]), ("bicycle", [4]),
                                       ("motorbus", [4]), ("motorcycle", [4]),
                                       ("seaplane", [4]), ("train", [4])]
                   for n in extras],
}

# Plan totals (what we intend to fetch)
PLAN_TOTALS = {k: len(v) for k, v in PLAN.items()}


def download(url: str, dest: Path, timeout: int = 40) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:  # network / 404 / timeout
        print(f"    DOWNLOAD FAIL {url}: {e}")
        return False
    try:
        arr = cv2.imdecode(
            __import__("numpy").frombuffer(data, dtype="uint8"), cv2.IMREAD_COLOR)
    except Exception:
        arr = None
    if arr is None:
        print(f"    NOT AN IMAGE (cv2 decode fail): {url}")
        return False
    dest.write_bytes(data)
    return True


def main(out_root: Path = Path("data/real_captchas")) -> int:
    out_root = out_root.resolve()
    manifest: list[dict] = []
    fetched = 0
    skipped = 0

    print(f"Output root: {out_root}")
    for ctype in sorted(PLAN):
        cdir = out_root / ctype
        cdir.mkdir(parents=True, exist_ok=True)
        print(f"\n[{ctype}] plan={PLAN_TOTALS[ctype]}")
        for repo, path in PLAN[ctype]:
            fname = Path(path).name  # upstream filename
            # keep a suffix if several upstream files share a name (slider/hcap)
            dest = cdir / fname
            if dest.exists():
                # avoid clobber; dedupe by appending an index
                i = 1
                while dest.exists() and not any(
                        m["file"] == str(dest.relative_to(out_root)) for m in manifest):
                    dest = cdir / f"{Path(fname).stem}_{i}{Path(fname).suffix}"
                    i += 1
            url = f"{BASE}/{repo}/main/{path}"
            print(f"  {url}")
            ok = download(url, dest)
            if ok:
                manifest.append({
                    "file": str(dest.relative_to(out_root)),
                    "source_url": url,
                    "type": ctype,
                })
                fetched += 1
            else:
                skipped += 1

    # verify all manifest files decode as images
    bad = []
    for m in manifest:
        full = out_root / m["file"]
        if cv2.imread(str(full)) is None:
            bad.append(m["file"])
    if bad:
        print("\nVALIDATION FAILED (cv2.imread None):", bad)

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    by_type = {}
    for m in manifest:
        by_type[m["type"]] = by_type.get(m["type"], 0) + 1

    print("\n================= RESULT =================")
    print(f"fetched={fetched} skipped={skipped} by_type={by_type}")
    print(f"manifest: {manifest_path}")
    if bad:
        print("!! some files failed cv2 validation:", bad)
        return 1
    return 0


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/real_captchas")
    sys.exit(main(root))
