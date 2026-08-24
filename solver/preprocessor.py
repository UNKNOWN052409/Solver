"""Image cleanup pipeline: turn noisy captcha images into OCR-ready binaries."""

from pathlib import Path

import cv2
import numpy as np


class Preprocessor:
    """Configurable cleanup pipeline for captcha images.

    Pipeline order: upscale -> grayscale -> blur -> threshold ->
    morphological cleanup -> optional line removal.
    """

    def __init__(
        self,
        scale: float = 2.0,
        threshold: str = "otsu",  # "otsu" | "adaptive" | "fixed"
        fixed_value: int = 128,
        denoise: bool = True,
        morph_open: bool = True,
        remove_lines: bool = False,
        invert: bool = True,  # ensure text is black-on-white at the end
    ):
        self.scale = scale
        self.threshold = threshold
        self.fixed_value = fixed_value
        self.denoise = denoise
        self.morph_open = morph_open
        self.remove_lines = remove_lines
        self.invert = invert

    def _load(self, image) -> np.ndarray:
        if isinstance(image, np.ndarray):
            img = image
        else:
            img = cv2.imread(str(image))
            if img is None:
                raise ValueError(f"Could not read image: {image}")
        return img

    def run(self, image) -> np.ndarray:
        """Return a cleaned single-channel uint8 image (text=black, bg=white)."""
        img = self._load(image)

        # Upscaling helps OCR engines resolve thin, distorted glyphs.
        if self.scale != 1.0:
            img = cv2.resize(
                img, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_CUBIC
            )

        if img.ndim == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img.copy()

        if self.denoise:
            gray = cv2.medianBlur(gray, 3)

        if self.threshold == "adaptive":
            binimg = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 31, 10,
            )
        elif self.threshold == "otsu":
            _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            _, binimg = cv2.threshold(gray, self.fixed_value, 255, cv2.THRESH_BINARY)

        if self.morph_open:
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
            binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, kernel, iterations=1)

        if self.remove_lines:
            binimg = self._strip_lines(binimg)

        if self.invert:
            black_ratio = float((binimg == 0).mean())
            if black_ratio > 0.5:  # background came out black; flip it
                binimg = cv2.bitwise_not(binimg)

        return binimg

    @staticmethod
    def _strip_lines(binimg: np.ndarray) -> np.ndarray:
        """Subtract long horizontal/vertical structures (grid-line captchas)."""
        h, w = binimg.shape
        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 15, 8), 1))
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h // 15, 8)))
        horizontal = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, hk)
        vertical = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, vk)
        lines = cv2.bitwise_or(horizontal, vertical)
        return cv2.bitwise_and(binimg, cv2.bitwise_not(lines))

    def save_debug(self, stages: dict, outdir="./debug"):
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        for name, img in stages.items():
            cv2.imwrite(str(out / f"{name}.png"), img)
