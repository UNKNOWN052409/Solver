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
    """Returns (samples, charset): each sample is (crops [length,32,32], text).

    Crops stay grouped per captcha so the train/val split can be made
    PER-IMAGE — exploding to individual glyphs before splitting leaks
    same-image crops into both sets and inflates val accuracy.
    """
    import cv2

    from solver.generator import DIGITS, LOWER

    gen = CaptchaGenerator(length=length, charset=DIGITS + LOWER)
    prep = Preprocessor(scale=2.0)

    chars = sorted(set(DIGITS + LOWER))

    samples = []
    attempts = 0
    while len(samples) < n_samples and attempts < n_samples * 6:
        attempts += 1
        img, text = gen.generate()
        binary = prep.run(np.array(img))
        boxes = find_char_boxes(binary)
        if len(boxes) != len(text):
            continue
        crops = extract_chars(binary, boxes)
        small = [
            cv2.resize(c, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)
            for c in crops
        ]
        samples.append((np.stack(small).astype(np.float32) / 255.0, text))
    return samples, "".join(chars)


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
    samples, charset = build_dataset(args.samples, args.length)
    idx = {c: i for i, c in enumerate(charset)}

    # Per-image split: whole captchas go to train or val, never both.
    perm = np.random.permutation(len(samples))
    cut = int(len(samples) * 0.9)
    tr, va = perm[:cut], perm[cut:]

    def explode(indices):
        xs, ys = [], []
        for i in indices:
            crops, text = samples[i]
            for crop, ch in zip(crops, text):
                xs.append(crop)
                ys.append(idx[ch])
        return (
            torch.from_numpy(np.stack(xs)[:, None, :, :]),
            torch.from_numpy(np.array(ys)),
        )

    xt, yt = explode(tr)
    xv, yv = explode(va)
    print(
        f"[+] {len(xt)} train / {len(xv)} val character crops "
        f"({len(tr)}/{len(va)} images) | classes={len(charset)}"
    )

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
