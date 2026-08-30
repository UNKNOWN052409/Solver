"""Train the slot CNN on a REAL labeled captcha dataset (no synthesis).

Dataset layout (label map + image dir, one file per captcha):
    <data_dir>/data.txt        lines: "<uuid-or-name> <label>"
    <data_dir>/images/<name>.png

    python -m training.train_slot --data /path/to/lcsd/4-characters-captcha \
        --out slot_model.pt --x0 11 --x1 69 --n-chars 4 --epochs 12

Geometry defaults match the LCSD-style 81x84/81-wide captchas; derive
x0/x1 for a new target by aggregating column occupancy across a few
hundred samples (see eval harness in repo docs). Split is per-IMAGE.
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solver.engines.cnn_engine import IMG_SIZE, CharNet  # noqa: E402


def load_pairs(data_dir: Path):
    pairs = []
    for line in (data_dir / "data.txt").read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        fname, label = line.rsplit(" ", 1)
        pairs.append((data_dir / "images" / f"{fname}.png", label))
    return pairs


def slot_crops(gray: np.ndarray, x0: int, x1: int, n_chars: int):
    h, w = gray.shape
    x0 = max(0, min(x0, w - n_chars))
    x1 = max(x0 + n_chars, min(x1, w))
    band_w = (x1 - x0) / n_chars
    crops = []
    for i in range(n_chars):
        xa = int(x0 + i * band_w)
        xb = int(x0 + (i + 1) * band_w)
        crop = gray[:, xa:xb]
        crop = cv2.resize(crop, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)
        crops.append(crop.astype(np.float32) / 255.0)
    return crops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="dir containing data.txt + images/")
    ap.add_argument("--out", default="slot_model.pt")
    ap.add_argument("--x0", type=int, default=11)
    ap.add_argument("--x1", type=int, default=69)
    ap.add_argument("--n-chars", type=int, default=4)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--test-frac", type=float, default=0.08)
    args = ap.parse_args()

    try:
        import torch
        import torch.nn.functional as F
    except ImportError:
        sys.exit("[!] PyTorch required: pip install torch")

    data_dir = Path(args.data)
    pairs = load_pairs(data_dir)
    if not pairs:
        sys.exit(f"[!] no labeled pairs found under {data_dir}")
    print(f"[*] {len(pairs)} real labeled captchas from {data_dir}")

    rng = np.random.default_rng(42)
    perm = rng.permutation(len(pairs))
    n_test = max(1, int(len(pairs) * args.test_frac))
    test_idx, train_idx = perm[:n_test], perm[n_test:]
    print(f"[*] per-image split: {len(train_idx)} train / {n_test} test")

    charset = sorted({c for _, lab in pairs for c in lab})
    idx = {c: i for i, c in enumerate(charset)}
    print(f"[*] charset ({len(charset)}): {''.join(charset)}")

    def explode(indices):
        xs, ys = [], []
        for i in indices:
            path, label = pairs[i]
            gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                continue
            for crop, ch in zip(slot_crops(gray, args.x0, args.x1, args.n_chars), label):
                xs.append(crop)
                ys.append(idx[ch])
        return (np.stack(xs)[:, None, :, :], np.array(ys))

    xt, yt = explode(train_idx)
    xv, yv = explode(test_idx)
    print(f"[+] crops: {len(xt)} train / {len(xv)} test")

    xt_t, yt_t = torch.from_numpy(xt), torch.from_numpy(yt)
    xv_t, yv_t = torch.from_numpy(xv), torch.from_numpy(yv)

    model = CharNet(len(charset))
    opt = torch.optim.Adam(model.net.parameters(), lr=args.lr)

    for epoch in range(args.epochs):
        model.net.train()
        shuffle = torch.randperm(len(xt_t))
        tot = 0.0
        for i in range(0, len(xt_t), args.batch):
            b = shuffle[i:i + args.batch]
            loss = F.cross_entropy(model.net(xt_t[b]), yt_t[b])
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item() * len(b)
        model.net.eval()
        with torch.no_grad():
            acc = (model.net(xv_t).argmax(1) == yv_t).float().mean().item()
        print(f"epoch {epoch+1:02d}/{args.epochs}  loss={tot/len(xt_t):.4f}  val_slot_acc={acc:.4f}")

    # full-captcha accuracy on held-out real images
    model.net.eval()
    full_ok = 0
    with torch.no_grad():
        for i in test_idx:
            path, label = pairs[i]
            gray = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                continue
            batch = np.stack(slot_crops(gray, args.x0, args.x1, args.n_chars))[:, None, :, :]
            preds = model.net(torch.from_numpy(batch)).argmax(1).tolist()
            if "".join(charset[p] for p in preds) == label:
                full_ok += 1
    print(f"[RESULT] FULL captcha acc on {n_test} unseen real images: "
          f"{full_ok}/{n_test} = {full_ok/n_test:.2%}")

    torch.save({"state_dict": model.net.state_dict(), "charset": "".join(charset)}, args.out)
    print(f"[+] saved -> {args.out}")


if __name__ == "__main__":
    main()
