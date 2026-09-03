"""5sim.net connector — virtual numbers se SMS-OTP receive.

Captcha+phone-verify combo: jahan captcha ke baad phone OTP bhi manga
jata hai (account-creation flows), wahan 5sim se number rent karke
OTP programmatic le lo.

API (Bearer key, https://5sim.net):
  GET  /v1/guest/countries            — free, keyless
  GET  /v1/guest/prices?country=X     — free, keyless
  GET  /v1/user/profile               — balance check
  GET  /v1/store/buy-activation-number/{country}/{operator}/{product}
  GET  /v1/user/check/{order_id}      — SMS aa ya nahi
  GET  /v1/user/finish/{order_id}     — order complete
  GET  /v1/user/cancel/{order_id}     — cancel

Usage:
    from solver.fivesim import FiveSim
    fs = FiveSim()                    # FIVESIM_KEY env ya key= arg
    fs.prices("india", "google")      # keyless price/stock check
    fs.buy("india", "any", "google")  # number rent
    fs.wait_otp(order_id, timeout=180) # poll karke OTP text
"""
import json
import os
import re
import time
import urllib.request

BASE = "https://5sim.net"


class FiveSim:
    def __init__(self, key: str | None = None, timeout: int = 15):
        self.key = key or os.environ.get("FIVESIM_KEY", "")
        self.timeout = timeout

    # ---------------------------------------------------------- core --
    def _req(self, path, method="GET", auth=True):
        url = BASE + path
        headers = {"User-Agent": "solver/1.0", "Accept": "application/json"}
        if auth:
            if not self.key:
                raise RuntimeError("FIVESIM_KEY missing — env ya key= do")
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read().decode(errors="replace")
        return json.loads(body) if body else {}

    # ------------------------------------------------------- keyless --
    def countries(self):
        return self._req("/v1/guest/countries", auth=False)

    def prices(self, country: str, product: str | None = None):
        """Keyless price/stock. prices('india') ya prices('india','google')"""
        c = self._req(f"/v1/guest/prices?country={country}", auth=False)
        if product:
            return c.get(country, {}).get(product, {})
        return c.get(country, {})

    def stock(self, country: str, product: str):
        """(cost, available_count) — keyless."""
        p = self.prices(country, product)
        if not p:
            return None, 0
        # operator rows: virtual21/virtual4/... me count>0 best pick
        rows = [(v.get("cost", 0), v.get("count", 0)) for v in p.values()
                if isinstance(v, dict)]
        rows = [r for r in rows if r[1] > 0] or [(p.get("cost", 0), 0)]
        best = min(rows, key=lambda r: r[0])
        return best[0], best[1]

    # -------------------------------------------------------- orders --
    def profile(self):
        return self._req("/v1/user/profile")

    def buy(self, country: str, operator: str = "any", product: str = "google"):
        """Number rent -> {id, phone, ...} ya error."""
        return self._req(
            f"/v1/store/buy-activation-number/{country}/{operator}/{product}")

    def check(self, order_id):
        """Order status + SMS list (aa chuke to 'sms' me)."""
        return self._req(f"/v1/user/check/{order_id}")

    def finish(self, order_id):
        return self._req(f"/v1/user/finish/{order_id}")

    def cancel(self, order_id):
        return self._req(f"/v1/user/cancel/{order_id}")

    # ----------------------------------------------------------- otp --
    def wait_otp(self, order_id, timeout=180, poll=5):
        """Poll karke pehla OTP nikaalo. Returns (otp_code, full_sms) ya (None, err)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = self.check(order_id)
            except Exception as e:
                return None, f"check fail: {str(e)[:60]}"
            smss = st.get("sms") or []
            if smss:
                sms = smss[0]
                text = sms.get("text", "") or sms.get("code", "")
                # digits extract (4-8 digit code)
                m = re.search(r"\b(\d{4,8})\b", text)
                if m:
                    return m.group(1), sms
            time.sleep(poll)
        return None, "timeout"

    def buy_and_wait(self, country: str, product: str, timeout=180,
                     operator: str = "any"):
        """One-shot: number + OTP. (phone, otp) ya (None, error)."""
        order = self.buy(country, operator, product)
        phone = order.get("phone")
        if not phone:
            return None, f"buy fail: {str(order)[:80]}"
        otp, sms = self.wait_otp(order.get("id"), timeout=timeout)
        if otp:
            self.finish(order.get("id"))
        return (phone, otp) if otp else (phone, f"otp-fail: {sms}")
