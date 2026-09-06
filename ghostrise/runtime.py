"""GhostRise run-mode resolver — GPU vs CPU vs low-RAM.

Picks a browsing profile before the engine launches:

  * detect_nv_gpu()   -> does this box have a usable NVIDIA GPU
                        (nvidia-smi present, CUDA_VISIBLE_DEVICES, colab
                        COLAB_GPU, a --gpu flag, and /dev/dri for iGPU)?
  * detect_low_ram()  -> is this a <~1GB RAM / phone-proot box (or was
                        --low-ram forced)?
  * tier()            -> engine Tier mirror of engine/src/adaptive.rs
                        (Low <1GB, Mid 1-4GB, High 4GB+).
  * browser_args()    -> per-tier chromium launch flags.
  * resolve()         -> dict {gpu, tier, browser_args, ...} — the single
                        entry point the CLI calls.
  * launch_browser()  -> builds a GhostWire (ghostrise.wire) pre-fed with
                        the resolved browser_args.

No GPU needed to import — this file degrades to CPU automatically.
"""

from __future__ import annotations

import os
import shutil
from typing import Optional

# ---------------------------------------------------------------- tier ---
# Mirror of engine/src/adaptive.rs Tier/from_mem_kb so Python can decide the
# same way the Rust engine does without spawning the binary.

TIER_LOW = "low"
TIER_MID = "mid"
TIER_HIGH = "high"


def mem_total_kb() -> Optional[int]:
    """/proc/meminfo MemTotal in kB — None if unavailable."""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def from_mem_kb(kb: int) -> str:
    """Tier by RAM, matching adaptive.rs from_mem_kb (1MB = 1024 kB)."""
    gb = kb / 1_048_576
    if gb < 1:
        return TIER_LOW
    if gb < 4:
        return TIER_MID
    return TIER_HIGH


def tier() -> str:
    """Detect tier from live memory (Rust default behavior)."""
    kb = mem_total_kb()
    if kb is None:
        return TIER_MID  # unknown -> conservative desktop default
    return from_mem_kb(kb)


def detect_low_ram(force: bool = False) -> bool:
    """Low-RAM if forced or if < ~1GB of RAM is visible.

    `force` is set from the --low-ram flag (user wants the 100MB mode on
    even on a box that reports plenty of RAM — e.g. a proot with a hard
    cgroup cap invisible to /proc/meminfo).
    """
    if force:
        return True
    kb = mem_total_kb()
    if kb is None:
        return False
    return kb < 1_048_576  # < 1GB


# ----------------------------------------------------------------- gpu ---
_NVIDIA_INDICATORS = ("nvidia-smi", "nvidia-cuda-mps-control")
_GPU_ENV_VARS = ("CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "COLAB_GPU")


def _env_gpu_flag() -> bool:
    """Mounted GPU signalled by env (colab GPU, docker --gpus, etc)."""
    for var in _GPU_ENV_VARS:
        v = os.environ.get(var, "").strip()
        if v and v != "0":
            return True
    return False


def detect_nv_gpu(force: bool = False) -> bool:
    """True when a usable NVIDIA GPU is present.

    Force takes precedence (--gpu flag).  Otherwise we look for an
    nvidia-smi binary in PATH, an env that mounts a GPU, or any
    /dev/nvidia* device node.  Also reports an iGPU (/dev/dri) separately
    so callers can enable hardware accel even without NVIDIA.
    """
    if force:
        return True
    if shutil.which("nvidia-smi"):
        return True
    for ind in _NVIDIA_INDICATORS:
        if shutil.which(ind):
            return True
    if _env_gpu_flag():
        return True
    try:
        for dev in os.listdir("/dev"):
            if dev.startswith("nvidia"):
                return True
    except OSError:
        pass
    return False


def detect_igpu() -> bool:
    """Intel/AMD iGPU exposed via /dev/dri/cardN (safe to accel on)."""
    try:
        if not os.path.isdir("/dev/dri"):
            return False
        return any(d.startswith("card") for d in os.listdir("/dev/dri"))
    except OSError:
        return False


# ---------------------------------------------------------- browser args ---
LOW_RAM_ARGS = [
    "--disable-gpu",            # no rasterisation, save VRAM/RAM
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-features=Translate,PermissionsPolicy",
    "--js-flags=--max-old-space-size=64",
    "--renderer-process-limit=1",
    "--no-zygote",
]

GPU_ARGS = [
    "--enable-gpu",             # GPU raster / compositing
    "--enable-gpu-rasterization",
    "--enable-zero-copy",
    "--ignore-gpu-blocklist",
]

IGPU_ARGS = ["--enable-gpu", "--use-gl=angle"]


def browser_args(gpu: bool, low_ram: bool, has_igpu: bool) -> list[str]:
    """Chromium flags for the resolved tier.

    low_ram always wins on saving memory (it disables the GPU); otherwise a
    usable GPU (nvidia or iGPU) enables acceleration.
    """
    if low_ram:
        return list(LOW_RAM_ARGS)
    if gpu or has_igpu:
        return list(GPU_ARGS)
    return []  # plain CPU


# -------------------------------------------------------------- resolve ---
def resolve(*, gpu: Optional[bool] = None, low_ram: Optional[bool] = None) -> dict:
    """Resolve the run profile -> dict.

    Returns
    -------
    {
      "gpu": bool,            # NVIDIA/CUDA GPU usable
      "igpu": bool,           # /dev/dri iGPU present
      "low_ram": bool,        # low-RAM mode on
      "tier": "low"|"mid"|"high",
      "browser_args": [...],  # chromium flags
      "mem_kb": int|None,
      "force_gpu": bool, "force_low_ram": bool,
    }

    `gpu`/`low_ram` are optional manual overrides from CLI flags; when None
    the box is probed.
    """
    force_gpu = bool(gpu)
    force_low = bool(low_ram)
    gpu_on = detect_nv_gpu(force=force_gpu)
    igpu_on = detect_igpu()
    low_on = detect_low_ram(force=force_low)
    # tier reflects the *effective* memory posture: forced low-ram always
    # lands on Low even if /proc reports gigabytes.
    if low_on:
        t = TIER_LOW
    else:
        t = from_mem_kb(mem_total_kb() or 8 * 1_048_576)
    return {
        "gpu": gpu_on,
        "igpu": igpu_on,
        "low_ram": low_on,
        "tier": t,
        "browser_args": browser_args(gpu_on, low_on, igpu_on),
        "mem_kb": mem_total_kb(),
        "force_gpu": force_gpu,
        "force_low_ram": force_low,
    }


def launch_browser(profile: dict | None = None, **kwargs):
    """Build a GhostWire pre-fed with the resolved browser_args.

    Pass a dict from resolve() (or let it re-resolve) plus any GhostWire
    kwargs (headless, profile, engine, ..).  Returns an un-entered
    GhostWire instance — caller enters/`with`s it.
    """
    from ghostrise.wire import GhostWire

    prof = profile or resolve(**kwargs)
    args = list(prof["browser_args"])
    return GhostWire(extra_args=args)


def main():
    """Standalone: print the resolved profile."""
    import json
    import sys

    force_gpu = "--gpu" in sys.argv
    force_low = "--low-ram" in sys.argv
    prof = resolve(gpu=force_gpu or None, low_ram=force_low or None)
    print(
        "ghostrise.runtime: "
        + json.dumps(prof, sort_keys=True)
    )


if __name__ == "__main__":
    main()
