"""Solver vision — keyless captcha-grid classification stack.

Ek chhota (~5-8M param) CNN jo reCAPTCHA v2 / hCaptcha grid tiles ko
multi-label classify karta hai — keyless, local, CPU-realistic.

Components:
  model.py   TileNet (multi-label grid classifier) + RotNet (Arkose
             rotation-angle regression head)
  harvest.py demo pages se tiles harvest -> weak-labels (CLIP/Qwen-VL
             distill se) -> dataset builder
  train.py   training loop (BCE multi-label + angle MSE), ONNX +
             int8 export
  serve.py   FastAPI /classify (batch tiles) — CPU/GPU serving

Param budget: ~5-8M @ 96x96 input. CPU int8: ~ms/tile. GPU batch:
~50k tiles/sec per card -> 5k+ solves/sec -> 100k+ users on a few cards.

Usage:
    python -m solver.vision.train --synthetic     # smoke: pipeline proof
    python -m solver.vision.harvest --demo recaptcha-v2 --out data/tiles
    python -m solver.vision.train --data data/tiles --epochs 20
    python -m solver.vision.serve --port 8030
"""
