"""Tesseract OCR backend. Requires the tesseract-ocr system binary."""

import shutil
import subprocess
import tempfile

import numpy as np
import cv2

from .base import BaseEngine


class TesseractEngine(BaseEngine):
    name = "tesseract"

    def __init__(self, charset: str = "0123456789abcdefghijklmnopqrstuvwxyz", psm: int = 7, oem: int = 1):
        """psm 7 = treat image as a single text line; 13 = raw line
        (often best for captchas).

        oem 1 (legacy engine) honors tessedit_char_whitelist. oem 3 (LSTM,
        the usual distro default) silently ignores the whitelist on many
        builds and leaks out-of-charset predictions — restricted-charset
        captchas must run with oem 1.
        """
        self.charset = charset
        self.psm = psm
        self.oem = oem

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def solve(self, image: np.ndarray) -> str:
        if not self.available():
            raise RuntimeError(
                "tesseract binary not found. Install it: "
                "`sudo apt-get install tesseract-ocr` (Debian/Kali/Ubuntu)"
            )
        with tempfile.NamedTemporaryFile(suffix=".png") as tmp:
            cv2.imwrite(tmp.name, image)
            cmd = [
                "tesseract", tmp.name, "stdout",
                "--oem", str(self.oem),
                "--psm", str(self.psm),
                "-c", f"tessedit_char_whitelist={self.charset}",
            ]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout.strip().replace(" ", "").replace("\n", "")
