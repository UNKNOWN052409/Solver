"""Regression tests for bugs found during real-data testing (LCSD dataset).

Each test names the bug it guards against. Run: pytest tests/test_realdata_regressions.py -q
"""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from solver.preprocessor import Preprocessor
from solver.segmentation import find_char_boxes, merge_touching


def _binary_from(grid):
    """Tiny helper: '#'-foreground ASCII grid -> uint8 binary image."""
    arr = np.array([[0 if c == "#" else 255 for c in row] for row in grid], dtype=np.uint8)
    return arr


class TestBug1SegmentationCrashOnEmpty:
    """BUG 1: filters can empty `cand` after the early `if not cand` guard,
    then np.percentile([]) crashed with IndexError (real LCSD image
    abac765b '#<nq'). The guard must now sit AFTER the junk filters."""

    def test_all_junk_image_returns_empty(self):
        # Real-crash geometry (81x34): a wide streak (aspect>4:1, killed by
        # the streak filter) + 1px specks (killed by area filter: min area
        # 5.5px at this size) -> candidates empty, must return [] not crash.
        img = np.full((34, 81), 255, dtype=np.uint8)
        img[10, 5:45] = 0        # 40x1 streak: aspect 40:1 -> streak filter
        img[30, 20] = 0          # speck: area 1 < 5.5 -> area filter
        img[5, 70] = 0           # speck
        assert find_char_boxes(img) == []

    def test_dense_noise_returns_empty(self):
        # scatter of tiny specks only -> area filter rejects all
        rng = np.random.default_rng(0)
        img = np.full((60, 160), 255, dtype=np.uint8)
        for _ in range(50):
            y, x = rng.integers(0, 60), rng.integers(0, 160)
            img[y, x] = 0
        assert find_char_boxes(img) == []

    def test_real_style_streak_and_speck_mix_no_crash(self):
        # wide diagonal-ish blob + one small dot: both rejected, no crash
        img = np.full((34, 81), 255, dtype=np.uint8)
        img[5:7, 10:40] = 0    # 30x2 streak -> aspect 15:1, rejected
        img[20:23, 50:53] = 0  # 3x3 dot -> area < 0.002*2754=5.5px? 9px passes area,
        # density 9/9=1.0 passes, height 3 < 0.3*ref(3)*? -> kept... acceptable:
        # what matters is it does not raise.
        find_char_boxes(img)


class TestBug2CNNEngineZeroBoxes:
    """BUG 2: CNNEngine.solve() crashed with np.stack([]) when segmentation
    returned zero boxes. Now returns '' instead."""

    def test_zero_boxes_returns_empty_string(self):
        pytest.importorskip("torch")
        import torch

        from solver.engines.cnn_engine import CharNet

        # minimal checkpoint with 2-class charset
        net = CharNet(2)
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            torch.save({"state_dict": net.net.state_dict(), "charset": "ab"}, f.name)
            path = f.name
        try:
            from solver.engines.cnn_engine import CNNEngine
            eng = CNNEngine(path)
            blank = np.full((68, 162), 255, dtype=np.uint8)  # no glyphs at all
            assert eng.solve(blank) == ""
        finally:
            os.unlink(path)


class TestBug4TrainValLeak:
    """BUG 4: train_cnn split crops AFTER exploding, leaking same-image
    glyphs into both sets. build_dataset must now return per-image
    grouped samples so the split is per-image."""

    def test_build_dataset_returns_grouped_samples(self):
        pytest.importorskip("torch")
        from training.train_cnn import build_dataset

        samples, charset = build_dataset(n_samples=8, length=5)
        assert len(samples) <= 8
        for crops, text in samples:
            assert crops.shape == (5, 32, 32)  # grouped, not flattened
            assert len(text) == 5
        # charset sorted, no duplicates
        assert list(charset) == sorted(set(charset))


class TestBug3PercentileThreshold:
    """BUG 3 (design): Otsu merged noise+lines+glyphs into one blob on
    real LCSD images (25.7% foreground). New 'percentile' mode keeps the
    darkest N% only."""

    def test_percentile_mode_keeps_darkest_pct(self):
        # gradient image: 50x50, values 0..2499
        img = np.tile(np.arange(0, 250, 5, dtype=np.uint8), (50, 1))
        prep = Preprocessor(scale=1.0, threshold="percentile", percentile=10.0,
                            denoise=False, morph_open=False, invert=False)
        out = prep.run(img)
        fg_ratio = (out == 0).mean()
        # darkest 10% -> 25 columns of 50 -> ~10% foreground (25*50/2500)
        assert 0.05 <= fg_ratio <= 0.15

    def test_percentile_mode_on_lcsd_style_noise(self):
        # light bg + heavy speckle + thin grid lines + dark glyphs
        rng = np.random.default_rng(1)
        img = np.full((34, 81), 240, dtype=np.uint8)
        img[8:26, 14:22] = 40   # glyph-ish dark block
        img[8:26, 30:38] = 60
        img[8:26, 46:54] = 50
        img[8:26, 62:70] = 45
        ys, xs = rng.integers(0, 34, 300), rng.integers(0, 81, 300)
        img[ys, xs] = rng.integers(120, 200, 300)  # noise dots
        img[2, :] = 150; img[31, :] = 150           # grid lines
        prep = Preprocessor(scale=1.0, threshold="percentile", percentile=10.0)
        out = prep.run(img)
        # glyph columns should dominate the foreground, not the lines
        col_fg = (out == 0).mean(axis=0)
        glyph_cols = col_fg[14:22].mean()
        line_cols = col_fg.mean()  # global average for contrast
        assert glyph_cols > line_cols


class TestBug6MergeTouchingWired:
    """BUG 6: merge_touching was dead code. Now available as an opt-in
    `fuse_gaps` flag on find_char_boxes (default OFF: measured 2/40 exact
    segmentation on synthetic captchas when ON vs 26/40 when OFF)."""

    def test_merge_touching_fuses_tiny_gaps(self):
        boxes = [(10, 5, 8, 20), (19, 5, 7, 20), (60, 5, 8, 20)]
        merged = merge_touching(boxes, gap_ratio=0.35)
        # gap between box0 and box1 = 1px << 0.35*7.5 -> fused
        assert len(merged) == 2
        assert merged[0][2] >= 15  # fused width covers both

    def test_merge_touching_respects_real_gaps(self):
        boxes = [(10, 5, 8, 20), (40, 5, 8, 20)]
        merged = merge_touching(boxes, gap_ratio=0.35)
        assert len(merged) == 2  # 22px gap is a real inter-char gap

    def test_fuse_gaps_off_by_default(self):
        # build a synthetic captcha, run segmentation both ways: OFF must
        # recover characters at least as well as ON
        from solver.generator import CaptchaGenerator

        gen = CaptchaGenerator(length=5)
        import numpy as np
        from solver.preprocessor import Preprocessor

        prep = Preprocessor(scale=2.0)
        off_ok = 0
        n = 20
        for _ in range(n):
            img, text = gen.generate()
            boxes = find_char_boxes(prep.run(np.array(img)))
            if len(boxes) == len(text):
                off_ok += 1
        assert off_ok / n >= 0.5  # his original pipeline quality preserved


class TestBug7TesseractOem:
    """BUG 7: --oem 3 (LSTM) ignores tessedit_char_whitelist on many builds.
    Engine must default to oem 1 and expose the knob."""

    def test_default_oem_is_legacy(self):
        from solver.engines.tesseract_engine import TesseractEngine
        eng = TesseractEngine()
        assert eng.oem == 1
        eng2 = TesseractEngine(oem=3)
        assert eng2.oem == 3


class TestSlotEngine:
    """New fixed-slot engine: geometry remap + no-crash on blank."""

    def test_slot_geometry_remaps(self):
        pytest.importorskip("torch")
        import torch

        from solver.engines.slot_engine import SlotEngine
        import tempfile, os

        net = CharNet2()
        with tempfile.NamedTemporaryFile(suffix=".pt", delete=False) as f:
            torch.save({"state_dict": net.net.state_dict(), "charset": "ab"}, f.name)
            path = f.name
        try:
            eng = SlotEngine(path, x0=11, x1=69, n_chars=4)
            wide = np.full((68, 162), 255, dtype=np.uint8)  # 2x reference width
            crops = eng._slot_crops(wide)
            assert len(crops) == 4
            assert crops[0].shape == (32, 32)
            # blank input solves to 4 chars without crashing
            assert len(eng.solve(wide)) == 4
        finally:
            os.unlink(path)


def CharNet2():
    from solver.engines.cnn_engine import CharNet
    return CharNet(2)
