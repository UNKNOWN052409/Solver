"""API server + client + probe integration tests.

Spins up the FastAPI app with TestClient (no network), auth on/off,
and probe against LIVE demo targets (skipped without network).
"""

import base64
import os

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("SOLVER_MODEL_DIR", os.environ.get("SOLVER_TEST_MODEL_DIR", REPO))
    monkeypatch.delenv("SOLVER_API_KEY", raising=False)
    from solver.server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(monkeypatch):
    monkeypatch.setenv("SOLVER_MODEL_DIR", os.environ.get("SOLVER_TEST_MODEL_DIR", REPO))
    monkeypatch.setenv("SOLVER_API_KEY", "sekrit")
    # wipe engine cache between fixture flavors
    import solver.server as srv
    srv._engines.clear()
    from fastapi.testclient import TestClient
    with TestClient(srv.app) as c:
        yield c
    srv._engines.clear()


class TestHealth:
    def test_health_shape(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert set(body["engines"]) == {"tesseract", "ensemble", "cnn", "slot", "audio", "service"}
        assert body["auth"] is False

    def test_health_reports_auth(self, auth_client):
        assert auth_client.get("/health").json()["auth"] is True


class TestAuth:
    def test_missing_key_401(self, auth_client):
        r = auth_client.post("/solve/image64", json={"image_b64": ""})
        assert r.status_code == 401

    def test_wrong_key_401(self, auth_client):
        r = auth_client.post("/solve/image64", json={"image_b64": ""},
                             headers={"X-API-Key": "nope"})
        assert r.status_code == 401

    def test_right_key_passes_gate(self, auth_client):
        # bad image but PAST auth (400 not 401)
        r = auth_client.post("/solve/image64", json={"image_b64": "!!!"},
                             headers={"X-API-Key": "sekrit"})
        assert r.status_code == 400


class TestSolve:
    def test_garbage_image_400(self, client):
        r = client.post("/solve/image64", json={"image_b64": base64.b64encode(b"notimage").decode()})
        assert r.status_code == 400
        assert "decodable" in r.json()["detail"]

    def test_unknown_engine_400(self, client):
        r = client.post("/solve/image64",
                        json={"image_b64": "AAAA", "engine": "psychic"})
        assert r.status_code in (400, 503)  # 400 unknown name, 503 no deps

    def test_service_missing_key_400(self, client):
        r = client.post("/solve/service", json={"kind": "image"})
        assert r.status_code == 400


class TestProbe:
    def test_probe_detects_turnstile_demo(self, client):
        pytest.importorskip("requests")
        r = client.get("/probe", params={
            "url": "https://turnstile-challenge-demo.globaldots-demo.cftenant.com/"})
        if r.status_code != 200:  # no network in CI
            pytest.skip("no network")
        body = r.json()
        assert "cloudflare-turnstile" in body["tech"]
        assert body["sitekeys"]["turnstile"]  # live keys present

    def test_probe_detects_hcaptcha_17wtf(self, client):
        r = client.get("/probe", params={"url": "https://17.wtf/login"})
        if r.status_code != 200:
            pytest.skip("no network")
        body = r.json()
        assert "hcaptcha" in body["tech"]
        keys = body["sitekeys"]["hcaptcha"]
        assert keys and len(keys[0]) == 36  # uuid-style hCaptcha sitekey

    def test_probe_regex_finds_sveltekit_env(self, client):
        # regression: PUBLIC_HCAPTCHA_SITE_KEY pattern (17.wtf style)
        import re
        html = 'env: {"PUBLIC_HCAPTCHA_SITE_KEY":"c040c4de-f62a-41ae-8e14-ee12ce846382"}'
        pat = r"""["'][A-Z_]*(?:HCAPTCHA|RECAPTCHA|TURNSTILE)[A-Z_]*["']\s*[:=]\s*["']([^"']+)["']"""
        assert re.findall(pat, html) == ["c040c4de-f62a-41ae-8e14-ee12ce846382"]


class TestEnsembleEngine:
    """4th engine: ensemble (red-channel isolation + weighted voting)."""

    def test_ensemble_red_isolate_kills_colored_noise(self):
        import numpy as np
        from solver.engines.ensemble_engine import EnsembleEngine as EnsembleSolver

        # BGR image: red glyph on white + blue noise blob
        img = np.full((30, 60, 3), 255, dtype=np.uint8)
        img[10:14, 5:25, 2] = 180   # red stroke (BGR: R=180, G=B=0)
        img[10:14, 5:25, 0] = 0
        img[10:14, 5:25, 1] = 0
        img[20:28, 30:50, 0] = 220  # blue junk
        out = EnsembleSolver._red_isolate(img)
        assert out.shape == (30, 60)
        # glyph pixels survive as black text
        assert out[12, 15] == 0
        # blue junk gone (white bg)
        assert out[24, 40] == 255

    def test_ensemble_vote_positional(self):
        from solver.engines.ensemble_engine import EnsembleEngine as EnsembleSolver

        es = EnsembleSolver()
        # 4 variants; red psm7 (weight 1.2) + plain psm7 (1.0) agree on "ab"
        raws = {
            ("plain", 7): "ab",
            ("plain", 13): "ax",
            ("red", 7): "ab",
            ("red", 13): "qb",
        }
        assert es.vote(raws) == "ab"

    def test_ensemble_vote_majority_length(self):
        from solver.engines.ensemble_engine import EnsembleEngine as EnsembleSolver

        es = EnsembleSolver()
        # most variants read 3 chars; one dropped a glyph
        raws = {
            ("plain", 7): "x7k",
            ("plain", 13): "x7k",
            ("red", 7): "x7k",
            ("red", 13): "xk",
        }
        assert es.vote(raws) == "x7k"

    def test_ensemble_vote_empty(self):
        from solver.engines.ensemble_engine import EnsembleEngine as EnsembleSolver

        assert EnsembleSolver().vote({}) == ""
        assert EnsembleSolver().vote({("plain", 7): "", ("red", 7): ""}) == ""

    def test_ensemble_registered_and_consistent(self, client):
        """ensemble == tesseract availability: same binary, more passes."""
        body = client.get("/health").json()
        assert body["engines"]["ensemble"] == body["engines"]["tesseract"]

    def test_solve_image64_accepts_ensemble_engine(self, client):
        import base64

        from solver.generator import CaptchaGenerator

        img, _text = CaptchaGenerator(length=4).generate()
        import io

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        r = client.post("/solve/image64", json={
            "image_b64": base64.b64encode(buf.getvalue()).decode(),
            "engine": "ensemble",
        })
        if r.status_code == 503:
            pytest.skip("no tesseract binary on this host")
        assert r.status_code == 200
        assert r.json()["engine"] == "ensemble"
        assert isinstance(r.json()["text"], str)


class TestTesseractUserland:
    """Userland /tmp/tessroot tree: direct-loader invocation + probing."""

    def test_binary_source_reports_userland_or_system(self):
        from solver.engines.tesseract_engine import TesseractEngine

        src = TesseractEngine.binary_source()
        assert src in ("system", "userland", "missing")
        if src != "missing":
            assert TesseractEngine().available() is True

    def test_userland_diagnose_probe_ok(self):
        from solver.engines.tesseract_engine import TesseractEngine

        if TesseractEngine.binary_source() == "missing":
            pytest.skip("no tesseract anywhere")
        d = TesseractEngine().diagnose()
        assert d["probe_ok"] is True, d

    def test_health_tesseract_green_when_binary_works(self, client):
        from solver.engines.tesseract_engine import TesseractEngine

        if TesseractEngine.binary_source() == "missing":
            pytest.skip("no tesseract anywhere")
        body = client.get("/health").json()
        assert body["engines"]["tesseract"] is True
        assert body["tesseract_source"] in ("system", "userland")
