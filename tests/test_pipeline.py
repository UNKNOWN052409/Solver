"""Pipeline tests. Run: pytest -q"""

import numpy as np
import pytest


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    from solver.generator import CaptchaGenerator

    gen = CaptchaGenerator(length=5)
    return [gen.generate() for _ in range(20)]


def _prep(img):
    from solver.preprocessor import Preprocessor

    return Preprocessor(scale=2.0).run(np.array(img))


def test_generator_produces_labeled_images(samples):
    for img, text in samples:
        assert img.size == (170, 60)
        assert len(text) == 5
        assert text.isalnum()


def test_segmentation_recovers_char_count(samples):
    """Most clean synthetic captchas should segment into exactly `length` blobs."""
    from solver.segmentation import find_char_boxes

    good = 0
    for img, text in samples:
        boxes = find_char_boxes(_prep(img))
        if len(boxes) == len(text):
            good += 1
    assert good / len(samples) >= 0.6, f"only {good}/{len(samples)} segmented cleanly"


def test_tesseract_engine_reports_availability():
    import shutil

    from solver.engines.tesseract_engine import TesseractEngine

    eng = TesseractEngine()
    # system binary OR userland tree (no-root installs, /tmp/tessroot)
    if shutil.which("tesseract"):
        assert eng.available() is True
    else:
        # userland fallback: available() iff the tree is usable
        assert eng.available() == (eng.binary_source() == "userland")


def test_cli_module_imports():
    from solver import cli  # import side-effect check: argparse builds OK

    assert hasattr(cli, "main")
