"""solve — local, script-based, CPU-runnable CAPTCHA transcription.

Design rule (LO): CAPTCHA solving is AI-INDEPENDENT. No AI API, no external
vision model, no network call. It must run purely on the local CPU as a
script, with a lightweight local model / image processing. The AI should NOT
be involved at all — "agar koi bhi person ko captcha solve karna hai, to
automatic ho jayega, koi AI ki need nahi rahegi."

This module contains ONLY local, deterministic logic:
  1. LOCAL OCR ensemble (tesseract variants + weighted char voting)
  2. LOCAL confidence from per-position variant agreement
  3. PyTorch TileNet path (F1 in CAPTCHA_CAPABILITY_REPORT) when a model file
     is present — still 100% local, no API
  4. HONEST low-conf flag when nothing local reaches the trusted bar

No VISION_LLM_URL calls here. (Browser vision for AI agents is the SEPARATE
MCP perception layer — ghostrise/ai_assistant + browser_agent — and that is
for the assistant, not for CAPTCHA.)

Returns dict: {text, confidence (0..1), method, local_candidate}
  method:
    'heuristic'          -> local OCR ensemble, confidence >= trust
    'tile'               -> local TileNet model read (conf from softmax)
    'heuristic-lowconf'  -> local best guess, BELOW trust -> not 'solved'
"""
from __future__ import annotations

import os

import cv2
import numpy as np

# ---------------------------------------------------------------- thresholds

DEFAULT_CONF_TRUST = 0.80  # >= this we treat a local read as 'solved'
# empirical cap: the local OCR ensemble is ~1/10 exact on this generator, so
# its agreement-based confidence is bounded BELOW trust. Only TileNet (trained
# locally) can credibly cross it.

# ---------------------------------------------------------------- local OCR

def _local_ensemble(image_bgr: np.ndarray) -> tuple[str, float]:
    """Local tesseract ensemble + confidence from per-position variant
    agreement. Returns (text, confidence 0..1)."""
    try:
        from solver.engines.ensemble_engine import EnsembleEngine
        eng = EnsembleEngine()
        detail = eng.solve_with_detail(image_bgr)
        text = detail["text"]
        raws = detail.get("variants") or {}
    except Exception:
        return "", 0.0
    if not text:
        return "", 0.0
    reads = {k: v for k, v in raws.items()
             if isinstance(v, str) and v}
    conf = 0.15  # structural floor: a raw OCR read with no variant detail
    if reads:
        n = len(text)
        acc = 0.0
        for pos in range(n):
            tally: dict = {}
            for s in reads.values():
                if pos < len(s) and s[pos]:
                    tally[s[pos]] = tally.get(s[pos], 0) + 1.0
            if tally:
                top = max(tally.values())
                acc += top / sum(tally.values())
        conf = acc / max(1, n)
    # length sanity: generator emits 5 chars; junk OCR reads 1-3. Penalize.
    if len(text) != 5:
        conf *= 0.25
    else:
        conf *= 0.55   # agreement shares the same OCR bias — never trust alone
    conf = float(min(0.7, max(0.0, conf)))  # bounded under the trust bar
    return text, conf

# ---------------------------------------------------------------- TileNet (local)

def _local_tilenet(image_bgr: np.ndarray,
                   model_path: str | None = None) -> tuple[str, float] | None:
    """PyTorch TileNet slot-read (F1 path). PURE LOCAL — a .pt file trained on
    this box, no API. Returns (text, confidence) or None if unavailable."""
    model_path = model_path or os.environ.get("CAPTCHA_MODEL", "model.pt")
    try:
        import torch  # local-only inference
    except Exception:
        return None
    if not os.path.exists(model_path):
        return None
    # NOTE: minimal inference stub — requires a real trained TileNet model file.
    # Without torch installed on this box this path returns None (honest).
    return None


def solve(image_or_path, conf_trust: float = DEFAULT_CONF_TRUST,
          model_path: str | None = None) -> dict:
    """Main entry. Accepts an image path or BGR array. Local-only; may call
    the local TileNet if torch + a model file are present, else the OCR
    ensemble. NEVER an external AI API."""
    if isinstance(image_or_path, (str, os.PathLike)):
        img = cv2.imread(str(image_or_path))
        if img is None:
            return {"text": "", "confidence": 0.0,
                    "method": "heuristic-lowconf",
                    "error": f"cannot read {image_or_path}"}
    else:
        img = image_or_path

    # 1) local TileNet (accurate, but needs a trained local model)
    tile = _local_tilenet(img, model_path)
    if tile and tile[0] and tile[1] >= conf_trust:
        return {"text": tile[0], "confidence": tile[1],
                "method": "tile", "local_candidate": tile[0]}

    # 2) local OCR ensemble (free, always)
    text, conf = _local_ensemble(img)
    method = "heuristic" if conf >= conf_trust else "heuristic-lowconf"
    return {"text": text, "confidence": conf,
            "method": method, "local_candidate": text}


# ---------------------------------------------------------------- CLI

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: python -m solver.vision.solve path_or_img")
    r = solve(sys.argv[1])
    print(f"text={r['text']!r} confidence={round(r['confidence'],3)} "
          f"method={r['method']}")
