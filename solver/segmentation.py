"""Character segmentation via connected-component analysis."""

import cv2
import numpy as np


def _x_overlap(a, b):
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ov = min(ax0 + aw, bx0 + bw) - max(ax0, bx0)
    return ov / max(min(aw, bw), 1)


def find_char_boxes(binary_img: np.ndarray, min_area_frac=0.002, max_boxes=16,
                   fuse_gaps: bool = False):
    """Locate character blobs in a preprocessed (text=black) image.

    Two-pass strategy for noisy captchas:
      1. collect candidate components with a permissive filter
      2. reject streaks by aspect ratio, drop specks against a p90
         height reference, then fuse fragmented pieces sharing an x-column
    Returns boxes sorted left-to-right as (x, y, w, h).
    """
    h, w = binary_img.shape
    fg = cv2.bitwise_not(binary_img)
    num, _, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)

    cand = []
    for i in range(1, num):  # label 0 is background
        x, y, bw, bh, area = stats[i]
        if area < min_area_frac * w * h:
            continue
        if bh > h * 0.98 or bw > w * 0.8:
            continue
        cand.append((int(x), int(y), int(bw), int(bh), int(area)))

    if not cand:
        return []

    # Streak rejection: wave-warped borders and grid lines are far wider
    # than tall compared to any glyph.
    cand = [b for b in cand if b[2] <= 4 * max(b[3], 1)]

    # Line-junk rejection: diagonal noise has sparse pixel coverage inside
    # its bounding box, glyphs are dense strokes.
    cand = [
        b for b in cand
        if b[4] / max(b[2] * b[3], 1) >= 0.22
    ]
    cand = [b[:4] for b in cand]

    if not cand:
        return []  # all candidates rejected as junk (guards percentile below)

    # Speck rejection: reference height from the 90th percentile so noise
    # dots can't drag the baseline down; real glyphs tower above specks.
    ref_h = float(np.percentile([b[3] for b in cand], 90))
    kept = [b for b in cand if b[3] >= ref_h * 0.30] or cand

    # Fragment fusion: pieces of one glyph share an x-column.
    changed = True
    while changed:
        changed = False
        for i in range(len(kept)):
            for j in range(i + 1, len(kept)):
                if _x_overlap(kept[i], kept[j]) > 0.3:
                    a, b = kept[i], kept[j]
                    nx0 = min(a[0], b[0])
                    ny0 = min(a[1], b[1])
                    nx1 = max(a[0] + a[2], b[0] + b[2])
                    ny1 = max(a[1] + a[3], b[1] + b[3])
                    kept[i] = [nx0, ny0, nx1 - nx0, ny1 - ny0]
                    del kept[j]
                    changed = True
                    break
            if changed:
                break

    # Tiny-gap fusion: OFF by default. Measured on synthetic captchas,
    # fusing boxes with gap < 0.35*width merges REAL adjacent glyphs
    # (2/40 exact vs 26/40 without) — it only helps targets whose glyphs
    # render as horizontal fragments, so it's opt-in per target.
    if fuse_gaps:
        kept = merge_touching(kept, gap_ratio=0.35)

    kept.sort(key=lambda b: b[0])

    # Over-wide boxes usually mean two glyphs kissed during rendering;
    # split them at the emptiest internal columns.
    med_w = float(np.median([b[2] for b in kept]))
    out = []
    for box in kept:
        n = int(round(box[2] / max(med_w, 1)))
        if med_w > 0 and box[2] > med_w * 1.45 and n >= 2:
            out.extend(_split_box(binary_img, box, min(n, 3)))
        else:
            out.append(box)
    out.sort(key=lambda b: b[0])
    return out[:max_boxes]


def _split_box(binary_img: np.ndarray, box, n_pieces: int):
    """Split an over-wide box into n pieces at vertical-projection valleys."""
    x, y, bw, bh = box
    fg = cv2.bitwise_not(binary_img[y:y + bh, x:x + bw])
    col = fg.sum(axis=0) / 255.0

    cuts = [0]
    for k in range(1, n_pieces):
        center = int(bw * k / n_pieces)
        half = max(int(bw / n_pieces * 0.3), 3)
        lo = max(center - half, cuts[-1] + 4)
        hi = min(center + half, bw - 5)
        if hi > lo:
            cuts.append(lo + int(np.argmin(col[lo:hi])))
        else:
            cuts.append(center)
    cuts.append(bw)
    return [
        (x + cuts[i], y, cuts[i + 1] - cuts[i], bh)
        for i in range(len(cuts) - 1)
        if cuts[i + 1] - cuts[i] >= 4
    ]


def extract_chars(binary_img: np.ndarray, boxes, pad=2):
    """Crop each box out of the image with a small safety margin."""
    h, w = binary_img.shape
    crops = []
    for (x, y, bw, bh) in boxes:
        x0, y0 = max(x - pad, 0), max(y - pad, 0)
        x1, y1 = min(x + bw + pad, w), min(y + bh + pad, h)
        crops.append(binary_img[y0:y1, x0:x1])
    return crops


def merge_touching(boxes, gap_ratio=0.35):
    """Post-pass for under-segmentation: fuse adjacent boxes whose gap
    is tiny (one glyph split into two side-by-side blobs)."""
    if len(boxes) < 2:
        return boxes
    merged = [list(boxes[0])]
    for x, y, w, h in boxes[1:]:
        px, py, pw, ph = merged[-1]
        gap = x - (px + pw)
        avg_w = (pw + w) / 2
        if gap < gap_ratio * avg_w:
            nx0, ny0 = min(px, x), min(py, y)
            nx1 = max(px + pw, x + w)
            ny1 = max(py + ph, y + h)
            merged[-1] = [nx0, ny0, nx1 - nx0, ny1 - ny0]
        else:
            merged.append([x, y, w, h])
    return [tuple(b) for b in merged]
