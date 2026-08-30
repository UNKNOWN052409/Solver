"""Audio-captcha engine: speech-to-text via Vosk (offline) or Whisper (API).

Audio captchas ("download audio, type what you hear") are the accessibility
path most sites still expose. This engine transcribes and cleans the result
into the expected charset. Works fully offline with vosk-model-small-en.

    AudioEngine(model_path="vosk-model-small-en-us-0.15").solve(wav_bytes)
"""

import re
import subprocess
import tempfile
from pathlib import Path


class AudioEngine:
    name = "audio"
    wants_binary = False

    # command word lists vary by site; keep the common ones
    _WORDS = (
        "zero one two three four five six seven eight nine "
        "ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
        "eighteen nineteen twenty thirty forty fifty sixty seventy "
        "eighty ninety"
    ).split()

    def __init__(self, model_path: str = "vosk-model-small-en-us-0.15",
                 charset: str = "0123456789", ffmpeg: str = "ffmpeg"):
        self.model_path = Path(model_path)
        self.charset = charset
        self.ffmpeg = ffmpeg

    def available(self) -> bool:
        try:
            from vosk import Model as _Model  # availability probe
            assert _Model is not None
            return self.model_path.exists()
        except ImportError:
            return False

    def _to_wav16k(self, audio_bytes: bytes, src_ext: str = "") -> str:
        """Normalize any input to 16kHz mono WAV via ffmpeg (Vosk's diet)."""
        with tempfile.NamedTemporaryFile(suffix=src_ext or ".bin", delete=False) as f:
            f.write(audio_bytes)
            src = f.name
        out = src.rsplit(".", 1)[0] + ".wav"
        subprocess.run(
            [self.ffmpeg, "-y", "-i", src, "-ar", "16000", "-ac", "1", out],
            capture_output=True, timeout=60,
        )
        return out

    def solve(self, audio_bytes: bytes, src_ext: str = "") -> str:
        from vosk import KaldiRecognizer
        model = self._load_model()
        wav = self._to_wav16k(audio_bytes, src_ext)
        try:
            with open(wav, "rb") as f:
                rec = KaldiRecognizer(model, 16000)
                rec.SetWords(True)
                chunk = 4096
                while True:
                    data = f.read(chunk)
                    if not data:
                        break
                    rec.AcceptWaveform(data)
                result = rec.FinalResult()
        finally:
            Path(wav).unlink(missing_ok=True)
        text = result.get("text", "")
        return self._clean(text)

    def _load_model(self):
        import vosk  # noqa

        if getattr(self, "_model", None) is None:
            if not self.model_path.exists():
                raise FileNotFoundError(
                    f"vosk model not found at {self.model_path} — "
                    "download: https://alphacephei.com/vosk/models"
                )
            self._model = vosk.Model(str(self.model_path))
        return self._model

    def _clean(self, transcript: str) -> str:
        """Map a raw transcript into the expected charset.

        Two captcha dialects exist:
          - digit strings: "two five nine one" -> "2591"
          - spelled-out letters: "A as in alpha" or just "ay" -> hard; the
            word list below handles the common NATO/ICAO spellings.
        """
        t = transcript.lower().strip()
        # NATO alphabet mapping (audio captchas often spell letters)
        nato = {
            "alpha": "a", "bravo": "b", "charlie": "c", "delta": "d",
            "echo": "e", "foxtrot": "f", "golf": "g", "hotel": "h",
            "india": "i", "juliet": "j", "kilo": "k", "lima": "l",
            "mike": "m", "november": "n", "oscar": "o", "papa": "p",
            "quebec": "q", "romeo": "r", "sierra": "s", "tango": "t",
            "uniform": "u", "victor": "v", "whiskey": "w", "x-ray": "x",
            "xray": "x", "yankee": "y", "zulu": "z",
        }
        digits = {w: str(i) for i, w in enumerate(self._WORDS)}
        # letters also spelled as words ("ay", "bee", "see"...)
        letters = {
            "ay": "a", "a": "a", "bee": "b", "b": "b", "see": "c", "sea": "c",
            "c": "c", "dee": "d", "d": "d", "e": "e", "ee": "e", "eff": "f",
            "f": "f", "gee": "g", "g": "g", "jay": "j", "j": "j", "kay": "k",
            "k": "k", "oh": "o", "o": "o", "pee": "p", "p": "p", "cue": "q",
            "q": "q", "are": "r", "r": "r", "ess": "s", "s": "s", "tee": "t",
            "t": "t", "you": "u", "u": "u", "vee": "v", "v": "v",
            "double-u": "w", "why": "y", "ex": "x", "zed": "z", "zee": "z", "z": "z",
        }
        table = {**nato, **digits, **letters}

        # first pass: whole-word matches
        words = re.split(r"[\s,.;!?-]+", t)
        mapped = []
        for w in words:
            w = w.strip("'\"")
            if not w:
                continue
            if w in table:
                mapped.append(table[w])
            elif w.isdigit() and w in self.charset:
                mapped.append(w)
            elif len(w) == 1 and w in self.charset.lower():
                mapped.append(w.upper() if self.charset.isupper() else w)
        return "".join(mapped)

    # -- convenience: also usable as a words-only checker (some sites ask to
    # type the Nth word, etc.) --
    def words(self, audio_bytes: bytes, src_ext: str = "") -> str:
        """Return the raw cleaned word sequence, for 'type the 3rd word' prompts."""
        from vosk import KaldiRecognizer
        model = self._load_model()
        wav = self._to_wav16k(audio_bytes, src_ext)
        try:
            with open(wav, "rb") as f:
                coord = KaldiRecognizer(model, 16000)
                chunk = 4096
                while True:
                    data = f.read(chunk)
                    if not data:
                        break
                    coord.AcceptWaveform(data)
                result = coord.FinalResult()
        finally:
            Path(wav).unlink(missing_ok=True)
        return " ".join(result.get("result", [])).strip()
