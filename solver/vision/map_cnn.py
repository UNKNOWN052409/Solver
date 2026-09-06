"""map_cnn — small multi-head CNN for real text captchas (5 chars, 24 classes).

LOCAL-only, pure torch (no torchvision, no external AI API). Shared conv
backbone + 5 classifier heads (one per character position), 24 classes each
(alphabet 3479ACDEFHJKLMNPQRTUVWXY).

Architecture (128x128 input, flexible):
  conv3x3 24 -> ReLU -> MaxPool2   (64)
  conv3x3 48 -> ReLU -> MaxPool2   (32)
  conv3x3 96 -> ReLU -> MaxPool2   (16)
  conv3x3 128-> ReLU -> MaxPool2   (8)
  GAP -> fc(128)
  5 x fc(24)  heads

Only ~250k params -> trains fast on CPU even with 20 real images augmented.

Usage (build + smoke):
    python -m solver.vision.map_cnn
"""
import os

ALPHABET = "3479ACDEFHJKLMNPQRTUVWXY"
NUM_CHARS = 5
NUM_CLASSES = len(ALPHABET)          # 24
CHAR2IDX = {c: i for i, c in enumerate(ALPHABET)}
IDX2CHAR = ALPHABET


def build_model(channels=(24, 48, 96, 128), seed=0):
    import torch
    import torch.nn as nn

    torch.manual_seed(seed)
    class _MAP(nn.Module):
        def __init__(self):
            super().__init__()
            def block(cin, cout):
                return nn.Sequential(
                    nn.Conv2d(cin, cout, 3, padding=1),
                    nn.ReLU(),
                    nn.MaxPool2d(2))
            seq = []
            cin = 1
            for cout in channels:
                seq.append(block(cin, cout))
                cin = cout
            self.features = nn.Sequential(*seq)
            # heads: NCHW (8x8) -> GAP(128) -> head
            self.heads = nn.ModuleList(
                [nn.Linear(channels[-1], NUM_CLASSES) for _ in range(NUM_CHARS)])

        def forward(self, x):
            h = self.features(x)                 # (N, C, 8, 8)
            g = h.mean(dim=(2, 3))               # (N, C)
            return [head(g) for head in self.heads]   # list of 5 x (N,24)

    net = _MAP()
    return net


if __name__ == "__main__":
    import torch
    net = build_model()
    nparams = sum(p.numel() for p in net.parameters())
    print(f"map_cnn: {nparams:,} params | alphabet={ALPHABET} "
          f"({NUM_CLASSES} classes) x {NUM_CHARS} heads")
    x = torch.randn(2, 1, 128, 128)
    out = net(x)
    print("forward:", [tuple(o.shape) for o in out])
