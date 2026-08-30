"""Client for the Solver REST API (solver.server). Use from anywhere.

    from solver.client import SolverClient
    sc = SolverClient("http://your-host:8000", api_key="optional")
    sc.solve_image_file("captcha.png")            # -> "aB9x"
    sc.solve_image_bytes(open("c.png","rb").read())
    sc.solve_image64(b64)
    sc.solve_audio_file("challenge.mp3")
    sc.probe("https://any-site.com/login")       # captcha tech fingerprint
    sc.solve_service("hcaptcha", sitekey=..., pageurl=..., key=...)
"""

import base64
from pathlib import Path

import requests


class SolverClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 120):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {}
        if api_key:
            self.headers["X-API-Key"] = api_key

    # ------------------------------------------------------------- local
    def _check(self, r: requests.Response) -> dict:
        if r.status_code != 200:
            try:
                detail = r.json().get("detail", r.text[:200])
            except Exception:
                detail = r.text[:200]
            raise RuntimeError(f"API {r.status_code}: {detail}")
        return r.json()

    def health(self) -> dict:
        return self._check(requests.get(f"{self.base}/health", timeout=self.timeout))

    # ------------------------------------------------------------- solve
    def solve_image_file(self, path: str, engine: str = "auto", model: str = "model.pt",
                        slot_x0: int = 11, slot_x1: int = 69, slot_n: int = 4) -> str:
        with open(path, "rb") as f:
            return self.solve_image_bytes(f.read(), engine, model, slot_x0, slot_x1, slot_n)

    def solve_image_bytes(self, data: bytes, engine: str = "auto", model: str = "model.pt",
                          slot_x0: int = 11, slot_x1: int = 69, slot_n: int = 4) -> str:
        r = requests.post(
            f"{self.base}/solve/image",
            files={"file": ("captcha.png", data, "image/png")},
            params={"engine": engine, "model": model,
                    "slot_x0": slot_x0, "slot_x1": slot_x1, "slot_n": slot_n},
            headers=self.headers, timeout=self.timeout,
        )
        return self._check(r)["text"]

    def solve_image64(self, b64: str, engine: str = "auto", model: str = "model.pt",
                      slot_x0: int = 11, slot_x1: int = 69, slot_n: int = 4) -> str:
        r = requests.post(
            f"{self.base}/solve/image64",
            json={"image_b64": b64, "engine": engine, "model": model,
                  "slot_x0": slot_x0, "slot_x1": slot_x1, "slot_n": slot_n},
            headers=self.headers, timeout=self.timeout,
        )
        return self._check(r)["text"]

    def solve_audio_file(self, path: str, charset: str = "0123456789") -> str:
        b64 = base64.b64encode(Path(path).read_bytes()).decode()
        r = requests.post(
            f"{self.base}/solve/audio",
            json={"audio_b64": b64, "charset": charset},
            headers=self.headers, timeout=self.timeout,
        )
        return self._check(r)["text"]

    # ------------------------------------------------------------- recon
    def probe(self, url: str) -> dict:
        r = requests.get(f"{self.base}/probe", params={"url": url},
                         headers=self.headers, timeout=self.timeout)
        return self._check(r)

    def solve_service(self, kind: str, key: str, sitekey: str = "", pageurl: str = "",
                      image_b64: str = "", proxy: str = "", phrase: bool = False) -> dict:
        r = requests.post(
            f"{self.base}/solve/service",
            json={"kind": kind, "sitekey": sitekey, "pageurl": pageurl,
                  "image_b64": image_b64, "proxy": proxy, "phrase": phrase},
            headers={**self.headers, "X-2Captcha-Key": key},
            timeout=self.timeout,
        )
        return self._check(r)
