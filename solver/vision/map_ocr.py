"""map_ocr — REAL-data text-captcha OCR wrapper.

`solve_chars(image)` -> 5-char string, using the locally-trained multi-head
CNN (map_cnn, weights at solver/vision/models/map_ocr.pt). 100% local, pure
torch, NO external AI API — satisfies LO's AI-INDEPENDENT rule.

The model was trained ONLY on real captchas (data/real_captchas/grid) with
real-derived augmentation (train_map_ocr.py). Preprocessing matches training:
grayscale + the same CLAHE/normalize path.

Usage:
    from solver.vision.map_ocr import solve_chars, MODEL_PATH
    s = solve_chars(image_bgr_or_path)      # -> "4KTN9"
"""
import os
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_cnn import ALPHABET, NUM_CHARS, NUM_CLASSES, build_model  # noqa: E402

_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
MODEL_PATH = os.path.join(_MODEL_DIR, "map_ocr.pt")

def _preprocess(img_gray):
    """Full-res grayscale -> same normalization the net was trained on.
    Training used plain grayscale /255 (CLAHE only as random aug); to match
    the raw real images at inference, use plain gray /255 (no CLAHE)."""
    return (img_gray / 255.0).astype(np.float32)[None, None]   # (1,1,128,128)


def load_net(path=MODEL_PATH):
    import torch
    if not os.path.exists(path):
        return None
    ckpt = torch.load(path, map_location="cpu")
    net = build_model(seed=0)
    net.load_state_dict(ckpt["model"])
    net.eval()
    return net


_net_cache = {}


def get_net(path=MODEL_PATH):
    if path not in _net_cache:
        _net_cache[path] = load_net(path)
    return _net_cache[path]


def solve_chars(image_or_path, path=MODEL_PATH):
    """image_or_path: BGR ndarray (128x128+) or a path. Returns 5-char string."""
    import torch
    if isinstance(image_or_path, (str, os.PathLike)):
        img = cv2.imread(str(image_or_path))
    else:
        img = image_or_path
    if img is None:
        return ""
    net = get_net(path)
    if net is None:
        return ""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.shape[:2] != (128, 128):
        gray = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
    x = torch.from_numpy(_preprocess(gray))
    with torch.no_grad():
        outs = [o.argmax(1).item() for o in net(x)]
    return "".join(ALPHABET[o] for o in outs)


def solve(image, path=MODEL_PATH):
    """Router-friendly wrapper: returns dict like other solvers."""
    s = solve_chars(image, path=path)
    ok = bool(s)
    return {"type": "text", "method": "map_ocr.cnn (REAL-trained)",
            "result": s, "confidence": (1.0 if ok else 0.0), "text": s}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "/home/kali/NeoSolver")
    from solver.vision.train_map_ocr import GT, DATA
    # evaluate solve_chars on ALL 20 real images with the trained net
    net = get_net()
    if net is None:
        print("no weights found — run train_map_ocr.py first")
        sys.exit(1)
    total = 0; nfull = 0
    for i in range(20):
        p = os.path.join(DATA, f"map_{i:05d}.png")
        pred = solve_chars(p)
        gt = GT[i]
        ca = sum(1 for a, b in zip(pred, gt) if a == b)
        total += ca
        if pred == gt:
            nfull += 1
        print(f"map_{i:05d} pred={pred!r} gt={gt!r} chars={ca}")
    print(f"\nALL-20-REAL: avg_chars={total/20:.3f}  full5_wins={nfull}/20")
    print("(compare: pure-CV ben onion baseline = 0.30 avg chars)")
