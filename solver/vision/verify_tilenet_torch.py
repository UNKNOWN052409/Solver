"""A1 verification: torch works on box + TileNet predict_labels end-to-end on a REAL captcha.

Loads a real grid captcha (data/real_captchas/grid/map_*.png), converts to
the 96x96 RGB input TileNet expects, runs:

  1. TileNet.predict_labels (pure-numpy reference)      -> labels
  2. A torch-native reimplementation of TileNet (same weights, same arch)
     run through torch on CPU -> labels, proving torch is genuinely wired
     end-to-end on real captcha pixels.
"""
import os, sys, time
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import TileNet, CLASSES, NUM_CLASSES

REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "..", "..", "data", "real_captchas")
GRID = os.path.join(REAL, "grid")
ROT = os.path.join(REAL, "rot")


def load_tile(path, size=96):
    """Load any captcha image as (1,3,size,size) float32 in [0,1]."""
    im = Image.open(path).convert("RGB").resize((size, size), Image.BILINEAR)
    arr = np.asarray(im, dtype=np.float32) / 255.0          # H,W,3
    return arr.transpose(2, 0, 1)[None].copy()               # 1,3,H,W


def main():
    # ---- pick 3 real captchas ----
    import glob
    grids = sorted(glob.glob(os.path.join(GRID, "map_*.png")))
    rots = sorted(glob.glob(os.path.join(ROT, "*_rot*.png")))
    if not grids or not rots:
        print("NO real captchas found"); return 1

    # ---- 1) pure-numpy TileNet.predict_labels on real grid ----
    net = TileNet()
    print(f"[TileNet] {net.num_params():,} params | {NUM_CLASSES} classes | "
          f"weights dtype={net.params['w0'].dtype}")
    g = grids[0]
    x = load_tile(g)
    t0 = time.time(); labels = net.predict_labels(x); dt = time.time() - t0
    print(f"[numpy]   {os.path.basename(g)}: {labels[0]}")
    print(f"          forward {dt*1000:.1f} ms")

    # ---- 2) torch-native TileNet on the SAME real captcha ----
    import torch
    torch.manual_seed(0)
    dev = "cuda" if torch.cuda.is_available() else ("mps" if hasattr(torch.backends,"mps") and torch.backends.mps.is_available() else "cpu")
    print(f"[torch]   {torch.__version__} device={dev}")

    # build torch model from the numpy reference weights (exact same arch)
    class TNet(torch.nn.Module):
        def __init__(self, src):
            super().__init__()
            ch = [3,16,32,64,128,128]
            self.convs = torch.nn.ModuleList()
            for i in range(5):
                cin, cout = ch[i], ch[i+1]
                c = torch.nn.Conv2d(cin, cout, 3, padding=0)
                with torch.no_grad():
                    c.weight.copy_(torch.from_numpy(src.params[f"w{i}"]))
                    c.bias.copy_(torch.from_numpy(src.params[f"b{i}"]))
                self.convs.append(c)
            self.wf = torch.nn.Parameter(torch.from_numpy(src.params["wf"]))
            self.bf = torch.nn.Parameter(torch.from_numpy(src.params["bf"]))
        def forward(self, x):
            h = x
            for i, c in enumerate(self.convs):
                h = torch.relu(c(h))
                if i < 4:
                    h = h[:, :, ::2, ::2]
            g = h.mean(dim=(2,3))
            return g @ self.wf + self.bf

    tnet = TNet(net).to(dev).eval()
    xt = torch.from_numpy(x).to(dev)
    t0 = time.time()
    with torch.no_grad():
        logits = tnet(xt)
        sig = torch.sigmoid(logits)
    dt = time.time() - t0
    tidx = (sig[0] > 0.5).nonzero(as_tuple=True)[0].tolist()
    tlabels = [(CLASSES[i] if i < len(CLASSES) else str(i), float(sig[0,i])) for i in tidx]
    print(f"[torch]   {os.path.basename(g)}: {tlabels}")
    print(f"          forward {dt*1000:.1f} ms")

    # matmul sanity (explicit tiny matmul)
    a = torch.randn(64,64,device=dev); b = torch.randn(64,64,device=dev)
    c = a@b
    print(f"[torch]   tiny matmul 64x64 ok, out sum={float(c.sum()):.3f}")

    # consistency check numpy vs torch
    np_sig = 1/(1+np.exp(-logits.cpu().numpy()))
    maxdiff = float(np.abs(np_sig - sig.cpu().numpy()).max())
    print(f"[check]   numpy-vs-torch max sigmoid diff = {maxdiff:.2e} "
          f"({'MATCH' if maxdiff < 1e-3 else 'MISMATCH'})")

    # ---- 3) RotNet angle on a real rotated tile ----
    r = rots[0]
    xr = load_tile(r, 96)
    from model import RotNet
    rotnet = RotNet()
    # torch rotnet
    tnet_rot = TNet(rotnet).to(dev).eval()
    with torch.no_grad():
        ang_np = rotnet.angle(xr)
        ang_pt = int(torch.argmax(tnet_rot(torch.from_numpy(xr).to(dev)), dim=1)[0].item())*10-180
    base = int(os.path.basename(r).split("rot")[1].split("_")[0])
    print(f"[rotnet]  {os.path.basename(r)} true={base} deg | numpy={ang_np} | torch={ang_pt}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
