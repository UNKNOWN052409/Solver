#!/usr/bin/env python3
"""REAL OCR training — memory-stable on CPU.
Preloads all real captchas ONCE into a single tensor (no per-batch file I/O / PIL churn)
so the box doesn't OOM. 4,587 real labeled gib captchas, 6-char code OCR.
"""
import os, json, time, random
import numpy as np
import torch, torch.nn as nn
from PIL import Image

ROOT = "/home/kali/NeoSolver/data/real_captchas_hf"
MANIFEST = os.path.join(ROOT, "manifest.json")
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
A2I = {c: i for i, c in enumerate(ALPHA)}
NLEN = 6; VOCAB = len(ALPHA)

def build():
    with open(MANIFEST) as f: m = json.load(f)
    rows = []
    for path, label in m.items():
        p = os.path.join(ROOT, str(path))
        lab = str(label)
        if os.path.exists(p) and len(lab) >= 3 and any(c.isalpha() for c in lab) and len(lab) >= NLEN:
            rows.append((p, lab[:NLEN].upper()))
    return rows

def preload(rows):
    xs, ys = [], []
    t0 = time.time()
    for p, lab in rows:
        try:
            im = Image.open(p); im.load()
            a = np.asarray(im.convert("L").resize((200, 50)), dtype=np.float32) / 255.0
            xs.append(a)
            y = np.zeros(NLEN, dtype=np.int64)
            for j, c in enumerate(lab):
                if c in A2I: y[j] = A2I[c]
            ys.append(y)
        except Exception:
            pass
    X = torch.from_numpy(np.stack(xs)[:, None]).contiguous()   # (N,1,50,200)
    Y = torch.from_numpy(np.stack(ys)).contiguous()
    print(f"[PRELOAD] {X.shape[0]} imgs {X.shape[1:]} -> {time.time()-t0:.0f}s", flush=True)
    return X, Y

class OCRNet(nn.Module):
    def __init__(self, vocab=VOCAB):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(64, 128, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(128, 256, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d((1, 1)),
        )
        self.heads = nn.ModuleList([nn.Linear(256, vocab) for _ in range(NLEN)])
    def forward(self, x):
        f = self.features(x).flatten(1)
        return torch.stack([h(f) for h in self.heads], dim=1)

def main():
    rows = build()
    print(f"[REAL] loaded {len(rows)} labeled captchas", flush=True)
    if not rows: print("NO DATA"); return
    random.seed(42); random.shuffle(rows)
    nv = int(len(rows) * 0.15)
    val_rows, tr_rows = rows[:nv], rows[nv:]
    Xtr, Ytr = preload(tr_rows)
    Xv, Yv = preload(val_rows)

    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"[DEVICE] {dev} cuda={torch.cuda.is_available()}", flush=True)
    Xtr, Ytr, Xv, Yv = Xtr.to(dev), Ytr.to(dev), Xv.to(dev), Yv.to(dev)

    bs = 256; epochs = 30
    model = OCRNet().to(dev)
    opt = torch.optim.Adam(model.parameters(), 1e-3)
    lossf = nn.CrossEntropyLoss()
    t0 = time.time()

    for ep in range(epochs):
        model.train()
        perm = torch.randperm(Xtr.shape[0])
        tot = corr = 0
        for s in range(0, Xtr.shape[0], bs):
            idx = perm[s:s+bs]
            X = Xtr[idx]; Y = Ytr[idx]
            out = model(X)
            loss = sum(lossf(out[:, j], Y[:, j]) for j in range(NLEN)) / NLEN
            opt.zero_grad(); loss.backward(); opt.step()
            pred = out.argmax(-1)
            corr += (pred == Y).all(-1).sum().item(); tot += Y.shape[0]
        model.eval()
        with torch.no_grad():
            pr = model(Xv).argmax(-1)
            vacc = (pr == Yv).all(-1).sum().item() / Yv.shape[0]
            # per-char acc too
            cacc = (pr == Yv).sum().item() / Yv.numel()
        print(f"[EP {ep}] train_img={corr/max(tot,1):.4f} val_img={vacc:.4f} val_char={cacc:.4f} ({time.time()-t0:.0f}s)", flush=True)
        torch.save(model.state_dict(), "/home/kali/NeoSolver/solver/vision/models/real_ocr.pt")
    print("[DONE] real OCR trained -> models/real_ocr.pt", flush=True)

if __name__ == "__main__":
    main()
