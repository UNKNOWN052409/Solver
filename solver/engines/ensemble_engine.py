"""Ensemble engine: multi-variant tesseract + weighted character voting.

The 4th local OCR engine. Single-pass OCR is brittle — one preprocessing
choice (plain binarize vs red-channel isolate, psm 7 vs 13) wins on one
captcha family and fails on the next. Live-verified on the synthetic
battery: red-isolated dominates colored-noise captchas, plain-prep wins
on gray-glyph ones, each far below usable solo accuracy. Running every
variant and voting character-wise recovers far more than any single pass.

Variants (all tesseract-backed sub-passes):
  plain psm7    — Preprocessor binary, single-line mode
  plain psm13  — same image, raw-line mode (different noise handling)
  red psm7     — red-channel isolation (kills non-red noise)
  red psm13   — red-isolated, raw-line
"""

from collections import Counter

import cv2
import numpy as np

from .base import BaseEngine


class EnsembleEngine(BaseEngine):
    name = "ensemble"
    wants_binary = False  # consumes the RAW image; variants prep internally

    #: variant weight by (prep, psm). red psm7 is the colored-captcha
    #: specialist; plain psm7 the general workhorse. psm13 runs are
    #: backups with lower weight (raw-line mode hallucinates more).
    VARIANT_WEIGHTS = {
        ("plain", 7): 1.0,
        ("plain", 13): 0.7,
        ("red", 7): 1.2,
        ("red", 13): 0.8,
    }

    CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"

    #: run red-isolate variants only when this fraction of pixels is
    #: red-dominant (R - max(G,B) > 40). Live-measured: red-glyph captchas
    #: sit at ~0.10, gray-glyph ones at 0.000 — a hard, clean separation.
    #: Below it the red channel carries no signal and its OCR reads pure
    #: channel junk ("oe"), which positional voting would then amplify.
    RED_SIGNAL_MIN_FRAC = 0.005

    def __init__(self, charset: str | None = None, weights: dict | None = None):
        self.charset = charset or self.CHARSET
        self.weights = weights or dict(self.VARIANT_WEIGHTS)

    # ------------------------------------------------------------ prep

    @staticmethod
    def _red_isolate(img_bgr: np.ndarray) -> np.ndarray:
        """Keep only red-dominant strokes (R - max(G,B)), binarize, and
        normalize to black glyphs on white background.

        Defeats colored-noise captchas (blue/green/gray junk): the noise
        carries no red so it vanishes before OCR ever sees it.
        """
        if img_bgr.ndim == 2:
            img_bgr = cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2BGR)
        f = img_bgr.astype(np.int16)
        redness = f[:, :, 2] - np.maximum(f[:, :, 0], f[:, :, 1])  # BGR: ch2=R
        redness = np.clip(redness, 0, 255).astype(np.uint8)
        _, binary = cv2.threshold(redness, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if (binary == 0).mean() > 0.5:  # bg came out black -> flip
            binary = cv2.bitwise_not(binary)
        return binary

    def _variants(self, image: np.ndarray):
        """Yield ((prep, psm), prepared_image, engine) for every pass.

        Red-isolate passes are gated on measured redness: without real
        red content the channel reads as junk and only pollutes the vote
        (skipping them also halves latency on non-red captchas).
        """
        from ..preprocessor import Preprocessor
        from .tesseract_engine import TesseractEngine

        plain = Preprocessor(scale=2.0).run(image)
        passes = [("plain", plain)]
        if self._red_signal(image) >= self.RED_SIGNAL_MIN_FRAC:
            red = self._red_isolate(image)
            red = cv2.resize(red, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
            passes.append(("red", red))
        for prep_label, img in passes:
            for psm in (7, 13):
                yield (prep_label, psm), img, TesseractEngine(
                    charset=self.charset, psm=psm, oem=1)

    @staticmethod
    def _red_signal(img_bgr: np.ndarray) -> float:
        """Fraction of pixels that are clearly red-dominant."""
        if img_bgr.ndim == 2:
            return 0.0
        f = img_bgr.astype(np.int16)
        redness = f[:, :, 2] - np.maximum(f[:, :, 0], f[:, :, 1])  # BGR: ch2=R
        return float((redness > 40).mean())

    # ------------------------------------------------------------ solve

    def solve(self, image: np.ndarray) -> str:
        raws: dict[tuple, str] = {}
        for label, img, eng in self._variants(image):
            try:
                raws[label] = eng.solve(img)
            except Exception:
                raws[label] = ""  # a variant failing never kills the vote
        self.last_votes = raws  # introspection for tests/debug
        return self.vote(raws)

    def vote(self, raws: dict) -> str:
        """Positional weighted voting over variant strings.

        Variant strings can differ in length (missed glyphs); each vote is
        anchored by index in its own string, and the winner is the
        most-voted character per position up to the majority length.

        Junk reads are dropped first: a variant that hallucinated off a
        near-empty channel (e.g. red-isolate on a gray-glyph captcha)
        returns 1-2 stray letters — far below the majority length — and
        would otherwise outvote the sane reads positionally.
        """
        lens = Counter(len(s) for s in raws.values() if s)
        if not lens:
            return ""
        n = lens.most_common(1)[0][0]
        # Junk-read suppression. Red-isolate hallucinates 1-2 stray letters
        # ("oe") on gray-glyph captchas — its channel is near-empty there.
        # Those reads only participate when the plain variants (the ones
        # with a real signal) are all blank, or when the red read is long
        # enough to be a genuine read of the same content.
        plain_reads = [s for (p, _), s in raws.items() if p == "plain" and s]
        red_reads = [s for (p, _), s in raws.items() if p == "red" and s]
        if plain_reads and red_reads and n >= 3:
            red_reads = [s for s in red_reads if len(s) >= n - 1]
            if not red_reads:  # every red read was channel junk
                raws = {k: s for k, s in raws.items() if k[0] != "red"}
                lens = Counter(len(s) for s in raws.values() if s)
                n = lens.most_common(1)[0][0] if lens else 0
        if not lens or n == 0:
            return ""
        out = []
        for pos in range(n):
            tally: Counter = Counter()
            for (prep, psm), s in raws.items():
                if pos < len(s) and s[pos]:
                    tally[s[pos]] += self.weights.get((prep, psm), 1.0)
            if tally:
                out.append(tally.most_common(1)[0][0])
        return "".join(out)

    # ------------------------------------------------------------ health

    @staticmethod
    def available() -> bool:
        from .tesseract_engine import TesseractEngine

        return TesseractEngine().available()

    def solve_with_detail(self, image: np.ndarray) -> dict:
        """Solve + return every variant's raw read (API debug field)."""
        text = self.solve(image)
        return {"text": text, "variants": self.last_votes}
