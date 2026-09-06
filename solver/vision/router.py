"""router — CAPTCHA-type detector + dispatcher (AI-INDEPENDENT, numpy+cv2 only).

Takes any captcha image and:
  1. classifies its TYPE with pure-CV heuristics (no torch/network/API)
  2. routes it to the matching LOCAL solver path

    detect_type(image_bgr, context=None) -> {
        kind, sub_kind, method, confidence
    }
    kind ∈ {text, grid_tiles, arkose_rotate, slider_gap, vqa, math, generic_image}

    solve_dispatch(image_bgr, context=None) -> {
        type, method, result, confidence
    }

Design rule (LO): CAPTCHA solving is AI-INDEPENDENT. Detectors and solvers
here run purely on the local CPU with numpy + OpenCV. PyTorch is OPTIONAL
and only used for the local TileNet grid path — guarded so this module still
works completely without torch.

Routing:
  grid_tiles     -> TileNet.predict_labels  (torch-guarded; falls back to
                                               solve_with_fallback if no torch)
  arkose_rotate  -> RotNet.angle            (same local guard)
  text / vqa / math / generic_image
                 -> solve_with_fallback.solve (local OCR ensemble)
  slider_gap     -> _solve_slider (cv2 gap-offset scan -> gap_x)

CLI:
    python -m solver.vision.router <image>     # prints type + method + result
    python -m solver.vision.router --verify    # self-test on synthetic images
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

# all AI-independent kinds that route through the pure-local fallback solver
_HEURISTIC_KINDS = ("text", "vqa", "math", "generic_image")

# ---------------------------------------------------------------------------
# small preprocessing helpers
# ---------------------------------------------------------------------------


def _gray(img: np.ndarray) -> np.ndarray:
    if img is None:
        return np.zeros((1, 1), np.uint8)
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _read(image_or_path) -> np.ndarray | None:
    if isinstance(image_or_path, (str, os.PathLike)):
        img = cv2.imread(str(image_or_path))
        return img
    return image_or_path


# ---------------------------------------------------------------------------
# Detectors — each returns (matched: bool, confidence: float, sub_kind: str)
# ---------------------------------------------------------------------------

def _detect_grid_tiles(gray: np.ndarray):
    """A tile grid has many small, uniformly spaced square cells. Detect the
    regular row+column lattice via edge projection periodicity + square
    contour count."""
    h, w = gray.shape[:2]
    if h < 20 or w < 20:
        return False, 0.0, ""

    edges = cv2.Canny(gray, 60, 160)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)

    # --- lattice score: evenly spaced edge peaks in row & column projections
    col_prof = edges.sum(axis=0) / max(1, h)     # (w,)
    row_prof = edges.sum(axis=1) / max(1, w)     # (h,)
    kx = max(3, w // 64)
    ky = max(3, h // 64)
    col_prof = cv2.GaussianBlur(col_prof.astype(np.float32), (max(3, kx | 1), 1), 0).ravel()
    row_prof = cv2.GaussianBlur(row_prof.astype(np.float32), (max(3, ky | 1), 1), 0).ravel()

    def _periodicity(prof: np.ndarray, axis_len: int) -> float:
        """Autocorrelation of the projection profile -> 1.0 if strongly periodic."""
        p = prof - prof.mean()
        n = len(p)
        denom = np.dot(p, p) or 1.0
        # autocorr at lags up to half the length
        lags = np.arange(1, max(2, axis_len // 3))
        best = 0.0
        for lag in range(max(2, axis_len // 20), max(2, axis_len // 3)):
            num = np.dot(p[: n - lag], p[lag:])
            r = num / denom
            if r > best:
                best = r
        # require a *strong* repeating peak + occasional negative (trough) ->
        # a real lattice profile swings positive/negative
        swing = np.ptp(p) / max(1e-6, p.max() - p.min() if (p.max() - p.min()) > 1e-6 else 1.0)
        return min(1.0, max(0.0, best))

    row_per = _periodicity(row_prof, h)
    col_per = _periodicity(col_prof, w)

    # --- square-contour count
    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    squares = 0
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 8 or ch < 8:
            continue
        ar = cw / max(1.0, ch)
        if 0.6 < ar < 1.7:                      # roughly square
            area = cv2.contourArea(c)
            r_area = cw * ch
            if r_area > 0 and 0.45 < area / r_area < 1.0:  # solid fill
                squares += 1

    lattice = (row_per + col_per) / 2.0
    # A real tile grid is periodic on BOTH axes (2D lattice); rotated text
    # rings are periodic on ONE axis only. Require the WEAKER axis to still be
    # periodic, so single-axis rot-text doesn't false-fire as grid_tiles.
    both_axes = min(row_per, col_per)
    tile_ratio = min(1.0, squares / 12.0)       # many closed cells
    conf = 0.45 * both_axes + 0.55 * tile_ratio
    matched = conf >= 0.42 and (squares >= 8 or both_axes >= 0.55)
    sub = f"lattice={both_axes:.2f} squares={squares}"
    return matched, float(min(1.0, conf)), sub


def _detect_arkose_rotate(gray: np.ndarray):
    """Arkose 'rotate the object': a big circular ball / radial-gradient disc
    dominates the frame. Signature: ONE LARGE high-circularity blob (a grid of
    many small squares >e.g. 0.78 max circularity, but there are MANY of them
    and each is small — so we require a single dominant disc)."""
    h, w = gray.shape[:2]
    if h < 40 or w < 40:
        return False, 0.0, ""

    # radial-gradient disc -> blur out into a bright blob; look for circle
    blurred = cv2.GaussianBlur(gray, (9, 9), 0)
    binimg = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    cnts, _ = cv2.findContours(binimg, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    best = 0.0          # circularity of the most circle-like LARGE blob
    large_circ = 0      # count of LARGE circular blobs (a grid -> many)
    for c in cnts:
        area = cv2.contourArea(c)
        if area < (h * w) * 0.06:
            continue
        peri = cv2.arcLength(c, True)
        if peri <= 0:
            continue
        circ = 4.0 * np.pi * area / (peri * peri)   # 1.0 == perfect circle
        if circ > best:
            best = circ
        if circ > 0.72:                  # a true disc-shaped large blob
            large_circ += 1

    # Hough is only trusted when the silhouette is ALSO strongly circular
    hough = 0.0
    try:
        c = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=h // 2,
                             param1=120, param2=40, minRadius=int(h * 0.10),
                             maxRadius=int(max(h, w) * 0.5))
        if c is not None:
            best_r = float(c[0, :, 2].max())
            if best_r > 0 and best_r >= max(h, w) * 0.12:   # sizable disc
                hough = min(1.0, c.shape[1] / 2.0)
    except Exception:
        pass

    # one dominant disc: high circularity AND it's the only big disc (grid
    # has many) -> cap conf down when many discs are present. Require a
    # genuinely round silhouette (square ~0.79, noise ~0.74 both excluded).
    crowding = max(1.0, float(large_circ))
    conf = max(best * 0.95, hough) / (crowding ** 0.4)
    # STRICT guard (real-data): a genuine arkose 'rotate' disc needs EITHER a
    # Hough-confirmed circle OR a near-perfect circularity (~0.85+, a real
    # filled disc). Chaotic text backgrounds top out ~0.78-0.80 circularity
    # with NO Hough circle — those must not fire as arkose_rotate.
    disc_strong = (hough > 0.0) or (best >= 0.85)
    matched = conf >= 0.68 and disc_strong
    sub = f"circ={best:.2f} hough={hough:.2f} discs={large_circ}"
    return matched, float(min(1.0, conf)), sub


def _slider_gap_scan(gray: np.ndarray):
    """Detect the slider puzzle gap + confidence via the column-edge signature.

    A slider gap is a narrow vertical slot (the missing puzzle piece) cut out
    of a large panel. In the per-column Canny-edge density profile the slot
    shows up as a TALL, NARROW, isolated spike — unlike a tile grid (whose
    periodic lines spread edge density into a WIDE spike) or a plain panel
    (no spike). Returns (gap_x, height, width_frac, conf) or (None,0,0,0)."""
    h, w = gray.shape[:2]
    if h < 40 or w < 40:
        return None, 0.0, 0.0, 0.0
    edges = cv2.Canny(gray, 50, 150)
    col = edges.sum(axis=0) / max(1, h)
    col = cv2.GaussianBlur(col.astype(np.float32), (11, 1), 0).ravel()
    m = int(w * 0.05)
    if w - 2 * m < 4:
        return None, 0.0, 0.0, 0.0
    core = col[m:w - m]
    mean = core.mean()
    s = core.std() or 1e-6
    val = np.asarray(core)
    top_idx = int(np.argmax(val))
    top = val[top_idx]
    height = (top - mean) / s                       # spike height in std units
    th = mean + (top - mean) * 0.4
    above = np.where(val >= th)[0]
    width = (above.max() - above.min()) if len(above) else 0
    width_frac = width / max(1, w)
    gap_x = m + top_idx
    return gap_x, float(height), float(width_frac), 0.0


def _detect_slider_gap(gray: np.ndarray):
    """Slider/fill-gap: a large panel with a narrow vertical notch. The notch
    produces a tall, narrow, isolated column-edge spike."""
    h, w = gray.shape[:2]
    gap_x, height, width_frac, _ = _slider_gap_scan(gray)
    if gap_x is None:
        return False, 0.0, ""
    # tall narrow isolated spike in a large panel
    matched = height >= 3.5 and width_frac <= 0.18
    conf = min(1.0, 0.4 + 0.25 * min(2.0, height / 3.5)
               + 0.25 * max(0.0, (0.18 - width_frac) / 0.18))
    sub = f"height={height:.1f} wfrac={width_frac:.2f} gapx={gap_x}"
    return matched, float(min(1.0, conf)), sub


def _detect_text(gray: np.ndarray):
    """Text captcha: a small set (2-8) of separate glyph blobs on a fairly flat
    background. Real-world text captchas are often LOW-CONTRAST / washed-out
    (e.g. map/chaotic backgrounds), so contrast-stretch (CLAHE) before Otsu
    so glyph structure actually survives the threshold."""
    h, w = gray.shape[:2]
    if h < 20 or w < 20:
        return False, 0.0, ""

    def glyphs_on(g):
        g = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(g)
        g = cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX)
        _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        b = cv2.morphologyEx(b, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        n, _, st, _ = cv2.connectedComponentsWithStats(b, 8)
        tot = 0
        cnt = 0
        for i in range(1, n):
            x, y, cw, ch, a = st[i]
            if cw < 2 or ch < 2:
                continue
            cnt += 1
            tot += a
        return cnt, tot

    # best signal across plain + CLAHE-enhanced thresholding
    best_glyphs, best_frac = glyphs_on(gray)
    if best_glyphs < 2:
        # also try global histogram (very low-contrast flat caps)
        b2 = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        n2, _, st2, _ = cv2.connectedComponentsWithStats(
            cv2.morphologyEx(b2, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8)), 8)
        c2 = t2 = 0
        for i in range(1, n2):
            cw, ch = st2[i][2], st2[i][3]
            if cw >= 2 and ch >= 2:
                c2 += 1
                t2 += st2[i][4]
        if c2 > best_glyphs:
            best_glyphs, best_frac = c2, t2

    glyphs = best_glyphs
    frac = best_frac / max(1, h * w)
    ideal = 3 <= glyphs <= 9 and 0.01 < frac < 0.35
    conf = min(1.0, max(0.0, (glyphs - 1) * 0.12 + (0.3 if ideal else 0.0)))
    matched = 0.5 <= glyphs <= 10 and ideal
    sub = f"glyphs={glyphs} frac={frac:.3f}"
    return matched, float(min(1.0, conf)), sub


def _detect_math(gray):
    """Math captcha / arithmetic: operator symbols (+ - x / =) present.

    Detects operators morphologically: a '+'/'x'/'=' has a crossing of a
    vertical line-stroke and a horizontal line-stroke (or a lone thin stroke
    for '-'), combined with compact digit blobs — giving a short-equation
    signature distinct from random 5-char text."""

    h, w = gray.shape[:2]
    if h < 30 or w < 30:
        return False, 0.0, ""
    _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binimg, 8)

    thin_strokes = 0     # '-', '/', '=' etc. (high aspect-ratio components)
    compact = 0          # digit-ish compact blobs
    for i in range(1, n_labels):
        x, y, cw, ch, a = stats[i]
        if cw < 3 or ch < 3:
            continue
        ar = max(cw, ch) / max(1.0, min(cw, ch))
        if ar > 2.2:
            thin_strokes += 1
        elif 0.25 < a / max(1, cw * ch) < 1.0 and max(cw, ch) <= max(h, w) * 0.35:
            compact += 1

    # '+'-like cross: vertical-line AND horizontal-line erosions overlap
    crossings = 0
    try:
        v = cv2.erode(binimg, np.ones((1, 5), np.uint8))
        hs = cv2.erode(binimg, np.ones((5, 1), np.uint8))
        xing = cv2.bitwise_and(v, hs)
        crossings = int(cv2.countNonZero(xing) > 0)
    except Exception:
        crossings = 0

    # short arithmetic expression: operators (thin strokes / cross) + digits.
    # STRICT guard (real-data): require a clear operator cross AND >=2 compact
    # digit blobs AND the compact blobs form a horizontal run (a real equation
    # sits on one line). Chaotic text-captcha backgrounds produce random
    # crosses + scattered blobs that otherwise false-positive as 'math'.
    operator_signal = thin_strokes + crossings
    # horizontal-run test: compact blobs share a narrow vertical band
    comp_x, comp_y = [], []
    for i in range(1, n_labels):
        x, y, cw, ch, a = stats[i]
        if cw < 3 or ch < 3:
            continue
        ar = max(cw, ch) / max(1.0, min(cw, ch))
        if not (ar > 2.2) and 0.25 < a / max(1, cw * ch) < 1.0 \
                and max(cw, ch) <= max(h, w) * 0.35:
            comp_x.append(x + cw / 2)
            comp_y.append(y + ch / 2)
    horizontal = False
    if len(comp_x) >= 2:
        cy0 = min(comp_y)
        cy1 = max(comp_y)
        horizontal = (cy1 - cy0) <= max(h, w) * 0.35

    conf = min(1.0, 0.45 + 0.18 * min(3, operator_signal) +
               (0.12 if compact >= 1 else 0.0))
    matched = (crossings > 0 and compact >= 2 and horizontal)
    sub = f"ops={operator_signal} compact={compact} cross={crossings} ln={horizontal}"
    return matched, float(min(1.0, conf)), sub


def _detect_vqa(gray):
    """VQA/instruction captcha: lots of natural text (many glyphs across a
    wide area) — a written question/instruction rather than 5 random chars."""
    h, w = gray.shape[:2]
    _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(binimg, 8)
    glyphs = 0
    for i in range(1, n_labels):
        x, y, cw, ch, a = stats[i]
        if cw >= 3 and ch >= 3:
            glyphs += 1
    conf = min(1.0, max(0.0, (glyphs - 10) * 0.08))
    matched = glyphs >= 14
    sub = f"glyphs={glyphs}"
    return matched, float(conf), sub


# ---------------------------------------------------------------------------
# public type detector
# ---------------------------------------------------------------------------

def detect_type(image_bgr, context=None) -> dict:
    """Classify the captcha type with pure-CV heuristics.

    Returns {kind, sub_kind, method, confidence} where
    kind ∈ {text, grid_tiles, arkose_rotate, slider_gap, vqa, math,
            generic_image}.
    """
    img = image_bgr
    if img is None:
        return {"kind": "generic_image", "sub_kind": "empty",
                "method": "fallback", "confidence": 0.0}
    gray = _gray(img)

    checks = [
        ("grid_tiles", _detect_grid_tiles(gray)),
        ("arkose_rotate", _detect_arkose_rotate(gray)),
        ("slider_gap", _detect_slider_gap(gray)),
        ("math", _detect_math(gray)),
        ("vqa", _detect_vqa(gray)),
        ("text", _detect_text(gray)),
    ]

    best_kind = "generic_image"
    best_conf = 0.0
    best_sub = ""
    for kind, (matched, conf, sub) in checks:
        if matched and conf > best_conf:
            best_kind, best_conf, best_sub = kind, conf, sub

    if best_conf <= 0.32:
        best_kind, best_sub = "generic_image", "no-clear-signal"

    return {
        "kind": best_kind,
        "sub_kind": best_sub,
        "method": "cvheuristics",
        "confidence": float(round(best_conf, 3)),
    }


# ---------------------------------------------------------------------------
# solvers
# ---------------------------------------------------------------------------

def _solve_grid(image_bgr, det: dict) -> dict:
    """Grid tiles -> TileNet.predict_labels when torch + a trained model are
    present (still LOCAL; no API). Otherwise honest fallback."""
    try:
        import torch
        from solver.vision.model import TileNet
    except Exception:
        torch = None
    model_path = os.environ.get("CAPTCHA_MODEL", os.path.join(
        os.path.dirname(__file__), "model.pt"))

    # always attach tile geometry (A2 tiler) so downstream has click coords
    tiles = None
    try:
        from solver.vision import tiler as _tiler
        t = _tiler.tile_grid(image_bgr)
        tiles = {"n": t.get("n"), "cell_w": t.get("cell_w"),
                 "cell_h": t.get("cell_h"), "tiles": t.get("tiles")}
    except Exception:
        tiles = None

    if torch is not None and os.path.exists(model_path):
        try:
            net = TileNet()
            # 96x96 RGB layout expected by the net
            gray = _gray(image_bgr)
            rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            x = cv2.resize(rgb, (96, 96)).astype(np.float32) / 255.0
            x = np.transpose(x, (2, 0, 1))[None]          # (1,3,96,96)
            labels = net.predict_labels(x)
            best = [(lbl, conf) for lbl, conf in labels[0]][:6]
            conf = det["confidence"]
            return {"type": "grid_tiles", "method": "TileNet.predict_labels",
                    "result": [lbl for lbl, _ in best],
                    "confidence": float(conf), "tiles": tiles}
        except Exception as exc:
            # fall through to the local OCR fallback rather than erroring
            pass

    from solver.vision import solve_with_fallback
    r = solve_with_fallback.solve(image_bgr)
    return {"type": "grid_tiles", "method": r["method"],
            "result": r["text"], "confidence": r["confidence"],
            "tiles": tiles}


def _solve_arkose(image_bgr, det: dict) -> dict:
    """Arkose rotate -> RotNet.angle when torch + model present (LOCAL);
    else pure-CV rotation estimate fallback."""
    try:
        import torch
        from solver.vision.model import RotNet
    except Exception:
        torch = None
    model_path = os.environ.get("CAPTCHA_MODEL", os.path.join(
        os.path.dirname(__file__), "model.pt"))
    if torch is not None and os.path.exists(model_path):
        try:
            net = RotNet()
            gray = _gray(image_bgr)
            rgb = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            x = cv2.resize(rgb, (96, 96)).astype(np.float32) / 255.0
            x = np.transpose(x, (2, 0, 1))[None]
            angle = net.angle(x)
            return {"type": "arkose_rotate", "method": "RotNet.angle",
                    "result": angle, "confidence": det["confidence"]}
        except Exception:
            pass
    # pure-CV ball refraction estimate: brightest edge gradient direction
    gray = _gray(image_bgr)
    try:
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        dx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
        dy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
        ang = float(np.degrees(np.arctan2(dy.mean(), dx.mean())))
    except Exception:
        ang = 0.0
    return {"type": "arkose_rotate", "method": "cvgradient",
            "result": round(ang, 1), "confidence": det["confidence"]}


def _solve_slider(image_bgr, det: dict) -> dict:
    """Slider gap-offset scan via cv2 contour/edge differencing. Returns the
    horizontal offset (in px) of the missing-piece gap (gap_x)."""
    gray = _gray(image_bgr)
    h, w = gray.shape[:2]
    gap_x, height, width_frac, _ = _slider_gap_scan(gray)
    if gap_x is None:
        gap_x = w // 2
    return {"type": "slider_gap", "method": "cv-gap-scan",
            "result": int(gap_x), "confidence": det["confidence"],
            "gap_x": int(gap_x)}


def _solve_heuristic(kind, image_bgr, det) -> dict:
    """text / vqa / math / generic_image -> local OCR ensemble fallback."""
    from solver.vision import solve_with_fallback
    r = solve_with_fallback.solve(image_bgr)
    return {"type": kind, "method": r["method"], "result": r["text"],
            "confidence": r["confidence"]}


# ---------------------------------------------------------------------------
# public dispatcher
# ---------------------------------------------------------------------------

def solve_dispatch(image_bgr, context=None) -> dict:
    """Detect the captcha type, then solve it with the matching LOCAL path."""
    img = _read(image_bgr)
    det = detect_type(img, context)
    kind = det["kind"]

    if kind == "grid_tiles":
        return _solve_grid(img, det)
    if kind == "arkose_rotate":
        return _solve_arkose(img, det)
    if kind == "slider_gap":
        return _solve_slider(img, det)
    # text / vqa / math / generic_image -> local OCR ensemble fallback
    return _solve_heuristic(kind, img, det)   # see _HEURISTIC_KINDS


# ---------------------------------------------------------------------------
# self-test (synthetic) — pure-CV generation, no data files
# ---------------------------------------------------------------------------

def _make_synthetic_tile_grid(rows=3, cols=4, cell=48) -> np.ndarray:
    """A fake reCAPTCHA-style tile grid: uniform square cells with grout."""
    h, w = rows * cell, cols * cell
    img = np.full((h, w, 3), 255, np.uint8)
    for r in range(rows):
        for c in range(cols):
            val = np.random.randint(0, 255, 3).tolist()
            cv2.rectangle(img, (c * cell, r * cell),
                          (c * cell + cell - 2, r * cell + cell - 2),
                          val, -1)
    # draw grout lines (cell borders) explicitly so the lattice is crisp
    for r in range(rows + 1):
        cv2.line(img, (0, r * cell), (w, r * cell), (10, 10, 10), 2)
    for c in range(cols + 1):
        cv2.line(img, (c * cell, 0), (c * cell, h), (10, 10, 10), 2)
    return img


def _make_synthetic_text() -> np.ndarray:
    """A fake 5-char text captcha on a flat background."""
    img = np.full((80, 220, 3), 245, np.uint8)
    txt = "4m4xj"
    # draw each char as a filled rect-ish glyph blob (keeps it CV-detectable
    # without needing a raster font by randomizing shapes)
    x = 20
    y0, y1 = 22, 60
    for i, ch in enumerate(txt):
        cw = 22
        cv2.rectangle(img, (x, y0), (x + cw, y1), (15, 15, 15), -1)
        # a 'hole' so it stays blob-like (not a solid mass)
        if i % 2 == 0:
            cv2.rectangle(img, (x + 5, y0 + 6), (x + cw - 5, y1 - 6),
                          (245, 245, 245), -1)
            cv2.rectangle(img, (x + 8, y0 + 12), (x + cw - 8, y1 - 12),
                          (15, 15, 15), -1)
        x += cw + 10
    return img


def _make_synthetic_arkose() -> np.ndarray:
    """A fake Arkose 'rotate the object': radial-gradient ball."""
    h = w = 200
    xx, yy = np.mgrid[0:h, 0:w]
    r = np.sqrt((xx - h // 2) ** 2 + (yy - w // 2) ** 2)
    ball = np.clip(255 - r * 2.5, 0, 255).astype(np.uint8)
    ball = cv2.GaussianBlur(ball, (5, 5), 0)
    return cv2.cvtColor(ball, cv2.COLOR_GRAY2BGR)


def _make_synthetic_slider() -> np.ndarray:
    """A fake slider: large panel with a narrow vertical gap notch."""
    h, w, gx, slotw = 200, 400, 250, 40
    img = np.full((h, w, 3), 230, np.uint8)
    cv2.rectangle(img, (10, 30), (w - 10, h - 30), (180, 180, 180), -1)
    cv2.rectangle(img, (gx - slotw // 2, h - 40), (gx + slotw // 2, h - 20),
                  (230, 230, 230), -1)
    cv2.rectangle(img, (gx - slotw // 2, h - 40), (gx - slotw // 2 + 3, h - 20),
                  (40, 40, 40), -1)
    cv2.rectangle(img, (gx + slotw // 2 - 3, h - 40), (gx + slotw // 2, h - 20),
                  (40, 40, 40), -1)
    return img


def _verify() -> None:
    """Build synthetic captchas and assert the router detects the right kind."""
    import json
    ok = True

    grid = _make_synthetic_tile_grid()
    td = detect_type(grid)
    grid_det = td["kind"] == "grid_tiles"
    ok = ok and grid_det
    print(f"[verify] synthetic tile-grid  -> {td['kind']:14s} "
          f"conf={td['confidence']} sub={td['sub_kind']} "
          f"{'OK' if grid_det else 'FAIL'}")

    text = _make_synthetic_text()
    tt = detect_type(text)
    text_det = tt["kind"] == "text"
    ok = ok and text_det
    print(f"[verify] synthetic text       -> {tt['kind']:14s} "
          f"conf={tt['confidence']} sub={tt['sub_kind']} "
          f"{'OK' if text_det else 'FAIL'}")

    arkose = _make_synthetic_arkose()
    ka = detect_type(arkose)
    arkose_det = ka["kind"] == "arkose_rotate"
    ok = ok and arkose_det
    print(f"[verify] synthetic arkose     -> {ka['kind']:14s} "
          f"conf={ka['confidence']} sub={ka['sub_kind']} "
          f"{'OK' if arkose_det else 'FAIL'}")

    slider = _make_synthetic_slider()
    ks = detect_type(slider)
    slider_det = ks["kind"] == "slider_gap"
    ok = ok and slider_det
    print(f"[verify] synthetic slider     -> {ks['kind']:14s} "
          f"conf={ks['confidence']} sub={ks['sub_kind']} "
          f"{'OK' if slider_det else 'FAIL'}")

    # dispatch smoke: a real solve path must return a result dict (no error)
    dr = solve_dispatch(grid)
    dr2 = solve_dispatch(text)
    dk = solve_dispatch(arkose)
    ds = solve_dispatch(slider)
    print(f"[verify] dispatch(grid)   -> type={dr['type']} method={dr['method']} "
          f"result={str(dr['result'])[:20]!r}")
    print(f"[verify] dispatch(text)   -> type={dr2['type']} method={dr2['method']} "
          f"result={str(dr2['result'])[:20]!r}")
    print(f"[verify] dispatch(arkose) -> type={dk['type']} method={dk['method']} "
          f"result={dk['result']}")
    print(f"[verify] dispatch(slider) -> type={ds['type']} method={ds['method']} "
          f"result={ds['result']} (gap_x={ds.get('gap_x')})")

    # write synthetic test images to disk so the CLI can be exercised on them
    outdir = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
    os.makedirs(outdir, exist_ok=True)
    cv2.imwrite(os.path.join(outdir, "synthetic_grid.png"), grid)
    cv2.imwrite(os.path.join(outdir, "synthetic_text.png"), text)
    cv2.imwrite(os.path.join(outdir, "synthetic_arkose.png"), arkose)
    cv2.imwrite(os.path.join(outdir, "synthetic_slider.png"), slider)

    print("\nRESULT:", "PASS" if ok else "FAIL")
    print(json.dumps({"grid_detect_ok": grid_det, "text_detect_ok": text_det}))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(_USAGE)
        return 1
    if argv[0] in ("--verify", "-v"):
        _verify()
        return 0
    path = argv[0]
    img = cv2.imread(path)
    if img is None:
        print(f"error: cannot read image {path}")
        return 1
    det = detect_type(img)
    res = solve_dispatch(img, det)
    print(f"type={res['type']} method={res['method']} "
          f"result={res['result']!r} confidence={res['confidence']}")
    return 0


_USAGE = """usage:
    python -m solver.vision.router <image>   # detect type + solve
    python -m solver.vision.router --verify  # self-test on synthetic captchas
"""

if __name__ == "__main__":
    raise SystemExit(_cli())
