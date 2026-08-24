"""Local CNN backend for custom captcha styles.

Workflow: generate labeled data -> train (training/train_cnn.py) ->
point this engine at the checkpoint. Beats generic OCR by a wide margin
on any specific target's glyph style.
"""

from pathlib import Path

import cv2
import numpy as np

from ..preprocessor import Preprocessor
from ..segmentation import extract_chars, find_char_boxes
from .base import BaseEngine

IMG_SIZE = 32


class CharNet:
    """Small per-character classifier: 32x32 grayscale crops -> charset index."""

    def __init__(self, num_classes: int):
        import torch.nn as nn

        self.net = nn.Sequential(
            nn.Conv2d(1, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),   # 16x16
            nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),  # 8x8
            nn.Conv2d(32, 64, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),  # 4x4
            nn.Flatten(),
            nn.Linear(64 * 4 * 4, 128), nn.ReLU(),
            nn.Dropout(0.25),
            nn.Linear(128, num_classes),
        )


class CNNEngine(BaseEngine):
    name = "cnn"

    def __init__(self, model_path: str, charset: str | None = None):
        import torch

        ckpt = torch.load(model_path, map_location="cpu", weights_only=False)
        self.charset = charset or ckpt["charset"]
        self.torch = torch
        self.model = CharNet(len(self.charset))
        self.model.net.load_state_dict(ckpt["state_dict"])
        self.model.net.eval()

    def _prep_crop(self, crop: np.ndarray) -> "np.ndarray":
        img = cv2.resize(crop, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)
        return img.astype(np.float32) / 255.0

    def solve(self, image: np.ndarray) -> str:
        boxes = find_char_boxes(image)
        crops = extract_chars(image, boxes)
        batch = np.stack([self._prep_crop(c) for c in crops])[:, None, :, :]
        x = self.torch.from_numpy(batch)
        with self.torch.no_grad():
            preds = self.model.net(x).argmax(dim=1).tolist()
        return "".join(self.charset[i] for i in preds)

    @staticmethod
    def available(model_path: str) -> bool:
        try:
            import torch  # noqa: F401
        except ImportError:
            return False
        return Path(model_path).exists()
