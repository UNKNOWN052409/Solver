"""Audio-captcha engine: speech-to-text via Vosk (offline) or Whisper (API).

Audio captchas ("download audio, type what you hear") are the accessibility
path most sites still expose. This engine transcribes and cleans the result
into the expected charset. Works fully offline with vosk-model-small-en.

    AudioEngine(model_path="vosk-model-small-en-us-0.15").solve(wav_bytes)
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np


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
        """Normalize any input to 16kHz mono WAV.

        Fast path: input is already a WAV (most audio captchas) — parse the
        header with stdlib `wave` and convert inline; no external binary.
        Slow path: mp3/ogg/etc need a decoder, fall back to ffmpeg if it
        exists on the system. Raises a clear error naming the missing piece.
        """
        out = str(Path(tempfile.gettempdir()) / ("netkit_audio_" + os.urandom(6).hex() + ".wav"))
        is_probably_wav = audio_bytes[:4] == b"RIFF" and audio_bytes[8:12] == b"WAVE"
        if is_probably_wav:
            self._convert_wav_bytes(audio_bytes, out)
            return out
        if shutil.which(self.ffmpeg):
            with tempfile.NamedTemporaryFile(suffix=src_ext or ".bin", delete=False) as f:
                f.write(audio_bytes)
                src = f.name
            subprocess.run(
                [self.ffmpeg, "-y", "-i", src, "-ar", "16000", "-ac", "1", out],
                capture_output=True, timeout=60,
            )
            Path(src).unlink(missing_ok=True)
            return out
        raise RuntimeError(
            f"audio engine needs ffmpeg for {src_ext or 'this'} audio (not a WAV, "
            "and ffmpeg is not installed); install ffmpeg or send WAV bytes"
        )

    @staticmethod
    def _convert_wav_bytes(data: bytes, out_path: str) -> None:
        """Pure-stdlib WAV -> 16kHz mono 16-bit via linear resample."""
        import wave

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            src = f.name
        try:
            with wave.open(src, "rb") as w:
                nch, sw, sr, nframes = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
                raw = w.readframes(nframes)
        finally:
            Path(src).unlink(missing_ok=True)

        # to 16-bit mono
        if sw == 2:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
        elif sw == 1:  # 8-bit unsigned
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) * 256
        elif sw == 4:  # 32-bit int
            samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 65536
        else:
            raise ValueError(f"unsupported sample width {sw}")
        if nch > 1:
            samples = samples.reshape(-1, nch).mean(axis=1)

        # resample sr -> 16000 (linear interpolation is fine for speech)
        if sr != 16000:
            n_out = int(len(samples) * 16000 / sr)
            x_old = np.linspace(0, 1, len(samples), endpoint=False)
            x_new = np.linspace(0, 1, n_out, endpoint=False)
            samples = np.interp(x_new, x_old, samples)

        pcm = np.clip(samples, -32768, 32767).astype("<i2")
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())

    def _transcribe(self, wav_path: str) -> dict:
        """Run Vosk over a 16k mono WAV; returns the FINAL result dict.

        vosk 0.3.45's FinalResult() returns a JSON *string* (measured:
        AttributeError when treated as dict) — parse it here so both
        call sites get a real dict.
        """
        import json as _json

        from vosk import KaldiRecognizer
        model = self._load_model()
        with open(wav_path, "rb") as f:
            rec = KaldiRecognizer(model, 16000)
            rec.SetWords(True)
            while True:
                data = f.read(4096)
                if not data:
                    break
                rec.AcceptWaveform(data)
            result = rec.FinalResult()
        if isinstance(result, str):
            result = _json.loads(result)
        return result

    def solve(self, audio_bytes: bytes, src_ext: str = "") -> str:
        wav = self._to_wav16k(audio_bytes, src_ext)
        try:
            result = self._transcribe(wav)
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
        wav = self._to_wav16k(audio_bytes, src_ext)
        try:
            result = self._transcribe(wav)
        finally:
            Path(wav).unlink(missing_ok=True)
        # result[] entries are word dicts {word, conf, start, end} — keep the words
        toks = [w.get("word", "") if isinstance(w, dict) else str(w)
                for w in result.get("result", [])]
        return " ".join(t for t in toks if t).strip()
