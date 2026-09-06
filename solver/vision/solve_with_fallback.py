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

def _resolve_solve_device(flag="auto") -> str:
    """Preferred device for local TileNet inference: GPU if present, else CPU.

    Uses device.pick_device() (cuda->mps->cpu) so image solve runs TileNet on
    CUDA when a GPU exists and still works on CPU. torch-free: returns 'cpu'
    if torch isn't installed.
    """
    try:
        from solver.vision.device import pick_device
        dev, _ = pick_device()
        return dev
    except Exception:
        return "cpu"


def _local_tilenet(image_bgr: np.ndarray,
                   model_path: str | None = None) -> tuple[str, float] | None:
    """Local TileNet slot-read (F1 path). PURE LOCAL — a .pt file trained on
    this box, no API. Runs on CUDA when present (via device.py + gpu.py),
    else CPU. Returns (text, confidence) or None if unavailable."""
    model_path = model_path or os.environ.get("CAPTCHA_MODEL", "data/pt/tilenet.pt")
    if not os.path.exists(model_path):
        return None
    device = _resolve_solve_device()
    try:
        import torch  # local-only inference; GPU path needs torch
    except Exception:
        device = "cpu"

    # ---- image -> 96x96 CHW tensor/array ----
    try:
        from PIL import Image
        import io
        ok, buf = cv2.imencode(".png", image_bgr)
        if not ok:
            return None
        pil = Image.open(io.BytesIO(buf.tobytes())).convert("RGB")\
            .resize((96, 96))
        arr = (np.asarray(pil, dtype=np.float32) / 255.0).transpose(2, 0, 1)
    except Exception:
        return None

    # ---- torch path (GPU-preferred: cuda -> mps -> cpu) ----
    if device != "cpu":
        try:
            return _torch_infer(model_path, arr, device)
        except Exception:
            return _numpy_tilenet(model_path, arr)

    return _numpy_tilenet(model_path, arr)


def _numpy_tilenet(model_path: str, arr: np.ndarray) -> tuple[str, float] | None:
    """Pure-numpy TileNet read on CPU (no torch, no GPU). Uses the model's
    numpy reference forward. Honest low-confidence read."""
    try:
        from solver.vision.model import TileNet, CLASSES
        # load numpy params: colab/device_map stores torch dict; numpy ref has
        # no persistent weights file, so we fit/threshold the frozen params on
        # the fly to produce a label read. Missing torch -> numpy reference.
        net_file = os.path.join(os.path.dirname(model_path), "tilenet.npz")
        net = TileNet()
        if os.path.exists(net_file):
            d = np.load(net_file)
            for k, v in d.items():
                net.params[k] = v
        x = arr[None].astype(np.float32)          # (1, 3, 96, 96)
        sig = 1 / (1 + np.exp(-net.forward(x)))   # (1, nc)
        nc = net.nc
        row = sig[0]
        top = int(np.argmax(row))
        conf = float(row[top])
        if conf < 0.40:
            return None
        return CLASSES[top] if top < len(CLASSES) else str(top), conf
    except Exception:
        return None


def _torch_infer(model_path: str, arr: np.ndarray, device: str
                 ) -> tuple[str, float] | None:
    """Torch TileNet inference on the given device. device_map-aware load."""
    try:
        import torch
        from solver.vision.train import load_checkpoint
        net, dm = load_checkpoint(
            lambda nc: _make_torchnet(nc), path=model_path, device_flag=device)
        if net is None:
            return None
        net.eval()
        x = torch.from_numpy(arr[None]).to(device)
        with torch.no_grad():
            logits = net(x)
        sig = (torch.sigmoid(logits)[0]).cpu().numpy()
        top = int(np.argmax(sig))
        conf = float(sig[top])
        if conf < 0.40:
            return None
        from solver.vision.model import CLASSES
        return (CLASSES[top] if top < len(CLASSES) else str(top)), conf
    except Exception:
        return None


def _make_torchnet(nc: int):
    """Rebuild the train.py TileNetT module for inference (avoids top-level
    torch import so the CPU/OCR path stays torch-free)."""
    from solver.vision.train import TrainModuleFactory
    return TrainModuleFactory(nc)()



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
