"""GPU connectivity layer — CUDA vram / local GPU discovery + backend resolution.

TileNet training & inference never *require* a GPU, but when one is present we
use it. This module is the single place that answers:

    * is there a CUDA GPU on THIS box? how much VRAM?
    * what local GPUs exist (CUDA list / MPS / CPU-only)?
    * can we run the TileNet compute path on GPU here?
    * what compute backend should we use, given the GPU_TARGET env var?

GPU_TARGET env values (resolved by ``resolve_backend()``):

    auto    -> best reachable, falling back to local CPU/CUDA
    local   -> local torch device (cuda if present else cpu)
    colab   -> torch on a (colab) TPU/GPU machine — resolved identically to
               ``local`` but reported as colab; needs torch/xla at runtime
    api     -> EXTERNAL_INFERENCE_URL OpenAI-style endpoint (http(s) POST
               /v1/images/... or a custom route). Falls back to local.
    cli     -> subprocess to a CLI inference binary (env INFERENCE_CLI).
               Falls back to local.
    remote  -> ssh torchrun to a remote host (env REMOTE_TORCHRUN_HOST).
               Falls back to local.

**Rule (LO): captcha solving is AI-INDEPENDENT — no external AI API.** The
``api`` / ``cli`` / ``remote`` targets exist only for *optional* acceleration
of the model-training side; inference on a captcha tile ALWAYS falls back to
local CPU/CUDA when those targets are unreachable. Nothing here ever requires
a network call to solve a captcha.
"""
from __future__ import annotations

import os
import shutil
import subprocess

# --------------------------------------------------------------------------
# local GPU primitives (torch-free where possible, so they work on this box)
# --------------------------------------------------------------------------


def detect_cuda_vram() -> dict:
    """Return VRAM summary for the local CUDA device(s).

    Returns dict: {available: bool, devices: [{index, name, total_vram_mb,
    free_vram_mb}]}. Uses nvidia-smi when torch is absent, else torch.
    Never raises — best-effort detection.
    """
    out = {"available": False, "devices": []}
    # 1) try torch first (richest metadata)
    try:
        import torch
        if torch.cuda.is_available():
            n = torch.cuda.device_count()
            for i in range(n):
                props = torch.cuda.get_device_properties(i)
                total = props.total_memory // (1024 * 1024)
                free = 0
                try:
                    free = (torch.cuda.mem_get_info(i)[0]) // (1024 * 1024)
                except Exception:
                    free = total
                out["devices"].append({
                    "index": i,
                    "name": props.name,
                    "total_vram_mb": int(total),
                    "free_vram_mb": int(free),
                })
            out["available"] = True
            return out
    except ImportError:
        pass
    except Exception:
        pass
    # 2) torch missing / no cuda -> nvidia-smi
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            for line in (r.stdout or "").strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 4:
                    out["devices"].append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "total_vram_mb": int(float(parts[2])),
                        "free_vram_mb": int(float(parts[3])),
                    })
            out["available"] = len(out["devices"]) > 0
        except Exception:
            pass
    return out


def list_local_gpus() -> list[dict]:
    """Human/script-friendly list of available compute devices on this box.

    Returns a list of dicts:
        {kind: 'cuda'|'mps'|'cpu', label: str, detail: str}
    CUDA entries come from detect_cuda_vram(); MPS from torch backend (Apple);
    a 'cpu' entry is always last.
    """
    gpus = []
    for d in detect_cuda_vram()["devices"]:
        gpus.append({
            "kind": "cuda",
            "label": f"cuda:{d['index']} {d['name']}",
            "detail": f"{d['free_vram_mb']}/{d['total_vram_mb']} MB",
        })
    if not gpus:
        try:
            import torch
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                gpus.append({"kind": "mps", "label": "mps (Apple)",
                             "detail": "Metal GPU"})
        except Exception:
            pass
    if not gpus:
        gpus.append({"kind": "cpu", "label": "cpu",
                     "detail": f"{os.cpu_count() or 1} cores"})
    return gpus


def can_run_gpu() -> bool:
    """True if the TileNet compute path can run on a GPU right now.

    Requires an actual backend (torch CUDA or MPS) — presence of an
    nvidia-smi GPU alone is not enough if torch isn't installed.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return True
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return True
        return False
    except Exception:
        return False


# --------------------------------------------------------------------------
# backend resolution from GPU_TARGET
# --------------------------------------------------------------------------

_TARGETS = ("local", "colab", "api", "cli", "remote", "auto")


def _fallback_note(reason: str) -> dict:
    """Backend dict that says: run on whatever local device exists (cpu/cuda)."""
    try:
        import torch
        dev = "cuda" if torch.cuda.is_available() else \
              ("mps" if (getattr(torch.backends, "mps", None)
                         and torch.backends.mps.is_available()) else "cpu")
    except Exception:
        dev = "cpu"
    note = f"target unreachable ({reason}); fallback to local {dev}"
    return {"backend": "local", "device_or_url": dev,
            "ready": dev != "cpu" or True, "note": note}


def _resolve(target: str) -> dict:
    """Resolve one GPU_TARGET value -> {backend, device_or_url, ready, note}.

    Every non-local target degrades to ``local`` CPU/CUDA when unreachable.
    """
    target = (target or "auto").strip().lower()
    if target not in _TARGETS:
        return _fallback_note(f"unknown GPU_TARGET={target!r}")

    if target in ("local", "auto"):
        dev, desc = "cpu", "CPU"
        try:
            import torch
            if torch.cuda.is_available():
                dev, desc = "cuda", f"CUDA:{torch.cuda.get_device_name(0)}"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                dev, desc = "mps", "Apple-MPS"
        except ImportError:
            desc = "CPU (torch missing)"
        return {"backend": "local", "device_or_url": dev,
                "ready": True, "note": f"local torch on {desc}"}

    if target == "colab":
        # colab = torch on that machine's GPU/TPU; from a local box the
        # practical route is identical to local torch. Report as colab.
        try:
            import torch
            if torch.cuda.is_available():
                return {"backend": "colab", "device_or_url": "cuda",
                        "ready": True, "note": "torch CUDA available"}
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return {"backend": "colab", "device_or_url": "mps",
                        "ready": True, "note": "torch MPS available"}
            return {"backend": "colab", "device_or_url": "cpu",
                    "ready": True, "note": "no GPU found (CPU)"}
        except ImportError:
            return _fallback_note("colab needs torch (not installed)")

    if target == "api":
        url = os.environ.get("EXTERNAL_INFERENCE_URL", "").strip()
        if not url:
            return _fallback_note("EXTERNAL_INFERENCE_URL not set")
        url = url if url.startswith(("https://", "http://")) else f"https://{url}"
        # We don't hard-fail on unreachable — readiness requires the env to be
        # set; the *caller* probes reachability and falls back to local if 4xx/5xx.
        return {"backend": "api", "device_or_url": url,
                "ready": bool(url), "note": f"external inference endpoint {url}"}

    if target == "cli":
        cli = os.environ.get("INFERENCE_CLI", "").strip()
        if not cli or not shutil.which(cli.split()[0]):
            return _fallback_note("INFERENCE_CLI not found on PATH")
        return {"backend": "cli", "device_or_url": cli,
                "ready": True, "note": f"CLI inference binary {cli.split()[0]}"}

    if target == "remote":
        host = os.environ.get("REMOTE_TORCHRUN_HOST", "").strip()
        if not host:
            return _fallback_note("REMOTE_TORCHRUN_HOST not set")
        return {"backend": "remote", "device_or_url": host,
                "ready": True, "note": f"ssh torchrun @ {host}"}

    return _fallback_note("unhandled target")


def resolve_backend(env_target: str | None = None) -> dict:
    """Public entry — resolve GPU_TARGET (or provided value) to a backend dict.

    Example:
        >>> resolve_backend("api")
        {"backend": "api", "device_or_url": "https://...", "ready": True, "note": ...}
    """
    if env_target is None:
        env_target = os.environ.get("GPU_TARGET", "auto")
    return _resolve(env_target)


def backend_summary(env_target: str | None = None) -> str:
    """One-line human summary (used in device_report)."""
    b = resolve_backend(env_target)
    return (f"backend={b['backend']} target={b['device_or_url']} "
            f"ready={b['ready']} ({b['note']})")


if __name__ == "__main__":
    print("VRAM   :", detect_cuda_vram())
    print("GPUs   :", list_local_gpus())
    print("can_run:", can_run_gpu())
    print("summary:", backend_summary())
