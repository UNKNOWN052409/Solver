"""Adaptive device selection — CPU / CUDA / MPS (Apple) auto.

GPU ho to use karo (batching + AMP ke saath), warna CPU pe graceful.
Har platform pe ek hi code chalta hai:
    from solver.vision.device import pick_device, batch_size_for

    device = pick_device()          # 'cuda' | 'mps' | 'cpu'
    bs = batch_size_for(device)     # 64 (cuda) | 32 (mps) | 4 (cpu)
"""
import os


def pick_device():
    """Best available compute device — adaptive, no config needed."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda", f"CUDA:{torch.cuda.get_device_name(0)}"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps", "Apple-MPS"
        return "cpu", f"CPU({os.cpu_count() or 1} cores)"
    except ImportError:
        return "cpu", "CPU (torch missing)"


def batch_size_for(device, base=4):
    """GPU pe bada batch, CPU pe chota — memory-safe adaptive scaling."""
    if device == "cuda":
        # VRAM ke hisaab se: per-GB ~8 tiles (96x96 tiny model)
        try:
            import torch
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 2**30
            return max(16, min(256, int(vram_gb * 8)))
        except Exception:
            return 64
    if device == "mps":
        return 32
    return max(1, min(8, (os.cpu_count() or 2) // 2)) * base // 4 or 4


def amp_enabled(device):
    """Mixed-precision CUDA pe (2x speed, same accuracy)."""
    return device == "cuda"


def device_report():
    """Ek line me status — shortcut/CLI me print hota hai."""
    dev, desc = pick_device()
    bs = batch_size_for(dev)
    amp = "ON" if amp_enabled(dev) else "off"
    return f"device={dev} ({desc}) | batch={bs} | AMP={amp}"
