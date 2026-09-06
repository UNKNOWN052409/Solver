"""solve_with_fallback — confidence-guarded image CAPTCHA transcription.

Keyless-by-default pipeline (ARCHITECTURE / CAPTCHA_CAPABILITY_REPORT.md):

  1. LOCAL (always, free): the tesseract ensemble drifts a candidate +
     a per-character confidence derived from how many variants agreed at
     each position. High agreement  -> high confidence -> we're done.
  2. VISION-LLM (when local confidence is low): hand the PNG (base64) to a
     configurable multimodal endpoint ("transcribe EXACTLY this distorted
     text, output only the text") and return its token.
  3. HONEST FALLBACK: if the vision endpoint is unreachable, we return the
     local candidate with method='heuristic-lowconf' and a confidence below
     the trusted bar — the CALLER knows it's unverified and must not treat
     it as solved.

No false "solved" claims: a result is only method='vision' or
method='heuristic' when confidence crosses the trusted threshold, else it is
explicitly flagged lowconf.

Wiring:
    VISION_LLM_URL    e.g. http://127.0.0.1:11434/v1/chat/completions
                      OR any OpenAI-compatible /chat/completions
                      (used as-is; a bare host http://h:port gets /v1 appended)
    VISION_LLM_MODEL  e.g. qwen2.5vl:7b / llava / gpt-4o
    VISION_LLM_KEY    optional Bearer token for OpenAI-compatible services

Returns dict:
    {text, confidence (0..1), method:
       'heuristic' | 'vision' | 'heuristic-lowconf',
     local_candidate, error?}
"""
from __future__ import annotations

import base64
import os

import cv2
import numpy as np

# ---------------------------------------------------------------- pipeline

DEFAULT_CONF_TRUST = 0.72   # >= this we trust a local read
DEFAULT_CONF_BAR = 0.45     # above this local but under trust -> prefer vision


def _base64_png(bgr: np.ndarray) -> str:
    """Encode BGR array as base64 PNG for a vision model."""
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return base64.b64encode(buf.tobytes()).decode()


def _vision_url() -> str | None:
    u = os.environ.get("VISION_LLM_URL", "").strip().rstrip("/")
    if not u:
        return None
    # OpenAI-compatible: ensure a chat/completions path
    if u.endswith("/chat/completions"):
        return u
    return u + ("/v1/chat/completions" if "/v1" not in u else "/chat/completions")


def _vision_call(png_b64: str) -> tuple[str | None, bool]:
    """Call the configured vision LLM. Returns (token, ok). Never raises to
    the solve path — unreachable/timeout -> (None, False)."""
    url = _vision_url()
    model = os.environ.get("VISION_LLM_MODEL", "qwen2.5vl:7b")
    key = os.environ.get("VISION_LLM_KEY", "")
    if not url:
        return None, False
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": (
                        "This is a distorted CAPTCHA. Transcribe EXACTLY the "
                        "letters and digits, lowercase only. Output ONLY the "
                        "text, no explanation, no quotes, no spaces.")},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{png_b64}"}},
                ]},
            ],
            "max_tokens": 24,
            "temperature": 0,
        }
        r = requests.post(url, json=payload, headers=headers, timeout=45)
        if r.status_code != 200:
            return None, False
        data = r.json()
        tok = data["choices"][0]["message"]["content"]
        tok = (tok or "").strip().lower()
        import re
        tok = re.sub(r"[^a-z0-9]", "", tok)
        return tok or None, True
    except Exception:
        return None, False


def _local_confident(image_bgr: np.ndarray
                     ) -> tuple[str, float]:
    """Local ensemble + a confidence score (0..1) from per-position vote
    agreement. High agreement + sane length => high confidence.

    Returns (text, confidence)."""
    try:
        from solver.engines.ensemble_engine import EnsembleEngine
        eng = EnsembleEngine()
        detail = eng.solve_with_detail(image_bgr)
        text = detail["text"]
        raw = detail.get("variants") or {}
        raws = raw if isinstance(raw, dict) else {}
    except Exception as e:  # ensemble/tesseract absent
        return "", 0.0

    # rebuild per-position vote agreement from the variant strings.
    # EnsembleEngine weights by (prep,psm); we approximate with the raw reads.
    if not text:
        return "", 0.0

    reads = {k: v for k, v in raws.items() if isinstance(v, str) and v}
    if not reads:
        # no variant detail -> fall back to a structural confidence floor
        if len(text) >= 5 and all(t in EnsembleEngine.CHARSET for t in text):
            return text, 0.5
        return text, 0.15

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
    # Length sanity: the generator emits 5 chars. Ensemble junk-reads are
    # 1-3 (colored noise) and its reads are all subject to the SAME OCR
    # weakness, so agreement overstates correctness. Hard-penalize any
    # length deviation and cap the whole heuristic — empirically this local
    # engine is ~1/10 EXACT regardless of how "confident" the voting looks.
    # The confidence must therefore NEVER claim 'solved' on its own.
    EXPECTED = 5
    if len(text) != EXPECTED:
        conf *= 0.25          # length off => likely misread / junk
    else:
        conf *= 0.55          # length right but chars still share OCR bias
    conf = float(min(0.65, max(0.0, conf)))  # hard cap: heuristic can't
    # reach the vision/trusted bar by itself — honesty over optimism.
    return text, conf


def solve_with_fallback(path_or_bgr, conf_trust: float = DEFAULT_CONF_TRUST,
                        conf_bar: float = DEFAULT_CONF_BAR,
                        prefer_vision: bool = True) -> dict:
    """Main entry. Accepts an image path or a BGR numpy array.

    Returns {text, confidence, method, local_candidate, error?}.
    method:
      'heuristic'        -> local confidence >= trust (high confidence local)
      'vision'           -> local was weak, vision model transcribed it
      'heuristic-lowconf'-> neither trusted; local guess returned, flagged.
    """
    if isinstance(path_or_bgr, (str, os.PathLike)):
        img = cv2.imread(str(path_or_bgr))
        if img is None:
            return {"text": "", "confidence": 0.0,
                    "method": "heuristic-lowconf",
                    "error": f"cannot read {path_or_bgr}"}
    else:
        img = path_or_bgr

    local_text, local_conf = _local_confident(img)

    # High local confidence -> done.
    if local_text and local_conf >= conf_trust:
        return {"text": local_text, "confidence": local_conf,
                "method": "heuristic", "local_candidate": local_text}

    # Weak local -> try a vision model (only when a URL is configured).
    if prefer_vision or local_conf < conf_bar:
        if _vision_url():
            png = _base64_png(img)
            tok, ok = _vision_call(png)
            if ok and tok:
                return {"text": tok, "confidence": 1.0,
                        "method": "vision", "local_candidate": local_text}

    # Neither trusted -> honest low-confidence local guess.
    method = "heuristic-lowconf"
    return {"text": local_text, "confidence": local_conf,
            "method": method, "local_candidate": local_text}


# ---------------------------------------------------------------- CLI

def cli(path: str) -> dict:
    import json
    res = solve_with_fallback(path)
    res_json = {
        "text": res.get("text", ""),
        "confidence": round(res.get("confidence", 0.0), 3),
        "method": res.get("method", ""),
    }
    if res.get("error"):
        res_json["error"] = res["error"]
    return res_json


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: python -m solver.vision.solve_with_fallback image.png")
    r = cli(sys.argv[1])
    print(f"text={r.get('text')!r} confidence={r.get('confidence')} "
          f"method={r.get('method')}")
