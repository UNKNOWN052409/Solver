"""Vision serving — FastAPI /classify, CPU/GPU, batch-ready.

    python -m solver.vision.serve --port 8030
    curl -X POST :8030/classify -d '{"tiles": ["<b64png>", ...]}'
         -> {"labels": [["crosswalk", ...], ...], "ms": 12}

Serving math (int8 ONNX):
  CPU core: ~100-300 tiles/sec -> 9-tile grid ~30-50ms
  GPU batch(64): ~50k tiles/sec/card -> 5k+ solves/sec -> few cards
  serve 100k+ users (solves are bursty; queue+autoscale handles it).
"""
import argparse
import base64
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from model import TileNet, RotNet, CLASSES


def _load():
    """ONNX (fast) warna numpy-ref TileNet. Returns (predict_fn, backend)."""
    onnx_path = "data/pt/tilenet.onnx"
    if os.path.exists(onnx_path):
        try:
            import onnxruntime as ort
            sess = ort.InferenceSession(onnx_path,
                                        providers=["CPUExecutionProvider"])
            def predict(batch):
                x = np.ascontiguousarray(batch, dtype=np.float32)
                return sess.run(None, {"tile": x})[0]
            return predict, "onnx"
        except ImportError:
            pass
    net = TileNet()
    def predict(batch):
        return net.forward(batch)
    return predict, "numpy-ref"


def tile_from_b64(b64png):
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(b64png))).convert("RGB")
    img = img.resize((96, 96))
    return (np.asarray(img, dtype=np.float32) / 255.0).transpose(2, 0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8030)
    args = ap.parse_args()

    predict, backend = _load()
    print(f"[vision-serve] backend={backend} port={args.port}")

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    import uvicorn

    app = FastAPI(title="solver-vision")

    @app.post("/classify")
    async def classify(request: Request):
        t0 = time.time()
        body = json.loads(await request.body())
        tiles = body.get("tiles", [])
        if not tiles:
            return JSONResponse({"error": "tiles[] required"}, status_code=400)
        X = np.stack([tile_from_b64(t) for t in tiles[:64]])
        logits = predict(X)
        sig = 1 / (1 + np.exp(-logits))
        out = []
        for row in sig:
            idx = np.where(row > 0.5)[0]
            out.append([CLASSES[i] for i in idx] or
                       [CLASSES[int(np.argmax(row))]])
        return {"labels": out,
                "scores": [[float(v) for v in row] for row in sig],
                "ms": round((time.time() - t0) * 1000, 1)}

    @app.post("/rotate")
    async def rotate(request: Request):
        """Arkose-style: 'kitna ghumana hai' -> angle deg."""
        body = json.loads(await request.body())
        t = body.get("tile")
        if not t:
            return JSONResponse({"error": "tile required"}, status_code=400)
        X = tile_from_b64(t)[None]
        net = RotNet()
        return {"angle": net.angle(X)}

    @app.get("/health")
    def health():
        return {"ok": True, "backend": backend, "classes": len(CLASSES)}

    uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
