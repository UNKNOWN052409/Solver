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
        assert set(body["engines"]) == {"tesseract", "cnn", "slot", "audio", "service"}
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
