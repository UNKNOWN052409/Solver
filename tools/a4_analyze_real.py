#!/usr/bin/env python3
"""D3: analyze every real captcha image -> per-type structured report.
Content/prompt inference, dims, color complexity. NO mock — real pixels."""
import os, json, hashlib
import cv2, numpy as np

ROOT = "/home/kali/NeoSolver/data/real_captchas"
OUT = {}

# --- content heuristics -----------------------------------------------------
def unique_colors(img3):
    # downscale color space for speed: bucket to 5 bits/channel
    q = (img3 >> 3)
    flat = q.reshape(-1, 3)
    return len(np.unique(flat, axis=0))

def edge_frac(gray):
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.sqrt(gx**2 + gy**2)
    return float((mag > 40).mean())

def analyze(path, label):
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        return {"error": "unreadable"}
    h, w = im.shape[:2]
    ch = 1 if im.ndim == 2 else im.shape[2]
    rgb = im if ch >= 3 else cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
    rgb3 = rgb[:, :, :3]
    gray = cv2.cvtColor(rgb3, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(rgb3, cv2.COLOR_BGR2HSV)
    sat = float(hsv[:, :, 1].mean())
    uc = unique_colors(rgb3)
    ef = edge_frac(gray)
    md5 = hashlib.md5(cv2.imencode('.png', im)[1]).hexdigest()
    return {
        "file": os.path.basename(path),
        "label": label,
        "dims": f"{w}x{h}",
        "w": w, "h": h,
        "channels": ch,
        "gray_min": int(gray.min()), "gray_max": int(gray.max()),
        "gray_mean": round(float(gray.mean()), 1),
        "gray_std": round(float(gray.std()), 1),
        "flat_unique_colors": uc,
        "mean_saturation": round(sat, 1),
        "edge_frac": round(ef, 4),
        "md5": md5[:16],
    }

def bucket(dirname):
    d = os.path.join(ROOT, dirname)
    items = []
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        if f.startswith('.'):
            continue
        items.append(analyze(p, dirname))
    return items

for d in ["grid", "rot", "slider", "hcaptcha"]:
    OUT[d] = bucket(d)

# summary
print(json.dumps(OUT, indent=2))

total = sum(len(v) for v in OUT.values())
print("\nTOTAL real images analyzed:", total)
from collections import Counter
print("per-dir:", {k: len(v) for k, v in OUT.items()})
