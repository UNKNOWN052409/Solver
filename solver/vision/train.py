"""TileNet training + distill + ONNX export.

Do modes:
  --synthetic : zero-data smoke — synthetic geometric tiles pe multi-label
               head train hota hai; pipeline proof (harvest->train->export
               ka rehearsal, torch-free numpy SGD)
  --data DIR  : harvested grids (harvest.py output, weak-labels meta.json
               se) pe training — torch path (agar available) warna numpy.

Target deployment: ONNX int8 -> CPU ~ms/tile, GPU batch 50k tiles/sec.

    python -m solver.vision.train --synthetic --epochs 3
    python -m solver.vision.train --data data/tiles --epochs 20
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import TileNet, CLASSES, NUM_CLASSES


# ------------------------------------------------ synthetic tiles

def synth_tile(rng, cls_idx):
    """Deterministic-class synthetic tile — color/texture signature per
    class (pipeline proof; real data pe weak-labels se seekhega)."""
    x = rng.normal(0, 0.15, (96, 96, 3)).astype(np.float32)
    hue = (cls_idx * 37) % 360 / 360.0
    base = np.array([hue, 0.8, 0.6])
    import colorsys
    r, g, b = colorsys.hsv_to_rgb(*base)
    x[..., int(cls_idx % 3)] += 0.5 * (0.4 + 0.6 * r)
    # texture stripes per class-group
    if cls_idx % 2 == 0:
        x[::8] += 0.3
    else:
        x[:, ::8] += 0.3
    # center blob — 'object present' signal
    cy, cx = 48, 48
    yy, xx = np.mgrid[0:96, 0:96]
    mask = ((yy - cy) ** 2 + (xx - cx) ** 2) < 600
    x[mask] += 0.4 * (1 + cls_idx % 7) / 7
    return x.transpose(2, 0, 1)


def make_batch(rng, n, n_classes):
    X, Y = [], []
    for _ in range(n):
        k = rng.integers(1, 3)
        cls = rng.choice(n_classes, size=k, replace=False)
        x = synth_tile(rng, cls[0])
        for c in cls[1:]:
            x = np.maximum(x, synth_tile(rng, c) * 0.6)
        X.append(x)
        y = np.zeros(n_classes, dtype=np.float32)
        y[cls] = 1.0
        Y.append(y)
    return np.stack(X), np.stack(Y)


# ------------------------------------------------ numpy SGD (ref)

def train_synthetic(net, epochs=3, steps_per_epoch=30, lr=0.05):
    rng = np.random.default_rng(7)
    # ref-impl me full backprop numpy conv pe mehenga hai — pipeline
    # proof: fc layer ko synthetic targets pe fit karo (feature extractor
    # frozen). Real training torch/ONNX path me hota hai (train_torch).
    print(f"[synthetic] fc-head fitting: {epochs}x{steps_per_epoch} steps")
    for ep in range(epochs):
        loss_acc = 0.0
        for _ in range(steps_per_epoch):
            X, Y = make_batch(rng, 8, net.nc)
            H = np.zeros((X.shape[0], 128), dtype=np.float32)
            # frozen GAP features (forward only, last conv layer se)
            h = X
            for i in range(5):
                from model import _conv_bn
                h = _conv_bn(h, net.params[f"w{i}"], net.params[f"b{i}"])
                h = np.maximum(h, 0)
                if i < 4:
                    h = h[:, :, ::2, ::2]
            H = h.mean(axis=(2, 3))
            logits = H @ net.params["wf"] + net.params["bf"]
            sig = 1 / (1 + np.exp(-logits))
            # BCE grad
            dz = (sig - Y) / Y.shape[0]
            gw = H.T @ dz
            gb = dz.sum(axis=0)
            net.params["wf"] -= lr * gw
            net.params["bf"] -= lr * gb
            loss_acc += float(-np.mean(Y * np.log(sig + 1e-7) +
                                         (1 - Y) * np.log(1 - sig + 1e-7)))
        print(f"  epoch {ep+1}: loss={loss_acc/steps_per_epoch:.4f}")
    # accuracy probe
    X, Y = make_batch(rng, 16, net.nc)
    sig = 1 / (1 + np.exp(-net.forward(X)))
    pred = (sig > 0.5).astype(np.float32)
    acc = (pred * Y).sum() / max(1.0, Y.sum())
    print(f"  recall@0.5: {acc:.3f} (synthetic signature task)")


def train_torch(data_dir, epochs=20):
    """Torch path — harvested grids + weak-labels se full training.
    Requires: torch, torchvision (CPU theek hai). Tiles 96x96,
    multi-label BCE, augmentation fliplr/rot90."""
    try:
        import torch
        import torch.nn as nn
        import torch.nn.functional as F
    except ImportError:
        print("[!] torch nahi hai — pip install torch (CPU wheel theek hai)")
        sys.exit(1)
    print("[torch] TileNet-T (trainable conv) init...")
    import torchvision.transforms as T
    from PIL import Image

    class TileNetT(nn.Module):
        def __init__(self, nc):
            super().__init__()
            def block(cin, cout):
                return nn.Sequential(
                    nn.Conv2d(cin, cout, 3), nn.ReLU(), nn.MaxPool2d(2))
            self.f = nn.Sequential(
                block(3, 16), block(16, 32), block(32, 64),
                block(64, 128), block(128, 128),
                nn.AdaptiveAvgPool2d(1))
            self.fc = nn.Linear(128, nc)
        def forward(self, x):
            return self.fc(self.f(x).flatten(1))

    # dataset: grids -> tiles + prompt weak-label (meta.json)
    # PU-learning (positive-unlabeled): grid prompt class ke tiles me se
    # sirf kuch positive hote hain (~25-40% reCAPTCHA me). Pure positive
    # labeling noise create karta — isliye har grid se:
    #   - 'select all' grids: positives = unknown subset -> PU trick
    #     (low-confidence ko negative-sample maaro, high-confidence pos)
    #   - practical approach: class-balanced sampling + mixup-style
    #     soft labels (pos_rate = 0.33 prior)
    meta = json.load(open(os.path.join(data_dir, "meta.json")))
    pairs, labels_vocab = [], {}
    POS_RATE = 0.33      # reCAPTCHA grid me ~1/3 tiles target hote hain
    for m in meta:
        gdir = os.path.join(data_dir, m["grid"])
        prompt = m.get("prompt", "").lower()
        # prompt -> class idx (fuzzy: substring match on CLASSES)
        tidx = [i for i, c in enumerate(CLASSES) if c in prompt]
        if not tidx:
            continue
        for fn in sorted(os.listdir(gdir)):
            if fn.endswith(".png"):
                # soft label: P(tile=class) = POS_RATE prior. Loss me
                # BCE-with-soft-targets — model khud separate seekhta hai.
                pairs.append((os.path.join(gdir, fn), tidx[0]))
    if not pairs:
        print("[!] koi labeled tiles nahi — harvest pehle chalao")
        sys.exit(1)
    nc = NUM_CLASSES
    net = TileNetT(nc)
    # ---- adaptive device (GPU->CUDA/MPS, warna CPU) ----
    from solver.vision.device import pick_device, batch_size_for, amp_enabled
    device, dev_desc = pick_device()
    net = net.to(device)
    use_amp = amp_enabled(device)
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)
    print(f"[device] {dev_desc} | batch={batch_size_for(device)} | AMP={'ON' if use_amp else 'off'}")
    print(f"[torch] {sum(p.numel() for p in net.parameters()):,} params | "
          f"{len(pairs)} tiles")
    tf = T.Compose([T.Resize((96, 96)), T.ToTensor()])
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    import random as _rnd
    bs = batch_size_for(device)
    for ep in range(epochs):
        tot, loss_acc = 0, 0.0
        order = pairs[:]
        _rnd.shuffle(order)
        for i in range(0, len(order), bs):
            chunk = order[i:i + bs]
            xs = torch.stack([tf(Image.open(p).convert("RGB")) for p, _ in chunk]).to(device)
            ys = torch.full((len(chunk), nc), 0.02)
            for j, (_, cls) in enumerate(chunk):
                ys[j, cls] = POS_RATE + 0.1
            with torch.amp.autocast("cuda", enabled=use_amp):
                logits = net(xs)
                loss = F.binary_cross_entropy_with_logits(logits.float(), ys)
            opt.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            loss_acc += float(loss) * len(chunk); tot += len(chunk)
        print(f"  epoch {ep+1}: loss={loss_acc/max(1,tot):.4f} ({tot} tiles, bs={bs})")
    os.makedirs("data/pt", exist_ok=True)
    torch.save(net.state_dict(), "data/pt/tilenet.pt")
    # ONNX export
    try:
        dummy = torch.zeros(1, 3, 96, 96)
        torch.onnx.export(net, dummy, "data/pt/tilenet.onnx",
                          input_names=["tile"], output_names=["logits"],
                          dynamic_axes={"tile": {0: "batch"},
                                        "logits": {0: "batch"}})
        print("[+] ONNX export: data/pt/tilenet.onnx (int8 quant next)")
    except Exception as e:
        print("[!] onnx export:", e)
    print("[done] weights: data/pt/tilenet.pt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--data", default="")
    ap.add_argument("--epochs", type=int, default=20)
    args = ap.parse_args()
    if args.synthetic:
        train_synthetic(TileNet(), epochs=min(3, args.epochs))
    elif args.data:
        train_torch(args.data, epochs=args.epochs)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
