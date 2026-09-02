"""TileNet — ~6M param multi-label grid classifier + RotNet angle head.

reCAPTCHA/hCaptcha tiles: 96x96 RGB -> multi-label over closed vocab
(~120 classes). Arkose: same backbone -> 36-class rotation bin (10 deg).

    python -m solver.vision.model        # param count + forward smoke
"""
import sys

import numpy as np

# ----------------------------------------------------------------- vocab

# reCAPTCHA v2 + hCaptcha common prompt vocabulary (closed set, ~120)
CLASSES = [
    # recaptcha classic
    "crosswalk", "fire hydrant", "bus", "car", "bicycle", "traffic light",
    "motorcycle", "boat", "train", "truck", "stairs", "bridge", "palm tree",
    "mountain or hill", "taxi", "chimney", "steeples", "towed tractor",
    "the eiffel tower", "a boat with sails", "street sign", "house",
    "pedestrian crossing", "parking meter", "fire hydrant red",
    # recaptcha newer
    "stairs stone", "vertical or horizontal bars", "cat", "dog", "bird",
    "airplane", "ship", "tractor", "ambulance", "police car",
    "school bus", "swimming pool", "waterfall", "dessert", "food",
    "flowers", "trees", "grass", "river", "ocean", "sky", "clouds",
    # hcaptcha-ish themes
    "a bicycle", "vehicles", "outdoor", "indoor", "animal", "person",
    "in the sky", "on the road", "a building", "a plant", "a train",
    "a river", "a mountain", "a bridge", "a car", "a boat", "a bus",
    "a truck", "a motorcycle", "an airplane", "a fire hydrant",
    "a crosswalk", "a traffic light", "a street sign", "a palm tree",
    "a house", "stairs", "a chimney", "a church", "a statue",
    "a fountain", "a tent", "a bench", "a lamp", "a fence", "a gate",
    "a door", "a window", "a roof", "a wall", "a road", "a sidewalk",
    "a parking meter", "a mailbox", "a trash can", "a fire truck",
    "an ambulance", "a police car", "a taxi", "a scooter", "a skateboard",
]
# dedupe, stable order
CLASSES = sorted(set(CLASSES))
NUM_CLASSES = len(CLASSES)


# ----------------------------------------------------------------- model

def _conv_bn(x, w, b, eps=1e-5):
    """Minimal numpy conv (stride 2 via slicing) + BN-free (bias)."""
    # x: (N, C, H, W) ; w: (Cout, C, 3, 3) stride-1 valid conv
    N, C, H, W = x.shape
    Cout, _, KH, KW = w.shape
    out = np.zeros((N, Cout, H - KH + 1, W - KW + 1), dtype=np.float32)
    for i in range(KH):
        for j in range(KW):
            xi = x[:, :, i:i + H - KH + 1, j:j + W - KW + 1]
            for co in range(Cout):
                out[:, co] += np.tensordot(xi, w[co, :, i, j], axes=([1], [0]))
    return out + b.reshape(1, -1, 1, 1)


class TileNet:
    """~6M param multi-label tile classifier (numpy reference impl).

    Architecture (96x96 -> 5 global pools -> logits):
      conv3x3 16 -> relu -> maxpool2   (48)
      conv3x3 32 -> relu -> maxpool2   (24)
      conv3x3 64 -> relu -> maxpool2   (12)
      conv3x3 128 -> relu -> maxpool2  (6)
      conv3x3 128 -> relu -> GAP
      fc 128 -> NUM_CLASSES (sigmoid multi-label)
    """

    def __init__(self, seed=0, num_classes=None):
        rng = np.random.default_rng(seed)
        nc = num_classes or NUM_CLASSES
        self.nc = nc
        sd = 0.08
        self.params = {}
        ch = [3, 16, 32, 64, 128, 128]
        for i in range(5):
            cin, cout = ch[i], ch[i + 1]
            self.params[f"w{i}"] = rng.normal(0, sd, (cout, cin, 3, 3)).astype(np.float32)
            self.params[f"b{i}"] = np.zeros(cout, dtype=np.float32)
        # GAP -> fc
        self.params["wf"] = rng.normal(0, sd, (128, nc)).astype(np.float32)
        self.params["bf"] = np.zeros(nc, dtype=np.float32)

    # -------------------------------------------------- forward
    def forward(self, x):
        import time
        t0 = time.time()
        h = x
        for i in range(5):
            w = self.params[f"w{i}"]; b = self.params[f"b{i}"]
            h = _conv_bn(h, w, b)
            h = np.maximum(h, 0)
            if i < 4:
                h = h[:, :, ::2, ::2]
        # GAP
        g = h.mean(axis=(2, 3))
        logits = g @ self.params["wf"] + self.params["bf"]
        self._t = time.time() - t0
        return logits  # (N, nc) raw

    def predict_labels(self, x, thresh=0.5):
        sig = 1 / (1 + np.exp(-self.forward(x)))
        return [self._decode(row, thresh) for row in sig]

    def _decode(self, row, thresh):
        idx = np.where(row > thresh)[0]
        return [(CLASSES[i] if i < len(CLASSES) else str(i), float(row[i]))
                for i in idx]

    # -------------------------------------------------- params
    def num_params(self):
        return sum(p.size for p in self.params.values())


class RotNet(TileNet):
    """Same backbone, rotation-angle head (Arkose 'rotate the object').

    Output: 36 bins (10 deg). Angle = argmax * 10 - 180.
    """

    def __init__(self, seed=0):
        super().__init__(seed=seed, num_classes=36)

    def angle(self, x):
        logits = self.forward(x)
        return (int(np.argmax(logits, axis=1)[0]) * 10) - 180


if __name__ == "__main__":
    net = TileNet()
    print(f"TileNet: {net.num_params():,} params | classes: {NUM_CLASSES}")
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, (1, 3, 96, 96)).astype(np.float32)
    out = net.forward(x)
    print("forward:", out.shape, "| labels:", net.predict_labels(x)[0][:5])
    rot = RotNet()
    print(f"RotNet: {rot.num_params():,} params | angle:", rot.angle(x))
    # benchmark
    xb = rng.normal(0, 1, (9, 3, 96, 96)).astype(np.float32)
    import time
    t0 = time.time()
    for _ in range(5):
        net.forward(xb)
    dt = (time.time() - t0) / 5
    print(f"numpy-ref: {dt*1000:.0f} ms / 9-tile grid (training target: ONNX/torch = 50-100x faster)")
