"""Synthetic captcha generator.

Produces labeled distorted-text captchas (Pillow + OpenCV) so you can
build training data matched to any target style: pick length, charset,
size, noise level. This is what makes custom CNN training possible.
"""

from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DIGITS = "0123456789"
LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class CaptchaGenerator:
    def __init__(
        self,
        length: int = 5,
        charset: str = DIGITS + LOWER,
        size=(170, 60),
        font_path: str = DEFAULT_FONT,
        rotation_range: int = 22,
        noise_dots: int = 220,
        lines: int = 3,
        wave_amplitude: int = 4,
    ):
        self.length = length
        self.charset = charset
        self.size = size
        self.rotation_range = rotation_range
        self.noise_dots = noise_dots
        self.lines = lines
        self.wave_amplitude = wave_amplitude

        try:
            self.font = ImageFont.truetype(font_path, size=int(size[1] * 0.62))
        except OSError:
            self.font = ImageFont.load_default()

    def _random_text(self) -> str:
        rng = np.random.default_rng()
        return "".join(rng.choice(list(self.charset)) for _ in range(self.length))

    def generate(self, text: str | None = None):
        """Returns (PIL.Image RGB, text)."""
        text = text or self._random_text()
        w, h = self.size
        img = Image.new("RGB", (w, h), (250, 250, 248))
        draw = ImageDraw.Draw(img)

        # Per-character tiles with rotation and vertical jitter.
        slot = w // (self.length + 1)
        for i, ch in enumerate(text):
            angle = np.random.uniform(-self.rotation_range, self.rotation_range)
            tile_size = int(h * 1.2)
            tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
            tdraw = ImageDraw.Draw(tile)
            shade = np.random.randint(20, 110)
            tdraw.text(
                (tile_size // 2, tile_size // 2), ch,
                font=self.font, fill=(shade, shade, shade, 255), anchor="mm",
            )
            tile = tile.rotate(angle, resample=Image.BICUBIC, expand=False)
            cx = int(slot * (i + 0.5) + np.random.uniform(-6, 6)) + slot // 2 - slot // 2
            cy = int(h / 2 + np.random.uniform(-5, 5))
            img.paste(tile, (cx - tile_size // 2, cy - tile_size // 2), tile)

        # Speckle noise.
        arr = np.array(img)
        ys = np.random.randint(0, h, self.noise_dots)
        xs = np.random.randint(0, w, self.noise_dots)
        vals = np.random.randint(40, 160, self.noise_dots)
        arr[ys, xs] = vals[..., None] if arr.ndim == 3 else vals
        img = Image.fromarray(arr)
        draw = ImageDraw.Draw(img)

        # Stray arcs / lines crossing the text.
        for _ in range(self.lines):
            x0, y0 = np.random.randint(0, w), np.random.randint(0, h)
            x1, y1 = np.random.randint(0, w), np.random.randint(0, h)
            g = np.random.randint(120, 200)
            draw.line([(x0, y0), (x1, y1)], fill=(g, g, g), width=np.random.randint(1, 3))

        # Sinusoidal wave warp.
        if self.wave_amplitude > 0:
            arr = np.array(img)
            hh, ww = arr.shape[:2]
            map_x, map_y = np.meshgrid(
                np.arange(ww, dtype=np.float32), np.arange(hh, dtype=np.float32)
            )
            map_y += (
                self.wave_amplitude
                * np.sin(map_x / ww * 2 * np.pi * np.random.uniform(1.0, 2.5))
            ).astype(np.float32)
            np.clip(map_y, 0, hh - 1, out=map_y)  # avoid smeared edge bands
            arr = cv2.remap(arr, map_x, map_y, interpolation=cv2.INTER_LINEAR)
            img = Image.fromarray(arr)

        return img, text

    def save_batch(self, outdir, n: int, prefix="sample"):
        """Generate n labeled samples -> outdir/<prefix>_<text>.png"""
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for _ in range(n):
            img, text = self.generate()
            p = out / f"{prefix}_{text}.png"
            img.save(p)
            paths.append(p)
        return paths
