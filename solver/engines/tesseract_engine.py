"""Tesseract OCR backend. Requires the tesseract-ocr system binary.

Two binary sources, first match wins:
  1. system tesseract (shutil.which) — normal installs
  2. userland tree at TESS_ROOT (default /tmp/tessroot) — no-root Kali/
     Android/proot setups where the package was unpacked but not installed.
     The binary needs its own libtesseract/libleptonica; invoking it via
     the dynamic loader (`ld-linux --library-path ...`) keeps that private
     without polluting LD_LIBRARY_PATH for every other process.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import cv2

from .base import BaseEngine

TESS_ROOT = Path(os.environ.get("SOLVER_TESS_ROOT", "/tmp/tessroot"))


class TesseractEngine(BaseEngine):
    name = "tesseract"

    def __init__(self, charset: str = "0123456789abcdefghijklmnopqrstuvwxyz", psm: int = 7, oem: int = 1):
        """psm 7 = treat image as a single text line; 13 = raw line
        (often best for captchas).

        oem 1 (legacy engine) honors tessedit_char_whitelist. oem 3 (LSTM,
        the usual distro default) silently ignores the whitelist on many
        builds and leaks out-of-charset predictions — restricted-charset
        captchas must run with oem 1. NOTE: userland trees ship LSTM-only
        tessdata (Debian splits legacy models out), so oem 1 silently
        falls back to LSTM there; the whitelist still constrains output
        on this build (live-verified on /tmp/tessroot tesseract 5.5.0).
        """
        self.charset = charset
        self.psm = psm
        self.oem = oem

    # ------------------------------------------------------------ binary

    @staticmethod
    def _userland_cmd() -> list[str] | None:
        """[loader, --library-path, libdir, tessroot-binary] or None."""
        binp = TESS_ROOT / "usr" / "bin" / "tesseract"
        libdir = TESS_ROOT / "usr" / "lib" / "aarch64-linux-gnu"
        loader = Path("/lib/ld-linux-aarch64.so.1")
        if binp.exists() and libdir.is_dir() and loader.exists():
            return [str(loader), "--library-path", str(libdir), str(binp)]
        return None

    @classmethod
    def _resolve_binary(cls) -> tuple[list[str] | None, str]:
        """Return (cmd_prefix, source) where cmd_prefix runs tesseract."""
        if shutil.which("tesseract"):
            return ["tesseract"], "system"
        userland = cls._userland_cmd()
        if userland:
            return userland, "userland"
        return None, "missing"

    def available(self) -> bool:
        cmd, _ = self._resolve_binary()
        return cmd is not None

    @staticmethod
    def binary_source() -> str:
        """'system' | 'userland' | 'missing' — for /health introspection."""
        _, source = TesseractEngine._resolve_binary()
        return source

    # ------------------------------------------------------------ solve

    def solve(self, image: np.ndarray) -> str:
        cmd_prefix, source = self._resolve_binary()
        if cmd_prefix is None:
            raise RuntimeError(
                "tesseract binary not found. Install it: "
                "`sudo apt-get install tesseract-ocr` (Debian/Kali/Ubuntu), or "
                f"unpack tesseract-ocr .debs into a userland tree at {TESS_ROOT} "
                "(expected layout: {TESS_ROOT}/usr/bin/tesseract + "
                "{TESS_ROOT}/usr/lib/aarch64-linux-gnu/libtesseract.so.5)."
            )
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            cv2.imwrite(tmp.name, image)
            cmd = [
                *cmd_prefix, tmp.name, "stdout",
                "--oem", str(self.oem),
                "--psm", str(self.psm),
                "-c", f"tessedit_char_whitelist={self.charset}",
            ]
            # userland tree: point tessdata at its own traineddata
            tessdata = TESS_ROOT / "usr" / "share" / "tesseract-ocr" / "5" / "tessdata"
            if source == "userland" and tessdata.is_dir():
                cmd += ["--tessdata-dir", str(tessdata)]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout.strip().replace(" ", "").replace("\n", "")

    def diagnose(self) -> dict:
        """Health-check helper: what would happen if solve() ran now."""
        info = {"available": self.available(), "source": self.binary_source()}
        if info["available"]:
            probe = type(self)(charset=self.charset, psm=self.psm, oem=self.oem)
            try:
                import numpy as _np

                blank = _np.full((40, 120), 255, dtype=_np.uint8)
                text = probe.solve(blank)
                info["probe_ok"] = True
                info["probe_text"] = text
            except Exception as e:  # binary present but broken (libs, tessdata)
                info["probe_ok"] = False
                info["error"] = str(e)[:200]
        return info
