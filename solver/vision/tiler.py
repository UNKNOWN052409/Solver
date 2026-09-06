"""tiler — tile-grid box extraction + per-tile features (AI-INDEPENDENT, cv2).

reCAPTCHA v2 / hCaptcha "select all tiles with X" grids are a regular NxN
lattice of squares (usually 3x3, sometimes 2x2). Before any model runs you
need the GEOMETRY of the grid: where each cell is, and its center pixel, so
a solver can click the right boxes.

This module turns a grid screenshot (or any image containing a clean NxN
square lattice) into:

    tile_grid(image, n=None) -> {
        n, rows, cols,
        cell_w, cell_h,                      # uniform cell size
        tiles: [ {row, col, x, y, w, h, cx, cy}, ... ]   # cx, cy = click point
    }

plus per-tile FEATURES a classifier can key on, all computed locally
(no torch, no model file, no API):

    tile_features(image, tiles) -> {
        feats: [ {row, col, mean_bgr, mean_gray, edge_density, contrast,
                  packed_vec: [..float32..]}, ... ],
        feats_np: (n*n, D)  # matrix rows = tiles, cols = feature dims
    }

    nearest_template_match(image, tiles, template, metric='ncc') -> labels
        # per-tile match score against a target template (e.g. a cropped
        # copy of the prompt's object) — the cheap "does this tile contain
        # the thing" signal that needs no training data.

Design rule (LO): AI-independent. All of this runs on numpy + OpenCV.

CLI:
    python -m solver.vision.tiler <image> [n]
        # prints detected lattice + tile centers + feature summary
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# lattice detection
# ---------------------------------------------------------------------------


def detect_lattice(image, n: int | None = None) -> tuple[int, int, int] | None:
    """Return (n, cell_w, cell_h) for the strongest square lattice, or None.

    If `n` is given, just measure exact uniform cell size from the projection
    peaks (assumes the grid is a clean NxN square lattice). Otherwise infer n
    from the periodicity of the edge-projection profile — require the width
    and height share the same lattice order (square grid).
    """
    g = _gray(image)
    h, w = g.shape[:2]
    if h < 20 or w < 20:
        return None
    edges = cv2.Canny(g, 60, 160)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)

    col_prof = edges.sum(axis=0) / max(1, h)   # (w,)
    row_prof = edges.sum(axis=1) / max(1, w)   # (h,)

    def _peaks(prof: np.ndarray, length: int, n_expected: int):
        """Find the n_expected+1 roughly-even left edges of the lattice."""
        mx = prof.max()
        if mx <= 0:
            return None
        th = prof.mean() + 0.30 * (mx - prof.mean())
        strong = np.where(prof >= th)[0]
        if len(strong) < 2:
            return None
        # cluster consecutive strong columns into bands
        bands = []
        start = strong[0]
        prev = strong[0]
        for v in strong[1:]:
            if v - prev > max(2, length // 200):
                bands.append((start, prev))
                start = v
            prev = v
        bands.append((start, prev))
        # take the strongest band centers as grid lines
        centers = []
        for (a, b) in bands:
            seg = prof[a:b + 1]
            centers.append(int(a + np.argmax(seg)))
        return centers

    # infer n if not given: try candidates 2..6, choose the one whose both
    # axes give the same lattice (square cells -> equal counts)
    if n is None:
        best_n, best_cw, best_ch = None, None, None
        for cand in range(2, 7):
            cxs = _peaks(col_prof, w, cand)
            rys = _peaks(row_prof, h, cand)
            if cxs is None or rys is None:
                continue
            if len(cxs) < 2 or len(rys) < 2:
                continue
            # expected 1 + cand lines
            if abs(len(cxs) - (1 + cand)) > 1 or abs(len(rys) - (1 + cand)) > 1:
                continue
            cw_i = (cxs[-1] - cxs[0]) / max(1, len(cxs) - 1)
            ch_i = (rys[-1] - rys[0]) / max(1, len(rys) - 1)
            ratio = cw_i / max(1.0, ch_i)
            if 0.6 < ratio < 1.7:                 # cells are ~square
                best_n, best_cw, best_ch = cand, cw_i, ch_i
                break
        n = best_n
        if n is None:
            # fall back to a hard assumption of 3x3 (the overwhelmingly common
            # reCAPTCHA v2 / hCaptcha grid)
            n, cw, ch = 3, w / 3.0, h / 3.0
            n, cw, ch = _exact_from_n(g, n)
            return (n, cw, ch)
        return (n, best_cw, best_ch)

    # n given -> exact uniform measurement (still projection-based)
    return _exact_from_n(g, n)


def _exact_from_n(g: np.ndarray, n: int) -> tuple[int, float, float]:
    """Measure uniform cell size for a KNOWN n — average the line spacing."""
    h, w = g.shape[:2]
    cw = w / max(1, n)
    ch = h / max(1, n)
    return n, float(cw), float(ch)


def tile_grid(image, n: int | None = None) -> dict:
    """Extract NxN tile boxes (with click centers) from a grid image.

    Returns {n, rows, cols, cell_w, cell_h, origin_x, origin_y, tiles} where
    each tile = {row, col, x, y, w, h, cx, cy} and (cx, cy) is the center
    pixel the solver should click.
    """
    lat = detect_lattice(image, n)
    img = _bgr(image)
    h, w = img.shape[:2]
    if lat is None:
        n_i = n or 3
        cw, ch = w / n_i, h / n_i
    else:
        n_i, cw, ch = lat

    tiles = []
    for r in range(n_i):
        for c in range(n_i):
            x = int(round(c * cw))
            y = int(round(r * ch))
            tw = int(round((c + 1) * cw)) - x
            th = int(round((r + 1) * ch)) - y
            tiles.append({
                "row": r, "col": c,
                "x": x, "y": y, "w": max(1, tw), "h": max(1, th),
                "cx": x + max(1, tw) // 2,
                "cy": y + max(1, th) // 2,
            })
    return {
        "n": n_i, "rows": n_i, "cols": n_i,
        "cell_w": float(cw), "cell_h": float(ch),
        "origin_x": 0, "origin_y": 0,
        "tiles": tiles,
    }


# ---------------------------------------------------------------------------
# per-tile features
# ---------------------------------------------------------------------------


def tile_features(image, tiles=None) -> dict:
    """Per-tile feature vector matrix a model/classifier can key on.

    For each tile returns:
      mean_bgr   - mean color (3 floats)
      mean_gray  - mean luminance
      edge_density - fraction of edge pixels (structural content)
      contrast   - std of gray values
      packed_vec - concatenated float32 vector (model input row)
    Also returns feats_np: (num_tiles, D) row-major in grid (row-major so the
    index maps to tile order in `tiles`).
    """
    img = _bgr(image)
    g = _gray(img)
    if tiles is None:
        tiles = tile_grid(image)["tiles"]

    rows = []
    for t in tiles:
        x, y, w, h = t["x"], t["y"], t["w"], t["h"]
        sub = img[y:y + h, x:x + w]
        sg = g[y:y + h, x:x + w]
        if sub.size == 0:
            rows.append({"row": t["row"], "col": t["col"],
                         "mean_bgr": (0.0, 0.0, 0.0), "mean_gray": 0.0,
                         "edge_density": 0.0, "contrast": 0.0})
            continue
        mean_b = sub[..., 0].mean()
        mean_g = sub[..., 1].mean()
        mean_r = sub[..., 2].mean()
        mean_gray = float(sg.mean())
        # edge density
        e = cv2.Canny(sg, 60, 160)
        edge_density = float(e.mean() / 255.0)
        contrast = float(sg.std())
        packed = np.array([mean_b, mean_g, mean_r, mean_gray,
                           edge_density, contrast], dtype=np.float32)
        rows.append({"row": t["row"], "col": t["col"],
                     "mean_bgr": (float(mean_b), float(mean_g), float(mean_r)),
                     "mean_gray": mean_gray,
                     "edge_density": edge_density,
                     "contrast": contrast,
                     "packed_vec": packed})

    feats = rows
    vecs = np.stack([r["packed_vec"] for r in feats]) if feats else \
        np.zeros((0, 6), np.float32)
    return {"feats": feats, "feats_np": vecs}


# ---------------------------------------------------------------------------
# nearest-template match (no training data)
# ---------------------------------------------------------------------------


def nearest_template_match(image, tiles, template, metric: str = "ncc",
                           n: int | None = None) -> list[float]:
    """Per-tile score of how strongly each cell matches `template`.

    `template` is an (Ht,Wt,3) BGR crop of the target object (e.g. a cropped
    "contains a bus" sample). Returns a score per tile in the same order as
    `tiles` (or tile_grid if None). Score ~1 => strong match.

    Internally resizes each tile's image crop to the template size and uses
    normalized cross-correlation (NC-metric) on gray. This is the cheap
    "does this tile contain the thing" signal that needs no model.
    """
    if tiles is None:
        tiles = tile_grid(image, n)["tiles"]
    g = _gray(image)
    tg = _gray(template)
    th, tw = tg.shape[:2]
    if th < 3 or tw < 3:
        return [0.0] * len(tiles)
    tg = tg.astype(np.float32)
    tm = tg.mean()
    ts = tg.std()
    ts = ts if ts > 1e-6 else 1e-6

    scores = []
    for t in tiles:
        x, y, w, h = t["x"], t["y"], t["w"], t["h"]
        sub = g[y:y + h, x:x + w]
        if sub.size == 0:
            scores.append(0.0)
            continue
        sub = cv2.resize(sub, (tw, th)).astype(np.float32)
        if metric == "ncc":
            sm = sub.mean()
            ss = sub.std()
            ss = ss if ss > 1e-6 else 1e-6
            s = float(((sub - sm) * (tg - tm)).sum() / (th * tw * ss * ts))
            scores.append(s)
        else:  # absolute diff (inverted -> 1 = identical)
            d = float(np.abs(sub - tg).mean() / 255.0)
            scores.append(1.0 - d)
    return scores


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gray(img):
    if img is None:
        return np.zeros((1, 1), np.uint8)
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img


def _bgr(img):
    if img is None:
        return np.zeros((1, 1, 3), np.uint8)
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return img


def _read(image_or_path):
    if isinstance(image_or_path, (str, os.PathLike)):
        return cv2.imread(str(image_or_path))
    return image_or_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(img, ret, feats):
    print(f"lattice n={ret['n']} cell={ret['cell_w']:.0f}x{ret['cell_h']:.0f} "
          f"({ret['rows']}x{ret['cols']})")
    for t in ret["tiles"]:
        print(f"  tile[{t['row']}][{t['col']}] "
              f"bbox=({t['x']},{t['y']},{t['w']}x{t['h']}) "
              f"click=({t['cx']},{t['cy']})")
    vecs = feats["feats_np"]
    print(f"feat matrix: {tuple(vecs.shape)} "
          f"(rows=tiles, cols=meanB,meanG,meanR,gray,edge,contrast)")


def _cli(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m solver.vision.tiler <image> [n]")
        return 1
    img = _read(argv[0])
    if img is None:
        print(f"error: cannot read {argv[0]}")
        return 1
    n = int(argv[1]) if len(argv) > 1 else None
    ret = tile_grid(img, n)
    feats = tile_features(img, ret["tiles"])
    _print_summary(img, ret, feats)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
