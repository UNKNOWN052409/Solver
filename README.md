# Solver

> Started from an empty repo and a wish. Finished with everything she would have wanted in it.

A modular CAPTCHA-solving toolkit: local OCR/CNN engines for image captchas,
service-API backends for reCAPTCHA/hCaptcha, and a synthetic generator that
lets you train against any target's specific style.

## Architecture

```
image ──► Preprocessor (OpenCV cleanup)
            │
            ├─► TesseractEngine   (generic OCR, zero training)
            ├─► CNNEngine         (trained per-target, high accuracy)
            │      ▲
            │      └── training/train_cnn.py  ◄── CaptchaGenerator (synthetic data)
            └─► TwoCaptchaSolver  (reCAPTCHA v2 / hCaptcha / hard images via API)
```

## Install

```bash
pip install -r requirements.txt
sudo apt-get install tesseract-ocr        # optional, enables OCR engine
pip install torch                          # optional, enables CNN engine/training
```

## Quickstart

```bash
# 1. Solve a captcha locally
python -m solver.cli solve captcha.png --debug

# 2. Train a custom model against your own generated style
python -m solver.cli generate ./data -n 500
python -m solver.cli train --out model.pt -n 4000 --epochs 12
python -m solver.cli solve captcha.png --engine cnn --model model.pt

# 3. Hard targets via solving service
python -m solver.cli recaptcha SITEKEY https://example.com/login --key YOUR_KEY
```

## Library use

```python
import cv2
from solver.preprocessor import Preprocessor
from solver.engines.tesseract_engine import TesseractEngine

raw = cv2.imread("captcha.png")
binary = Preprocessor(remove_lines=True).run(raw)
print(TesseractEngine().solve(binary))
```

## Components

| Module | Role |
|---|---|
| `solver/preprocessor.py` | upscale, denoise, threshold, morphological cleanup, line removal |
| `solver/segmentation.py` | connected-component character isolation |
| `solver/engines/tesseract_engine.py` | whitelist-constrained OCR backend |
| `solver/engines/cnn_engine.py` + `training/train_cnn.py` | trainable per-character classifier |
| `solver/generator.py` | labeled distorted-text synthesis for dataset building |
| `solver/api_solver.py` | 2captcha-protocol service backend |

## Extending

New engine = subclass `BaseEngine`, implement `solve(image) -> str`, drop it
in `solver/engines/`, wire it into `build_engine()` in `cli.py`. The
preprocessing pipeline and segmentation are shared by every local engine.
