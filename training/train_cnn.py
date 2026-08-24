"""Train the per-character CNN on synthetic captchas.

    python -m training.train_cnn --out model.pt -n 4000 --epochs 12

Pipeline: CaptchaGenerator -> Preprocessor -> segmentation -> crops
labeled by character -> CharNet -> checkpoint {state_dict, charset}.
Samples whose glyph count doesn't match their label are discarded
(overlapping characters are ambiguous anyway).
"""

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from solver.engines.cnn_engine import IMG_SIZE, CharNet  # noqa: E402
from solver.generator import CaptchaGenerator  # noqa: E402
from solver.preprocessor import Preprocessor  # noqa: E402
from solver.segmentation import extract_chars, find_char_boxes  # noqa: E402


def build_dataset(n_samples: int, length: int):
    from solver.generator import DIGITS, LOWER

    gen = CaptchaGenerator(length=length, charset=DIGITS + LOWER)
    prep = Preprocessor(scale=2.0)

    chars = sorted(set(DIGITS + LOWER))
    idx = {c: i for i, c in enumerate(chars)}

    xs, ys = [], []
    attempts = 0
    while len(xs) < n_samples * length and attempts < n_samples * 6:
        attempts += 1
        img, text = gen.generate()
        binary = prep.run(np.array(img))
        boxes = find_char_boxes(binary)
        if len(boxes) != len(text):
            continue
        for crop, ch in zip(extract_chars(binary, boxes), text):
            small = cv2resize(crop)
            xs.append(small.astype(np.float32) / 255.0)
            ys.append(idx[ch])
    return np.stack(xs)[:, None, :, :], np.array(ys), "".join(chars)


def cv2resize(crop):
    import cv2
    return cv2.resize(crop, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="model.pt")
    ap.add_argument("-n", "--samples", type=int, default=4000)
    ap.add_argument("--epochs", type=int, default=12)
    ap.add_argument("--length", type=int, default=5)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=1e-3)
    args = ap.parse_args()

    try:
        import torch
        import torch.nn.functional as F
    except ImportError:
        sys.exit("[!] PyTorch required for training: pip install torch")

    print(f"[*] Building dataset ({args.samples} samples)...")
    x, y, charset = build_dataset(args.samples, args.length)
    n = len(x)
    perm = np.random.permutation(n)
    cut = int(n * 0.9)
    tr, va = perm[:cut], perm[cut:]
    xt = torch.from_numpy(x[tr])
    yt = torch.from_numpy(y[tr])
    xv = torch.from_numpy(x[va])
    yv = torch.from_numpy(y[va])
    print(f"[+] {n} character crops | classes={len(charset)}")

    model = CharNet(len(charset))
    opt = torch.optim.Adam(model.net.parameters(), lr=args.lr)

    for epoch in range(args.epochs):
        model.net.train()
        shuffle = torch.randperm(len(xt))
        total_loss = 0.0
        for i in range(0, len(xt), args.batch):
            bidx = shuffle[i:i + args.batch]
            logits = model.net(xt[bidx])
            loss = F.cross_entropy(logits, yt[bidx])
            opt.zero_grad()
            loss.backward()
            opt.step()
            total_loss += loss.item() * len(bidx)

        model.net.eval()
        with torch.no_grad():
            acc = (model.net(xv).argmax(1) == yv).float().mean().item()
        print(f"epoch {epoch+1:02d}/{args.epochs}  loss={total_loss/len(xt):.4f}  val_acc={acc:.4f}")

    torch.save({"state_dict": model.net.state_dict(), "charset": charset}, args.out)
    print(f"[+] Saved checkpoint -> {args.out}")


if __name__ == "__main__":
    main()
