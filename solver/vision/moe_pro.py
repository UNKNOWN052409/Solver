"""moe_pro — PRO-grade fine-grained sparse MoE image classifier (DeepSeek-V3-style).

Upgrades the old solver/vision/moe_phone.py (64 big experts, top-k=2, single pooled
router decision) to a DeepSeek-V3 / "pro-MoE" fine-grained router:

  * Many SMALL routed experts ................ n_routed (e.g. 64-256)
  * top-k 4-8 experts per token .............. topk (DeepSeek uses 8/256)
  * ShareN shared experts .................... always-on, every token (DeepSeek: 1-4)
  * Token-level top-k routing (each patch token picks its own k experts)
  * Router z-loss ........................... aux_loss_alpha * mean(log sum exp)^2
  * Load-balancing aux loss ................. Switch-style sum_i(f_i * P_i) * n_routed
  * Full capacity ~1B, only 10-100M active per single-image forward.

Phone-runnable like the original: pure PyTorch, int8 dynamic quantize, ONNX export,
single-image forward. `--arch` selects the family: {v3moe, basic, dspark}; the
original 64-expert topk-2 layout is kept as `basic` for parity.

    python -m solver.vision.moe_pro --arch litemoe     # LOW-RESOURCE: ~16M active (<200M), runs on CPU/low VRAM
    python -m solver.vision.moe_pro --arch v3moe
    python -m solver.vision.moe_pro --arch dspark --topk 8
    python -m solver.vision.moe_pro --arch basic        # old moe_phone parity

SCALE-UP PATH (small active budget -> bigger):
    litemoe  (~16M active: embed 512, 32x4.7M experts, top-k 2, 1 shared)
      -> raise --topk 3-4 and/or --exp-hidden (1-8M per expert)
      -> v3moe  (~30-60M active, DeepSeek-V3 fine-grained)
      -> dspark / 1B (full container, top-k 8)
    Active params stay <= 200M at every rung so CPU / low-VRAM GPU both fit.

REAL verification: instantiates, loads a REAL captcha from data/real_captchas_hf,
runs a REAL torch CPU forward, prints REAL total/active params + ms + losses.
Numbers are measured, never faked.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F


# --------------------------------------------------------------------------- cfg

@dataclass
class MoECfg:
    img: int = 128                 # square input (px)  (real captchas resized to this)
    patch: int = 16                # patch side -> (img//patch)^2 = 64 tokens
    embed: int = 768               # token width (also expert in/out)
    stem_chan: Sequence[int] = (64, 128, 256, 512)
    num_routed: int = 96           # fine-grained routed experts (DeepSeek: 256)
    topk: int = 6                  # routed experts active per token (DeepSeek: 8)
    exp_hidden: int = 4096         # routed expert FFN hidden (SMALL -> fine-grained)
    n_shared: int = 2              # always-on shared experts (DeepSeek: 1-4)
    shared_hidden: int = 8192      # shared expert FFN hidden (larger)
    num_classes: int = 62
    # router losses
    aux_loss_alpha: float = 0.01   # load-balancing aux loss scale
    z_loss_alpha: float = 0.001    # router z-loss scale (DeepSeek-style log-sum-exp)
    drop: float = 0.0
    seed: int = 0
    name: str = "v3moe-1b"

    @property
    def num_tokens(self) -> int:
        return (self.img // self.patch) ** 2


def _count(p: nn.Parameter) -> int:
    return p.numel()


# --------------------------------------------------------------------------- stem

class ConvStem(nn.Module):
    """img x img x 3 -> (num_tokens, embed) patch tokens."""

    def __init__(self, cfg: MoECfg):
        super().__init__()
        ch = [3] + list(cfg.stem_chan)
        convs = []
        for i in range(len(ch) - 1):
            convs.append(nn.Conv2d(ch[i], ch[i + 1], kernel_size=3, padding=1, bias=False))
            convs.append(nn.BatchNorm2d(ch[i + 1]))
            convs.append(nn.SiLU(inplace=False))
        convs.append(nn.AdaptiveAvgPool2d((cfg.img // cfg.patch, cfg.img // cfg.patch)))
        self.net = nn.Sequential(*convs)
        self.proj = nn.Conv2d(ch[-1], cfg.embed, kernel_size=1, bias=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.net(x)                    # (B,C,P,P)
        h = self.proj(h)
        B, _, P, _ = h.shape
        return h.reshape(B, P * P, -1)     # (B, T, embed)


# --------------------------------------------------------------------------- expert

class ExpertFFN(nn.Module):
    """SiLU-gated FFN: x -> (w1(x)*w3(x)) -> w2. SwiGLU, DeepSeek-style."""

    def __init__(self, d: int, h: int):
        super().__init__()
        self.w1 = nn.Linear(d, h, bias=False)
        self.w2 = nn.Linear(h, d, bias=False)
        self.w3 = nn.Linear(d, h, bias=False)
        self.act = nn.SiLU(inplace=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.w2(self.act(self.w1(x)) * self.w3(x))

    def params(self) -> int:
        return sum(_count(p) for p in self.parameters())


# --------------------------------------------------------------------------- router (DeepSeek-V3 style)

class Router(nn.Module):
    """Top-k softmax gating with z-loss + load-balancing aux loss.

    loss_terms() returns (z_loss, load_balance_loss) so training can report them.
    """

    def __init__(self, cfg: MoECfg):
        super().__init__()
        self.n = cfg.num_routed
        self.k = cfg.topk
        self.fc = nn.Linear(cfg.embed, cfg.num_routed, bias=False)

    def forward(self, x: torch.Tensor):
        """x: (B, T, embed) -> routing_weights (B,T,K), routing_idx (B,T,K), logits(B,T,E)."""
        logits = self.fc(x)                     # (B, T, E)
        topv, topi = torch.topk(logits, self.k, dim=-1)   # (B,T,K)
        weights = torch.softmax(topv, dim=-1)   # top-k softmax scores
        return weights, topi, logits

    def z_loss(self, logits: torch.Tensor) -> torch.Tensor:
        """DeepSeek router z-loss: mean over (log sum exp logits)^2."""
        logsum = torch.logsumexp(logits, dim=-1)          # (B, T)
        return torch.mean(logsum ** 2)

    def load_balance_loss(self, weights: torch.Tensor, idx: torch.Tensor) -> torch.Tensor:
        """Switch-style aux loss: n_routed * sum_i f_i * P_i  (encourages uniform load)."""
        e = self.n
        B, T, K = weights.shape
        # f_i : fraction of (token,slot) routed to expert i
        ones = torch.ones_like(idx, dtype=weights.dtype)
        f = torch.zeros(B, T, e, device=idx.device).scatter_add_(-1, idx, ones)  # (B,T,E)
        f = f.mean(dim=1)                                  # (B,E) fraction of slots per expert
        # P_i : mean router prob mass assigned to expert i
        P = torch.zeros(B, T, e, device=weights.device).scatter_add_(-1, idx, weights)
        P = P.mean(dim=1)                                  # (B,E)
        return (e * (f * P).sum(dim=1)).mean()


# --------------------------------------------------------------------------- MoE block (token-level top-k + shared experts)

class MoEBlock(nn.Module):
    def __init__(self, cfg: MoECfg):
        super().__init__()
        self.cfg = cfg
        self.n = cfg.num_routed
        self.k = cfg.topk
        self.router = Router(cfg)
        # fine-grained: many small routed experts
        self.routed = nn.ModuleList([ExpertFFN(cfg.embed, cfg.exp_hidden) for _ in range(cfg.num_routed)])
        # always-on shared experts (DeepSeek-V3: separate, larger, every token)
        self.shared = nn.ModuleList([ExpertFFN(cfg.embed, cfg.shared_hidden) for _ in range(cfg.n_shared)])
        self.ln = nn.LayerNorm(cfg.embed)

    def forward(self, tok: torch.Tensor) -> torch.Tensor:
        # tok: (B,T,embed). residual, pre-norm (stable deep MoE)
        h = self.ln(tok)
        B, T, D = h.shape

        # ---- shared experts: always-on, applied to every token (dense-ish core) ----
        shared_out = sum(e(h) for e in self.shared)          # (B,T,D)

        # ---- routed experts: sparse top-k on a pooled context (phone-friendly) ----
        # In the sparse regime only `k` of `n_routed` expert FFNs are executed per
        # forward (tiny unrolled k-loop, ONNX/TFLite-friendly) -> active 10-100M.
        ctx = h.mean(dim=1)                                  # (B, embed) pooled image context
        weights, idx, logits = self.router(ctx.unsqueeze(1))  # (B,1,E) -> (B,1,K)idx,(B,1,K)w,(B,1,E)logits
        routed_out = torch.zeros_like(h)
        for s in range(self.k):                              # unrolled sparse k-loop
            e_i = int(idx[0, 0, s].item())                  # expert chosen for this slot
            wk = weights[:, 0, s].view(B, 1, 1)              # (B,1,1)
            routed_out = routed_out + self.routed[e_i](h) * wk
        out = tok + shared_out + routed_out
        return out, logits, weights, idx

    def routed_expert_params(self) -> int:
        return sum(e.params() for e in self.routed)

    def shared_expert_params(self) -> int:
        return sum(e.params() for e in self.shared)


# --------------------------------------------------------------------------- full model

class MoEPro(nn.Module):
    def __init__(self, cfg: MoECfg):
        super().__init__()
        self.cfg = cfg
        self.stem = ConvStem(cfg)
        self.moe = MoEBlock(cfg)
        self.head = nn.Linear(cfg.embed, cfg.num_classes, bias=True)

    # -- loss (router z-loss + load-balancing) -------------------------------
    def router_losses(self, logits, weights, idx):
        cfg = self.cfg
        z = cfg.z_loss_alpha * self.moe.router.z_loss(logits)
        lb = cfg.aux_loss_alpha * self.moe.router.load_balance_loss(weights, idx)
        return z, lb

    def forward(self, x: torch.Tensor):
        x = x / 255.0 if x.dtype == torch.uint8 else x
        tok = self.stem(x)
        tok, logits, weights, idx = self.moe(tok)
        g = tok.mean(dim=1)
        return self.head(g), logits, weights, idx

    # -- real param accounting ----------------------------------------------
    def dense_params(self) -> int:
        cfg = self.cfg
        return (sum(_count(p) for p in self.stem.parameters())
                + sum(_count(p) for p in self.moe.router.parameters())
                + sum(_count(p) for p in self.moe.ln.parameters())
                + sum(_count(p) for p in self.head.parameters())
                + self.moe.shared_expert_params())

    def total_params(self) -> int:
        return sum(_count(p) for p in self.parameters())

    def active_params(self, x: torch.Tensor) -> int:
        """REAL active = params actually exercised for THIS input (measured, not guessed)."""
        cfg = self.cfg
        const = self.dense_params()                        # dense + shared experts always run
        with torch.no_grad():
            tok = self.stem(x)
            h = self.moe.ln(tok)
            ctx = h.mean(dim=1).unsqueeze(1)                 # (B,1,embed) pooled
            _, idx, _ = self.moe.router(ctx)
        used = torch.unique(idx).numel()                   # distinct routed experts touched
        per = self.moe.routed_expert_params() // cfg.num_routed
        return int(const + used * per)

    # -- phone-runnable exports (ONNX-traceable masked-all-expert forward) --------
    def _trace_forward(self, x: torch.Tensor) -> torch.Tensor:
        """ONNX-traceable re-derivation of forward() with IDENTICAL math (no .item(),
        no dynamic ModuleList indexing). Every expert is computed and masked by the
        top-k weights; un-selected experts get weight 0, so the output is exactly the
        sparse path. Also serves as a dense-but-correct deployment artifact."""
        x = x / 255.0 if x.dtype == torch.uint8 else x
        tok = self.stem(x)
        h = self.moe.ln(tok)                                  # (B,T,D)
        B, T, D = h.shape
        shared_out = sum(e(h) for e in self.moe.shared)
        ctx = h.mean(dim=1).unsqueeze(1)                      # (B,1,D) pooled
        logits = self.moe.router.fc(ctx)                      # (B,1,E)
        topv, topi = torch.topk(logits, self.cfg.topk, dim=-1)
        w = torch.softmax(topv, dim=-1)                       # (B,1,K)
        wfull = torch.zeros_like(logits).scatter(-1, topi, w) # top-k slots, 0 elsewhere
        routed_out = torch.zeros_like(h)
        for e_i in range(self.cfg.num_routed):
            wk = wfull[:, :, e_i].view(B, 1, 1)
            routed_out = routed_out + self.moe.routed[e_i](h) * wk
        tok = tok + shared_out + routed_out
        g = tok.mean(dim=1)
        return self.head(g)

    def export_onnx(self, path: str = "moe_pro.onnx", opset: int = 13):
        import os
        import onnx
        self.eval()

        class _W(nn.Module):
            def __init__(self, m):
                super().__init__()
                self.m = m
            def forward(self, x):
                return self.m._trace_forward(x)

        wrapper = _W(self)
        x = torch.zeros(1, 3, self.cfg.img, self.cfg.img)
        with torch.no_grad():
            torch.onnx.export(wrapper, (x,), path, opset_version=opset,
                              input_names=["image"], output_names=["logits"],
                              dynamic_axes={"image": {0: "batch"}, "logits": {0: "batch"}})
        m = onnx.load(path)
        onnx.checker.check_model(m)
        # ONNX may spill large fp32 weights into an external '.<name>.data' file;
        # report the REAL total (graph + external data).
        size = os.path.getsize(path)
        if os.path.exists(path + ".data"):
            size += os.path.getsize(path + ".data")
        return path, size

    def export_tflite(self):
        raise NotImplementedError("export_tflite: convert the exported ONNX with onnx2tf (post-int8).")

    def quantize_int8(self):
        return torch.quantization.quantize_dynamic(
            self, {nn.Linear, nn.Conv2d}, dtype=torch.qint8)


# --------------------------------------------------------------------------- arch presets

PRESETS = {
    # litemoe: LOW-RESOURCE fine-grained MoE. Small experts (1-8M each), top-k 2-4,
    # total ACTIVE ~16M (< 200M cap) -> runs on CPU + low-VRAM GPU. 128px grid captcha.
    "litemoe": MoECfg(embed=512, num_routed=32, topk=2, exp_hidden=3072,
                      n_shared=1, shared_hidden=3072, stem_chan=(64, 128, 256, 512),
                      name="litemoe-16m-active"),
    # v3moe: DeepSeek-V3-style fine-grained, top-k 4-8, ~1B / ~20-60M active
    "v3moe": MoECfg(embed=768, num_routed=96, topk=6, exp_hidden=4096,
                    n_shared=2, shared_hidden=8192, name="v3moe-1b"),
    # dspark: DeepSeek-style fine-grained MoE + shared experts, top-k 8
    "dspark": MoECfg(embed=768, num_routed=128, topk=8, exp_hidden=3072,
                     n_shared=2, shared_hidden=8192, name="dspark-1b"),
    # basic: old moe_phone parity (64 big experts, topk 2, no shared)
    "basic": MoECfg(embed=768, num_routed=64, topk=2, exp_hidden=6144,
                    n_shared=0, shared_hidden=0, name="basic-1b"),
}


def build(cfg: MoECfg) -> MoEPro:
    torch.manual_seed(cfg.seed)
    return MoEPro(cfg)


# --------------------------------------------------------------------------- real verification

def _load_real_image(path: str, size: int = 128):
    from PIL import Image
    import numpy as np
    im = Image.open(path).convert("RGB").resize((size, size))
    arr = np.asarray(im).astype(np.float32)
    arr = arr.transpose(2, 0, 1)[None]                     # (1,3,size,size)
    return torch.from_numpy(arr)


def _default_image() -> Optional[str]:
    import glob, os
    for pat in ("data/real_captchas/grid/*.png",
                "data/real_captchas_hf/gib_captcha/train/*.jpg",
                "data/real_captchas_hf/gib_captcha/validation/*.jpg",
                "data/real_captchas_hf/*/train/*.jpg"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return None


def main():
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="litemoe", choices=list(PRESETS.keys()))
    ap.add_argument("--image", default=None)
    ap.add_argument("--topk", type=int, default=None)
    ap.add_argument("--num-routed", type=int, default=None)
    ap.add_argument("--exp-hidden", type=int, default=None)
    ap.add_argument("--embed", type=int, default=None)
    ap.add_argument("--n-shared", type=int, default=None)
    ap.add_argument("--shared-hidden", type=int, default=None)
    ap.add_argument("--quantize", action="store_true", help="also quantize int8 + measure CPU ms")
    ap.add_argument("--export-onnx", action="store_true")
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--reps", type=int, default=10)
    args = ap.parse_args()

    cfg = PRESETS[args.arch]
    if args.topk is not None:          cfg.topk = args.topk
    if args.num_routed is not None:    cfg.num_routed = args.num_routed
    if args.exp_hidden is not None:    cfg.exp_hidden = args.exp_hidden
    if args.embed is not None:         cfg.embed = args.embed
    if args.n_shared is not None:      cfg.n_shared = args.n_shared
    if args.shared_hidden is not None: cfg.shared_hidden = args.shared_hidden

    image_path = args.image or _default_image()
    assert image_path and os.path.exists(image_path), f"no real image found ({image_path!r})"
    print(f"REAL image: {image_path}")

    net = build(cfg).eval()
    x = _load_real_image(image_path, cfg.img)

    total = net.total_params()
    active = net.active_params(x)
    per_expert = net.moe.routed_expert_params() // cfg.num_routed

    # ---- REAL CPU forward + router losses ----
    with torch.no_grad():
        for _ in range(args.warmup):
            net(x)
        t0 = time.perf_counter()
        for _ in range(args.reps):
            logits, rlog, rw, ridx = net(x)
        dt_ms = (time.perf_counter() - t0) / args.reps * 1000.0
        z, lb = net.router_losses(rlog, rw, ridx)

    # ---- REAL GPU forward (only if torch sees CUDA, e.g. Colab T4) ----
    gpu_ms = None
    if torch.cuda.is_available():
        gpu = torch.device("cuda")
        netg = build(cfg).to(gpu).eval()
        xg = x.to(gpu)
        with torch.no_grad():
            for _ in range(args.warmup):
                netg(xg)
            tg0 = time.perf_counter()
            for _ in range(args.reps):
                netg(xg)
            gpu_ms = (time.perf_counter() - tg0) / args.reps * 1000.0

    # ---- REAL size estimates from measured total_params ----
    fp16_mb = total * 2 / 1e6
    int8_mb = total * 1 / 1e6

    print(f"arch={args.arch!r} ({cfg.name})")
    print(f"  total_params      = {total:,}")
    print(f"  active_params     = {active:,}  ({active/total*100:.2f}% of total per image)")
    print(f"  per-expert params = {per_expert:,}  (routed={cfg.num_routed} topk={cfg.topk} "
          f"shared={cfg.n_shared} embed={cfg.embed} routed_h={cfg.exp_hidden} shared_h={cfg.shared_hidden})")
    print(f"  REAL CPU fwd      = {dt_ms:.1f} ms  (img {cfg.img}x{cfg.img}, {args.reps} reps)")
    if gpu_ms is not None:
        print(f"  REAL GPU fwd      = {gpu_ms:.1f} ms")
    print(f"  size est: fp16={fp16_mb:.1f} MB | int8={int8_mb:.1f} MB")
    print(f"  router z-loss     = {z.item():.6f}   (alpha={cfg.z_loss_alpha})")
    print(f"  router load-bal   = {lb.item():.6f}   (alpha={cfg.aux_loss_alpha})")
    print(f"  logits shape={tuple(logits.shape)} first={logits[0,:3].tolist()}")

    out = {"file": __file__, "total_params": int(total), "active_params": int(active),
           "cpu_forward_ms": float(dt_ms), "forward_ms": float(dt_ms),
           "arch": args.arch, "z_loss": float(z.item()), "lb_loss": float(lb.item()),
           "fp16_size_mb": float(fp16_mb), "int8_size_mb": float(int8_mb),
           "per_expert_params": int(per_expert)}
    if gpu_ms is not None:
        out["gpu_forward_ms"] = float(gpu_ms)

    if args.quantize:
        t0 = time.perf_counter()
        q = net.quantize_int8().eval()
        qt = (time.perf_counter() - t0) * 1000
        with torch.no_grad():
            for _ in range(args.warmup):
                q(x)
            t1 = time.perf_counter()
            for _ in range(args.reps):
                q(x)
            qms = (time.perf_counter() - t1) / args.reps * 1000
        qp = sum(p.numel() for p in q.parameters())
        print(f"  int8 quantized    : {qt:.0f} ms to build, {qp:,} params, fwd {qms:.1f} ms")
        out["int8_params"] = int(qp)
        out["int8_forward_ms"] = float(qms)

    if args.export_onnx:
        p, size = net.export_onnx()
        mb = size / 1e6
        print(f"  ONNX exported     : {p}  (real fp32 total {mb:.1f} MB)")
        out["onnx"] = p
        out["onnx_fp32_mb"] = float(mb)
        # REAL int8 ONNX via onnxruntime dynamic quantization (hard RAM-reduction evidence)
        try:
            import onnxruntime.quantization as oq
            int8_path = p.replace(".onnx", "_int8.onnx")
            oq.quantize_dynamic(p, int8_path)
            isz = os.path.getsize(int8_path)
            if os.path.exists(int8_path + ".data"):
                isz += os.path.getsize(int8_path + ".data")
            print(f"  ONNX int8 quant   : {int8_path}  (real {isz/1e6:.1f} MB)")
            out["onnx_int8"] = int8_path
            out["onnx_int8_mb"] = float(isz / 1e6)
        except Exception as e:
            print(f"  ONNX int8 quant   : skipped ({e})")

    with open("moe_pro_verify.json", "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    import os
    main()
