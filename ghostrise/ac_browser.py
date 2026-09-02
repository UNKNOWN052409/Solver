"""GhostRise anti-captcha browser layer.

GhostSession ke upar ek wrapper jo HAR page ko "anti-captcha browser"
banata hai: wall detect -> auto-solve -> reload -> verify. Ab agent
code ko captcha ka dhyan hi nahi rakhna — `open(url)` normal page
deta hai (solved).

    from ghostrise.ac_browser import ACSession
    with ACSession(profile="work1") as b:
        page = b.open("https://site-with-captcha.com/login")
        # page pe ab captcha nahi hoga (ya info milegi kyu nahi hua)
"""
import time

from ghostrise.engine import GhostSession
from ghostrise.captcha_agent import (
    detect_widget,
    solve_page_captcha,
)

WALL_MARKERS = (
    "Verifying your browser", "Checking your browser", "Just a moment",
    "Checking browser integrity", "Automated verification failed",
    "Enable JavaScript and cookies to continue",
)


class ACSession:
    """Anti-captcha browser — GhostSession + wall/captcha auto-clear.

    Ladder har URL pe:
      1. load page
      2. wall-marker dikha -> wait/reload loop (JS interstitials
         real engine me khud clear hote hain)
      3. captcha widget dikha -> solve_page_captcha (keyless stack:
         clicks, stealth, OCR, vision hook)
      4. retry dono max_retries tak
      5. normal Page return (chahe solve fail ho — info ke saath)
    """

    def __init__(self, profile="default", proxy=None, headed=False,
                 max_retries=2, solve=True, engine="auto"):
        self.g = GhostSession(profile=profile, proxy=proxy,
                              headed=headed, engine=engine)
        self.max_retries = max_retries
        self.solve = solve
        self.last_info = None
        self._entered = False

    # context-manager passthrough
    def __enter__(self):
        self._entered = True
        self.g.__enter__()
        return self

    def __exit__(self, *exc):
        return self.g.__exit__(*exc)

    # ------------------------------------------------------- core
    def _body_head(self, page, n=300):
        try:
            return page.eval_on_selector("body", f"e => e.innerText.slice(0, {n})")
        except Exception:
            return ""

    def _wait_wall_clear(self, page, max_wait=25):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            body = self._body_head(page, 200)
            if not any(m in body for m in WALL_MARKERS):
                return True
            time.sleep(2.5)
        # last resort reload
        try:
            page.reload(wait_until="domcontentloaded")
            time.sleep(5)
            body = self._body_head(page, 200)
            return not any(m in body for m in WALL_MARKERS)
        except Exception:
            return False

    def open(self, url, wait=5, solve_vision=None):
        """URL kholo; wall/captcha ho to clear karke normal page do.

        solve_vision: optional callable(tiles_b64, prompt) -> [labels]
        — v2/hCaptcha grids ke liye external dimaag (AI agent ka
        multimodal vision ya local vision-serve). None = keyless
        built-ins hi.
        """
        info = {"wall": None, "captcha": None, "attempts": 0}
        page = self.g.page(url)
        time.sleep(wait)

        for attempt in range(self.max_retries):
            info["attempts"] = attempt + 1
            # 1) JS interstitial wall
            body = self._body_head(page, 250)
            if any(m in body for m in WALL_MARKERS):
                info["wall"] = "detected -> clearing"
                if self._wait_wall_clear(page):
                    info["wall"] = "cleared"
                else:
                    info["wall"] = "uncleared (engine-level trust issue)"

            # 2) captcha widget
            widgets = detect_widget(page)
            if widgets and self.solve:
                kind = next(iter(widgets))
                ok, sinfo = solve_page_captcha(self.g.human(page) and page
                                                or page, self.g)
                info["captcha"] = f"{kind}: {sinfo}"
                if ok:
                    # solved — naya page state
                    time.sleep(1.5)
            else:
                info["captcha"] = "none"

            # final check — page ab clean?
            body = self._body_head(page, 200)
            clean = (not any(m in body for m in WALL_MARKERS)
                     and not detect_widget(page))
            if clean:
                break
            # retry: reload
            try:
                page.reload(wait_until="domcontentloaded")
                time.sleep(4)
            except Exception:
                pass

        self.last_info = info
        return page

    def human(self, page):
        return self.g.human(page)
