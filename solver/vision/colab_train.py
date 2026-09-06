#!/usr/bin/env python3
"""TileNet GPU re-train (Colab T4) — harvested real tiles, full E2E."""
import os, sys, json, base64, glob, io
import numpy as np

# ---- Colab GPU setup ----
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

dev = "cuda" if torch.cuda.is_available() else "cpu"
print("DEVICE:", dev, torch.cuda.get_device_name(0) if dev == "cuda" else "", flush=True)

TILES_DIR = "/home/kali/NeoSolver/data/tiles"
out_dir = "/home/kali/NeoSolver/data/pt"

# ---- load harvested tiles -> (image, onehot-label) ----
# meta.json me prompt -> class; har grid ki tile files
CLASSES = ["a bus", "a bicycle", "fire hydrant", "a taxi", "a crosswalk",
           "a chimney", "a car", "a truck", "a motorcycle", "a boat",
           "a train", "an airplane", "a bridge", "a mountain", "a river"]
CLS2IDX = {c: i for i, c in enumerate(CLASSES)}

def load_dataset():
    metas = json.load(open(os.path.join(TILES_DIR, "meta.json")))
    X, Y = [], []
    for m in metas:
        gdir = os.path.join(TILES_DIR, m["grid"])
        p = (m.get("prompt") or "").lower()
        cls = None
        for c in CLASSES:
            if c.split(" ")[-1] in p or c in p:
                cls = c
                break
        if cls is None:
            continue
        y = CLS2IDX[cls]
        for f in sorted(glob.glob(os.path.join(gdir, "*.png")))[:9]:
            try:
                from PIL import Image
                im = Image.open(f).convert("RGB").resize((96, 96))
                X.append(np.asarray(im, dtype=np.float32) / 255.0)
                Y.append(y)
            except Exception:
                continue
    return np.array(X), np.array(Y)

print("loading tiles...", flush=True)
X, Y = load_dataset()
print("samples:", X.shape, "classes:", len(CLASSES), flush=True)

# ---- TileNet (compact) ----
class TileNet(nn.Module):
    def __init__(self, n_cls=len(CLASSES)):
        super().__init__()
        self.f = nn.Sequential(
            nn.Conv2d(3, 24, 3, padding=1), nn.BatchNorm2d(24), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(24, 48, 3, padding=1), nn.BatchNorm2d(48), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(48, 96, 3, padding=1), nn.BatchNorm2d(96), nn.ReLU(), nn.MaxPool2d(2),
            nn.AdaptiveAvgPool2d(4),
        )
        self.head = nn.Sequential(nn.Flatten(), nn.Linear(96 * 4 * 4, 128), nn.ReLU(), nn.Linear(128, n_cls))
    def forward(self, x):
        return self.head(self.f(x))

model = TileNet().to(dev)
opt = optim.AdamW(model.parameters(), lr=1e-3)
lossf = nn.CrossEntropyLoss()

# train (PU soft-target nahi — real labels hain harvest se)
X_t = torch.from_numpy(X.transpose(0, 3, 1, 2)).to(dev)
Y_t = torch.from_numpy(Y).long().to(dev)
EPOCHS = int(os.environ.get("EPOCHS", "25"))
for ep in range(EPOCHS):
    model.train()
    opt.zero_grad()
    logits = model(X_t)
    loss = lossf(logits, Y_t)
    loss.backward()
    opt.step()
    acc = (logits.argmax(1) == Y_t).float().mean().item()
    if ep % 5 == 0 or ep == EPOCHS - 1:
        print(f"ep {ep} loss={loss.item():.4f} acc={acc:.3f}", flush=True)

# ---- ONNX export (batch dynamic) ----
os.makedirs(out_dir, exist_ok=True)
model.eval()
try:
    import torch.onnx as onnx
    dummy = torch.randn(1, 3, 96, 96).to(dev)
    onnx_path = os.path.join(out_dir, "tilenet_gpu.onnx")
    torch.onnx.export(model, dummy, onnx_path,
                      input_names=["tile"], output_names=["logits"],
                      dynamic_axes={"tile": {0: "batch"}})
    print("ONNX saved:", onnx_path, flush=True)
except Exception as e:
    print("ONNX err:", str(e)[:120], flush=True)

# save weights
torch.save(model.state_dict(), os.path.join(out_dir, "tilenet_gpu.pt"))
print("DONE. weights saved.", flush=True)
