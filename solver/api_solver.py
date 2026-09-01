"""Third-party solving service backend (2captcha-compatible APIs).

For captcha families you can't crack locally (reCAPTCHA v2/v3, hCaptcha,
Turnstile, enterprise image grids): ship the task to a human-power farm
and get a token back. Standard in.php/res.php protocol.

NOTE: this module must stay importable in the pure-python "requests
tier" (no cv2/numpy) — see tests/test_mobile_compat.py. The local
ensemble OCR engine lives in solver/engines/ensemble_engine.py.
"""

import base64
import time

import requests


class TwoCaptchaSolver:
    IN_URL = "https://2captcha.com/in.php"
    RES_URL = "https://2captcha.com/res.php"
    TASK_URL = "https://api.2captcha.com/createTask"

    def __init__(self, api_key: str, poll_interval: float = 5.0, timeout: float = 180.0):
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

    def solve_recaptcha_v3(
        self,
        sitekey: str,
        pageurl: str,
        action: str = "",
        min_score: float = 0.4,
        enterprise: bool = False,
    ) -> str:
        """reCAPTCHA v3 / Enterprise (score-based, action-scoped).

        v3 tokens carry a score + action; sites like arena.ai validate
        the action string (e.g. ``chat_submit``) server-side, so the
        solving farm must mint the token with the same action.
        Enterprise v3 uses the same protocol with ``enterprise=1``
        (2captcha) — pass the enterprise sitekey as usual.
        """
        params = {
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": pageurl,
            "version": "v3",
            "min_score": min_score,
        }
        if action:
            params["action"] = action
        if enterprise:
            params["enterprise"] = 1
        task_id = self._submit(params)
        return self._poll(task_id)

    def solve_recaptcha_enterprise(
        self,
        sitekey: str,
        pageurl: str,
        action: str = "",
        min_score: float = 0.4,
    ) -> str:
        """reCAPTCHA Enterprise (grecaptcha.enterprise.execute)."""
        return self.solve_recaptcha_v3(
            sitekey, pageurl, action=action, min_score=min_score, enterprise=True
        )

    def solve_turnstile(self, sitekey: str, pageurl: str) -> str:
        """Cloudflare Turnstile widget -> cf-turnstile-response token."""
        task_id = self._submit(
            {"method": "turnstile", "sitekey": sitekey, "pageurl": pageurl}
        )
        return self._poll(task_id)

    def solve_geetest(self, gt: str, challenge: str, pageurl: str) -> str:
        """GeeTest v3/v4 (slider) -> challenge/validate token.

        `gt` + `challenge` page ke initGeetest(...) JS call se milte hain —
        server.py /inspect dono extract karta hai.
        """
        task_id = self._submit(
            {"method": "geetest", "gt": gt, "challenge": challenge,
             "pageurl": pageurl}
        )
        return self._poll(task_id)

    def solve_funcaptcha(self, publickey: str, pageurl: str,
                         surl: str = "") -> str:
        """FunCaptcha / Arkose (Netflix-class logins) -> token."""
        params = {"method": "funcaptcha", "publickey": publickey,
                  "pageurl": pageurl}
        if surl:
            params["surl"] = surl
        task_id = self._submit(params)
        return self._poll(task_id)

    def solve_datadome(self, pageurl: str, proxy: str,
                       user_agent: str, captcha_url: str) -> dict:
        """DataDome interstitial (Amazon-class) -> datadome cookie.

        A service worker solves through `proxy` and returns the datadome
        cookie bound to that exit IP — replay it on requests from the
        same proxy.
        """
        host, port, user, password = self._split_proxy(proxy)
        if not user:
            raise ValueError("authenticated proxy required (user:pass@host:port)")
        task = {
            "type": "AntiDataDomeTask",
            "websiteURL": pageurl,
            "captchaURL": captcha_url,
            "proxyType": "http",
            "proxyAddress": host,
            "proxyPort": int(port),
            "proxyLogin": user,
            "proxyPassword": password,
            "userAgent": user_agent,
        }
        solution = self._poll_task(self._create_task(task))
        return {
            "datadome": solution.get("cookie", ""),
            "user_agent": solution.get("userAgent", user_agent),
            "solution": solution,
        }

    def solve_hcaptcha(self, sitekey: str, pageurl: str) -> str:
        """hCaptcha -> response token."""
        task_id = self._submit(
            {"method": "hcaptcha", "sitekey": sitekey, "pageurl": pageurl}
        )
        return self._poll(task_id)

    # ---- Cloudflare managed-challenge / Turnstile interstitials ----

    def _create_task(self, task: dict) -> str:
        r = requests.post(
            self.TASK_URL,
            json={"clientKey": self.key, "task": task},
            timeout=30,
        )
        data = r.json()
        if data.get("errorId") != 0:
            raise RuntimeError(f"2captcha createTask failed: {data}")
        return data["taskId"]

    def _poll_task(self, task_id: str) -> dict:
        import time as _t

        deadline = _t.time() + self.timeout
        while _t.time() < deadline:
            _t.sleep(self.poll_interval)
            r = requests.post(
                "https://api.2captcha.com/getTaskResult",
                json={"clientKey": self.key, "taskId": task_id},
                timeout=30,
            )
            data = r.json()
            if data.get("status") == "ready":
                return data["solution"]
            if data.get("status") != "processing":
                raise RuntimeError(f"2captcha task error: {data}")
        raise TimeoutError("2captcha task did not finish in time")

    @staticmethod
    def _split_proxy(p: str):
        """host:port:user:pass | user:pass@host:port -> (host, port, user, pass)"""
        rest = p.split("://", 1)[-1]
        if "@" in rest:
            creds, hostport = rest.rsplit("@", 1)
            user, _, password = creds.partition(":")
        else:
            parts = rest.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                return host, port, user, password
            hostport = rest
            user = password = None
        host, port = hostport.rsplit(":", 1)
        return host, port, user, password

    def solve_cloudflare(self, pageurl: str, proxy: str) -> dict:
        """Clear a Cloudflare managed challenge FROM your exit IP.

        A service worker browses through `proxy`, solves the challenge
        there, and returns the cf_clearance cookie + matching UA — both
        bound to that proxy IP, ready to replay through it.
        """
        host, port, user, password = self._split_proxy(proxy)
        if not user:
            raise ValueError("authenticated proxy required (user:pass@host:port)")
        task = {
            "type": "AntiCloudflareTask",
            "websiteURL": pageurl,
            "proxyType": "http",
            "proxyAddress": host,
            "proxyPort": int(port),
            "proxyLogin": user,
            "proxyPassword": password,
        }
        solution = self._poll_task(self._create_task(task))
        return {
            "cf_clearance": solution.get("cookies", {}).get("cf_clearance", ""),
            "user_agent": solution.get("user_agent", ""),
            "headers": solution.get("headers", {}),
            "cookies": solution.get("cookies", {}),
        }
