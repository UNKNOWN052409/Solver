"""Fixed-slot engine for captchas with stable, known glyph geometry.

Some real targets (e.g. government/portal captchas with heavy speckle and
grid lines) render each character into a predictable x-band. Connected-
component segmentation drowns in the noise there, but slicing the known
slots and classifying each crop works well. Train with
training/train_slot.py, then:

    SlotEngine("model.pt", x0=11, x1=69, n_chars=4)
"""

import cv2
import numpy as np

from .cnn_engine import IMG_SIZE, CNNEngine


class SlotEngine(CNNEngine):
    name = "slot"
    wants_binary = False  # consumes RAW images; its own slot slicer handles prep

    def __init__(self, model_path: str, x0: int = 11, x1: int = 69,
                 n_chars: int = 4, charset: str | None = None):
        super().__init__(model_path, charset)
        self.x0 = x0
        self.x1 = x1
        self.n_chars = n_chars

    def _slot_crops(self, image: np.ndarray):
        """Slice the glyph band into n_chars equal grayscale slots.

        x0/x1 geometry is defined against the 81px-wide reference image;
        if we're handed a different width (preprocessor scaling, retina
        captures), remap the band proportionally.
        """
        gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        scale = w / 81.0
        x0 = int(self.x0 * scale)
        x1 = int(self.x1 * scale)
        x0 = max(0, min(x0, w - self.n_chars))
        x1 = max(x0 + self.n_chars, min(x1, w))
        band_w = (x1 - x0) / self.n_chars
        crops = []
        for i in range(self.n_chars):
            xa = int(x0 + i * band_w)
            xb = int(x0 + (i + 1) * band_w)
            crop = gray[:, max(xa, 0):xb]
            crop = cv2.resize(crop, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)
            crops.append(crop.astype(np.float32) / 255.0)
        return crops

    def solve(self, image: np.ndarray) -> str:
        # Feed RAW grayscale slots (no thresholding): the net learned its own
        # noise rejection during training, binarizing here only hurts it.
        crops = self._slot_crops(image)
        batch = np.stack(crops)[:, None, :, :]
        x = self.torch.from_numpy(batch)
        with self.torch.no_grad():
            preds = self.model.net(x).argmax(dim=1).tolist()
        return "".join(self.charset[i] for i in preds)
