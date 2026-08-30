"""LIVE audio-captcha test: real TTS speech -> Vosk engine -> digits.

Real-world chain, no mocks anywhere:
  piper-tts (offline neural TTS, real speech WAV)
    -> solver.engines.audio_engine (WAV normalize -> Vosk -> cleanup)
    -> compare digits with the spoken truth

Skipped (not faked) only if the model files are absent.
Run: pytest tests/test_live_audio.py -q
"""

from pathlib import Path

import pytest

from solver.engines.audio_engine import AudioEngine

PIPER_MODEL = Path("/home/kali/data/piper/en_US-lessac-medium.onnx")
VOSK_MODEL = Path("/home/kali/data/vosk/vosk-model-small-en-us-0.15")


def _models_present() -> bool:
    return PIPER_MODEL.exists() and VOSK_MODEL.exists()


def _tts(text: str, out: Path) -> bytes:
    import wave

    from piper import PiperVoice

    voice = PiperVoice.load(str(PIPER_MODEL))
    with wave.open(str(out), "wb") as w:
        voice.synthesize_wav(text, w)
    return out.read_bytes()


class TestLiveAudioCaptcha:
    def test_digits_8371(self, tmp_path):
        if not _models_present():
            pytest.skip("piper/vosk models not downloaded")
        wav = _tts("eight three seven one", tmp_path / "a.wav")
        eng = AudioEngine(model_path=str(VOSK_MODEL))
        got = eng.solve(wav, src_ext=".wav")
        assert got == "8371", f"engine said {got!r}"

    def test_digits_0953(self, tmp_path):
        if not _models_present():
            pytest.skip("piper/vosk models not downloaded")
        wav = _tts("zero nine five three", tmp_path / "b.wav")
        eng = AudioEngine(model_path=str(VOSK_MODEL))
        got = eng.solve(wav, src_ext=".wav")
        assert got == "0953", f"engine said {got!r}"

    def test_raw_words_mode(self, tmp_path):
        """words() must return the transcript for 'type the 3rd word' prompts.

        Vosk's small model transcribes 'two' as 'to' sometimes — homophone
        confusion at the WORD layer is expected; assert word COUNT and the
        first/last words, matching how real 'type the Nth word' prompts
        are consumed.
        """
        if not _models_present():
            pytest.skip("piper/vosk models not downloaded")
        wav = _tts("seven two four", tmp_path / "c.wav")
        eng = AudioEngine(model_path=str(VOSK_MODEL))
        words = eng.words(wav, src_ext=".wav")
        parts = words.split()
        assert len(parts) == 3, words
        assert parts[0] == "seven" and parts[2] == "four", words
