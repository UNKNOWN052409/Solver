"""train_map_ocr — TRAIN the real-data multi-head CNN on 20 real map captchas.

REAL-ONLY. No synthetic/mock captcha images. The 20 labeled real images in
data/real_captchas/grid/ are augmented (rotation / elastic / affine-warp /
contrast / translation) to stretch ~20 real samples into a few hundred
REAL-DERIVED training patches. Ground-truth 5-char labels come from
tools/a3_measure_before.py (the hardcoded dict, verbatim).

Data split (honest): leave K=5 real images OUT of training; they are only
used as the VALIDATION set (augmented too, since 5 real images is tiny, but
their source identity never touches the training set). Per-char + full-image
accuracies reported on those held-out real images and on training real images.

    python -m solver.vision.train_map_ocr --epochs 40 --aug 12 --holdout 5
"""
import argparse
import os
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_cnn import ALPHABET, NUM_CHARS, NUM_CLASSES, CHAR2IDX, build_model

DATA = "/home/kali/NeoSolver/data/real_captchas/grid"
# Ground truth dict — VERBATIM from tools/a3_measure_before.py (REAL labels).
GT = {
 0:"4KTN9",1:"7UTUP",2:"D37JF",3:"HTJA9",4:"JX7CL",5:"JYRJX",6:"KK4EK",
 7:"KWNVJ",8:"PY3WU",9:"TH9TQ",10:"TJKN9",11:"WELDP",12:"UPVAP",13:"FYEVU",
 14:"Q9DHQ",15:"3WCE7",16:"LDWC7",17:"QR939",18:"R3AWX",19:"WTVRY"}

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
MODEL_PATH = os.path.join(MODEL_DIR, "map_ocr.pt")


# ---------------------------------------------------------------- load real data

def load_real():
    """Load the 20 real images -> (tensor list, labels). Channels kept as-is."""
    items = []
    for i in range(20):
        p = os.path.join(DATA, f"map_{i:05d}.png")
        if not os.path.exists(p):
            raise FileNotFoundError(p)
        img = cv2.imread(p)                     # BGR uint8 128x128
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        items.append((gray, GT[i]))
    return items


# ---------------------------------------------------------------- augmentation

def _elastic(gray, rng, alpha=14, sigma=4):
    """Small elastic distortion — helps with font/warp variance. Real-derived."""
    shape = gray.shape
    dx = cv2.GaussianBlur((rng.random(size=shape) * 2 - 1).astype(np.float32),
                          (0, 0), sigma) * alpha
    dy = cv2.GaussianBlur((rng.random(size=shape) * 2 - 1).astype(np.float32),
                          (0, 0), sigma) * alpha
    x, y = np.meshgrid(np.arange(shape[1]), np.arange(shape[0]))
    mapx = (x + dx).astype(np.float32)
    mapy = (y + dy).astype(np.float32)
    return cv2.remap(gray, mapx, mapy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def augment(gray, rng, mode=0):
    """Return one REAL-derived augmented patch (128x128 uint8)."""
    img = gray
    if mode % 4 == 1:        # rotation
        ang = rng.uniform(-9, 9)
        M = cv2.getRotationMatrix2D((64, 64), ang, 1.0)
        img = cv2.warpAffine(img, M, (128, 128), borderMode=cv2.BORDER_REPLICATE)
    elif mode % 4 == 2:      # affine warp / perspective-ish
        pts1 = np.float32([[10, 10], [118, 12], [8, 118]])
        off = rng.uniform(-6, 6, (3, 2)).astype(np.float32)
        pts2 = pts1 + off
        M = cv2.getAffineTransform(pts1, pts2)
        img = cv2.warpAffine(img, M, (128, 128), borderMode=cv2.BORDER_REPLICATE)
    elif mode % 4 == 3:      # elastic
        img = _elastic(img, rng)
    # translation (any mode ~ half the time)
    if rng.random() < 0.6:
        tx = int(rng.uniform(-4, 4)); ty = int(rng.uniform(-4, 4))
        M = np.float32([[1, 0, tx], [0, 1, ty]])
        img = cv2.warpAffine(img, M, (128, 128), borderMode=cv2.BORDER_REPLICATE)
    # contrast/brightness
    img = img.astype(np.float32)
    a = rng.uniform(0.7, 1.5)
    b = rng.uniform(-20, 20)
    img = np.clip(a * img + b, 0, 255).astype(np.uint8)
    # CLAHE for contrast on low-contrast originals
    if rng.random() < 0.5:
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        img = clahe.apply(img)
    return img


def build_dataset(items, aug_per, rng):
    """Expand real images -> tensors. Returns (X (N,1,128,128) float32 /255, Y (N,5) ints)."""
    X, Y = [], []
    for (gray, label) in items:
        # ground patch (no aug)
        X.append((gray / 255.0).astype(np.float32)[None, None])
        Y.append([CHAR2IDX[c] for c in label])
        for m in range(aug_per):
            X.append((augment(gray, rng, m) / 255.0).astype(np.float32)[None, None])
            Y.append([CHAR2IDX[c] for c in label])
    X = np.ascontiguousarray(np.concatenate(X, axis=0))
    Y = np.array(Y, dtype=np.int64)
    # shuffle
    idx = rng.permutation(len(X))
    return X[idx], Y[idx]


# ---------------------------------------------------------------- train / eval

def one_hot_seq(Y, nc=24):
    """Y (N,5) -> list of 5 one-hot (N,24) float32."""
    import torch
    N = Y.shape[0]
    return [torch.zeros(N, nc, dtype=torch.float32).scatter_(
        1, torch.from_numpy(Y[:, c]).unsqueeze(1).long(), 1.0) for c in range(5)]


def eval_model(net, Xt, Yt, device):
    """Return (per_char_acc, n_chars_correct_total, n_imgs, full_img_acc)."""
    import torch
    net.eval()
    with torch.no_grad():
        xb = torch.from_numpy(Xt).to(device)
        outs = [o.argmax(1).cpu().numpy() for o in net(xb)]
        pred = np.stack(outs, axis=1)                 # (N,5)
        correct = (pred == Yt)
        per_char = correct.mean()
        full_img = (correct.all(axis=1)).mean()
        n_chars = int(correct.sum())
    return float(per_char), n_chars, len(Xt), float(full_img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--aug", type=int, default=12, help="augmentations per real image")
    ap.add_argument("--holdout", type=int, default=5, help="real images left out for val")
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--save", default=MODEL_PATH)
    args = ap.parse_args()

    import torch
    import torch.nn as nn

    device = "cpu"
    rng = np.random.default_rng(0)

    real = load_real()
    print(f"[real] loaded {len(real)} real labeled images (144x144 -> {DATA})")
    print(f"[real] alphabet={ALPHABET} ({NUM_CLASSES} chars) x {NUM_CHARS} positions")

    # ---- honest split: leave `holdout` real images out of training
    rng2 = np.random.default_rng(123)
    val_idx = sorted(rng2.choice(len(real), size=args.holdout, replace=False).tolist())
    train_items = [real[i] for i in range(len(real)) if i not in val_idx]
    val_items = [real[i] for i in val_idx]
    print(f"[split] TRAIN real images={len(train_items)} idxs="
          f"{sorted(i for i in range(20) if i not in val_idx)}")
    print(f"[split] VAL  real images={len(val_items)} idxs={val_idx}")

    # ---- build datasets (real-derived augmentation)
    Xtr, Ytr = build_dataset(train_items, args.aug, rng)
    Xva, Yva = build_dataset(val_items, args.aug, rng)
    print(f"[data] train patches={Xtr.shape[0]} (REAL-derived, {len(train_items)} src imgs "
          f"x {args.aug+1}) | val patches={Xva.shape[0]} (REAL held-out, {len(val_items)} src)")

    # ---- build + train
    net = build_model(seed=0).to(device)
    nparams = sum(p.numel() for p in net.parameters())
    print(f"[model] map_cnn {nparams:,} params | device={device}")

    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    xtr = torch.from_numpy(Xtr).to(device)
    bs = 32
    for ep in range(args.epochs):
        net.train()
        order = torch.randperm(Xtr.shape[0])
        tot_loss = 0.0; nbatch = 0
        for bi in range(0, Xtr.shape[0], bs):
            idx = order[bi:bi + bs]
            xb = xtr[idx]
            yb = one_hot_seq(Ytr[idx.cpu().numpy()])
            outs = net(xb)
            loss = sum(nn.functional.binary_cross_entropy_with_logits(o, y)
                       for o, y in zip(outs, yb))
            opt.zero_grad(); loss.backward(); opt.step()
            tot_loss += float(loss); nbatch += 1
        sched.step()
        if (ep + 1) % 10 == 0 or ep == 0:
            tr_ac, tr_n, _, _ = eval_model(net, Xtr, Ytr, device)
            va_per, va_n, va_cnt, va_full = eval_model(net, Xva, Yva, device)
            print(f"  ep {ep+1}/{args.epochs} loss={tot_loss/nbatch:.3f} "
                  f"train_char={tr_ac:.3f} val_char={va_per:.3f} "
                  f"val_full_img={va_full:.3f} ({va_n}/{va_cnt} chars on val)")

    # ---- final honest evaluation (on the SAME data split, no tuning magic)
    tr_ac, tr_nc, _, tr_full = eval_model(net, Xtr, Ytr, device)
    va_per, va_nc, va_cnt, va_full = eval_model(net, Xva, Yva, device)
    print("=" * 60)
    print(f"[RESULT] TRAIN real-derived: char_acc={tr_ac:.3f} ({tr_nc}/{Xtr.shape[0]} chars) "
          f"full_img={tr_full:.3f}")
    print(f"[RESULT] VAL held-out REAL:  char_acc={va_per:.3f} ({va_nc}/{va_cnt} chars, "
          f"{len(val_items)} real imgs) full_img={va_full:.3f}")
    # baseline comparison: random guessing on 24 classes = 1/24 = 0.0417/char
    print(f"[baseline] random 1/{NUM_CLASSES} = {1/NUM_CLASSES:.3f} char_acc; "
          f"existing pure-CV ben onion = 0.30 char_acc (ref a3_measure_before)")

    # ---- save weights
    os.makedirs(os.path.dirname(args.save), exist_ok=True)
    torch.save({
        "model": net.state_dict(),
        "alphabet": ALPHABET,
        "num_chars": NUM_CHARS,
        "num_classes": NUM_CLASSES,
        "device": device,
        "aug_per": args.aug,
        "holdout_idxs": val_idx,
        "src": "REAL data/real_captchas/grid (20 labels from tools/a3_measure_before.py)",
        "train_char_acc": tr_ac,
        "val_char_acc": va_per,
        "val_full_img_acc": va_full,
    }, args.save)
    print(f"[+] saved weights -> {args.save}")


if __name__ == "__main__":
    main()
