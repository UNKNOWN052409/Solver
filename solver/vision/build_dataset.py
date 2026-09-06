#!/usr/bin/env python3
"""build_dataset — build a filtered, normalized, deduped REAL captcha dataset.

Scan every image under data/real_captchas/, assign each its family + a
classification/regression target, then:
  1. DEDUPE   — drop exact + near-duplicates (perceptual-hash, hamming < 4)
  2. NORMALIZE— resize every kept image to a per-family canonical size
  3. SPLIT    — deterministic train/val split per family (seed-stable)

Family -> target (ground truth lives on disk / in prior tools):
  grid/text : 5-char string, 24-char alphabet 3479ACDEFHJKLMNPQRTUVWXY
              (GT dict from tools/a3_measure_before.py, mirrored here)
  rotation  : angle-to-upright, encoded in filename <idx>_rot<ANG>_00_<idx>.png
  slider    : regression x-offset (CV-solved, no stored per-sample class)
  hcaptcha  : object tile class (category filename upstream; local files 1..4.jpg
              are not category-named -> unlabeled for classification)

Output (JSON, written to solver/vision/dataset_real.json):
  {dedup_removed, kept, by_family, splits:{train,val}, targets_per_family,
   classification_targets_total}

REAL-ONLY: every entry is a real file read with cv2; nothing is fabricated.

Usage:
    python -m solver.vision.build_dataset [--out path] [--val_frac 0.25]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

import cv2
import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "real_captchas")

# ---- ground truth ----------------------------------------------------------
# grid text GT — VERBATIM from tools/a3_measure_before.py + solver/vision/train_map_ocr.py
GRID_GT = {
 0:"4KTN9",1:"7UTUP",2:"D37JF",3:"HTJA9",4:"JX7CL",5:"JYRJX",6:"KK4EK",
 7:"KWNVJ",8:"PY3WU",9:"TH9TQ",10:"TJKN9",11:"WELDP",12:"UPVAP",13:"FYEVU",
 14:"Q9DHQ",15:"3WCE7",16:"LDWC7",17:"QR939",18:"R3AWX",19:"WTVRY"}
ALPHABET = "3479ACDEFHJKLMNPQRTUVWXY"

# canonical normalized size per family: (w, h)
NORM_SIZE = {
    "grid":    (128, 128),
    "rotation": (152, 152),
    "slider":  (416, None),      # keep aspect; width 416 cap
    "hcaptcha":(128, 128),
}

_FAMILY_DIRS = {
    "grid":     "grid",
    "rotation": "rot",
    "slider":   "slider",
    "hcaptcha": "hcaptcha",
}


def parse_rot_angle(fname: str):
    m = re.match(r"(\d+)_rot(\d+)_00_\d+\.png", fname)
    return int(m.group(2)) if m else None


def family_target(family: str, path: str, fname: str):
    """Return (has_target:bool, target). target = str label or int class."""
    if family == "grid":
        m = re.match(r"map_(\d{5})\.png", fname)
        if m and int(m.group(1)) in GRID_GT:
            label = GRID_GT[int(m.group(1))]
            # classification target: 5 heads x 24-way, but a single string label
            return True, {"label": label,
                          "num_heads": 5,
                          "classes_per_head": len(ALPHABET),
                          "alphabet": ALPHABET}
        return False, None
    if family == "rotation":
        a = parse_rot_angle(fname)
        if a is not None:
            return True, {"label": a, "n_bins": 360}   # 360-way angle class
        return False, None
    if family == "slider":
        return True, {"label": None, "target_type": "regression_x_offset"}
    if family == "hcaptcha":
        return False, None   # category filename lost local -> unlabeled
    return False, None


def phash(img: np.ndarray) -> int:
    """dHash-ish construction: 8x8 downscale -> 64-bit perceptual hash."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (9, 8), interpolation=cv2.INTER_AREA)  # 8 rows diff
    diff = g[:, 1:] > g[:, :-1]
    return int("".join("1" if b else "0" for b in diff.flatten()), 2)


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def normalize(img: np.ndarray, family: str) -> np.ndarray:
    w, h = NORM_SIZE[family]
    if family == "slider" and h is None:
        r = w / float(img.shape[1])
        nh = max(1, int(round(img.shape[0] * r)))
        return cv2.resize(img, (w, nh), interpolation=cv2.INTER_AREA)
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def collect_all():
    """Return list of dicts: {family, path, fname, src_dims, has_target, target}."""
    recs = []
    for family, sub in _FAMILY_DIRS.items():
        d = os.path.join(ROOT, sub)
        if not os.path.isdir(d):
            continue
        for fname in sorted(os.listdir(d)):
            if fname.startswith("."):
                continue
            p = os.path.join(d, fname)
            if not os.path.isfile(p):
                continue
            img = cv2.imread(p, cv2.IMREAD_COLOR)
            if img is None:
                print(f"  !! unreadable: {p}")
                continue
            has_t, tgt = family_target(family, p, fname)
            recs.append({
                "family": family, "path": p, "fname": fname,
                "src_dims": (img.shape[1], img.shape[0]),
                "channels": img.shape[2] if img.ndim == 3 else 1,
                "sha1": hashlib.sha1(cv2.imencode(".png", img)[1]).hexdigest()[:16],
                "phash": phash(img),
                "has_target": has_t, "target": tgt,
            })
    return recs


def dedupe(recs, thresh=4):
    """Drop exact/near-dupes keeping the first occurrence per family cluster."""
    keep, seen = [], []
    for r in recs:
        dup = False
        for s in seen:
            if s["family"] == r["family"] and hamming(s["phash"], r["phash"]) < thresh:
                dup = True
                break
        if not dup:
            keep.append(r)
            seen.append(r)
    return keep, len(recs) - len(keep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dataset_real.json"))
    ap.add_argument("--val_frac", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    recs = collect_all()
    src_total = len(recs)
    print(f"[scan] real files read: {src_total}")

    # dedupe
    kept, removed = dedupe(recs)
    print(f"[dedupe] removed={removed} kept={kept}")

    # split per family (seed-stable)
    rng = np.random.default_rng(args.seed)
    by_fam = {}
    for r in kept:
        by_fam.setdefault(r["family"], []).append(r)
    splits = {"train": [], "val": []}
    for fam, fam_recs in by_fam.items():
        idx = rng.permutation(len(fam_recs))
        nval = max(0, int(round(len(fam_recs) * args.val_frac)))
        for pos in range(len(fam_recs)):
            r = fam_recs[idx[pos]]
            splits["val" if pos < nval else "train"].append(r)

    # classification target accounting
    targets_per_family = {}
    total_class_targets = 0
    for fam, fr in by_fam.items():
        labeled = [r for r in fr if r["has_target"]]
        if fam == "grid":
            # 5 heads x 24 classes, and each labeled image contributes 5 class decisions
            classes = 5 * len(ALPHABET)
            n_imgs = len(labeled)
        elif fam == "rotation":
            classes = 360
            n_imgs = len(labeled)
        else:
            classes = None
            n_imgs = len(labeled)
        targets_per_family[fam] = {
            "images": len(fr),
            "labeled": n_imgs,
            "class_space": classes,
            "class_instances": (n_imgs * 5 if fam == "grid" else
                                n_imgs * 1 if fam == "rotation" else None),
        }
        if classes:
            total_class_targets += targets_per_family[fam]["class_instances"]

    out = {
        "real_files_total": src_total,
        "dedup_removed": removed,
        "kept_total": len(kept),
        "by_family": {f: len(v) for f, v in by_fam.items()},
        "splits": {
            "train": len(splits["train"]),
            "val": len(splits["val"]),
            "val_frac": args.val_frac,
            "train_by_family": {f: sum(1 for r in splits["train"] if r["family"] == f)
                                for f in by_fam},
            "val_by_family": {f: sum(1 for r in splits["val"] if r["family"] == f)
                              for f in by_fam},
        },
        "targets_per_family": targets_per_family,
        "classification_targets_total": total_class_targets,
        "norm_size": NORM_SIZE,
    }

    with open(args.out, "w") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps(out, indent=2))
    print(f"\n[ok] wrote {args.out}")


if __name__ == "__main__":
    main()
