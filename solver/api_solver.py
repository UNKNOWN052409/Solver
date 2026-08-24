"""Third-party solving service backend (2captcha-compatible APIs).

For captcha families you can't crack locally (reCAPTCHA v2/v3, hCaptcha,
Turnstile, enterprise image grids): ship the task to a human-power farm
and get a token back. Standard in.php/res.php protocol.
"""

import base64
import time

import requests


class TwoCaptchaSolver:
    IN_URL = "https://2captcha.com/in.php"
    RES_URL = "https://2captcha.com/res.php"

    def __init__(self, api_key: str, poll_interval: float = 5.0, timeout: float = 120.0):
        self.key = api_key
        self.poll_interval = poll_interval
        self.timeout = timeout

    def _submit(self, params: dict) -> str:
        params = {"key": self.key, "json": "1", **params}
        r = requests.post(self.IN_URL, data=params, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            raise RuntimeError(f"2captcha submit failed: {data.get('request')}")
        return data["request"]  # task id

    def _poll(self, task_id: str) -> str:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            r = requests.get(
                self.RES_URL,
                params={"key": self.key, "action": "get", "id": task_id, "json": "1"},
                timeout=30,
            )
            data = r.json()
            if data.get("status") == 1:
                return data["request"]
            if data.get("request") != "CAPCHA_NOT_READY":
                raise RuntimeError(f"2captcha error: {data.get('request')}")
        raise TimeoutError("2captcha task did not finish in time")

    def solve_image(self, image_bytes: bytes, phrase: bool = False) -> str:
        """Base64 image captcha -> text."""
        b64 = base64.b64encode(image_bytes).decode()
        task_id = self._submit({"method": "base64", "body": b64, "phrase": int(phrase)})
        return self._poll(task_id)

    def solve_recaptcha_v2(self, sitekey: str, pageurl: str) -> str:
        """reCAPTCHA v2 -> g-recaptcha-response token."""
        task_id = self._submit(
            {"method": "userrecaptcha", "googlekey": sitekey, "pageurl": pageurl}
        )
        return self._poll(task_id)

    def solve_hcaptcha(self, sitekey: str, pageurl: str) -> str:
        """hCaptcha -> response token."""
        task_id = self._submit(
            {"method": "hcaptcha", "sitekey": sitekey, "pageurl": pageurl}
        )
        return self._poll(task_id)
