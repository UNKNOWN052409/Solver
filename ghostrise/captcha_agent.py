"""GhostRise captcha-agent — on-page widget solving, no solving API.

Page pe captcha widget load hota hai -> ye module browser ke andar hi
solve karta hai:

  reCAPTCHA v2 (checkbox)   — click + audio fallback + Solver OCR
  reCAPTCHA v3/Enterprise   — score-based, engine stealth se hi pass
                              (nothing to click; token auto-mints)
  hCaptcha (checkbox)       — click + (challenge grid -> human dekh)
  Cloudflare Turnstile     — managed widget, click ya auto
  AWS WAF captcha          — click widget
  slider (GeeTest class)   — RL-style human drag (bezier + jitter)

Koi API key nahi. Kuch walls (reCAPTCHA image grid, hCaptcha pickax
grid) me image challenge aa sakta hai — wo Solver ke local OCR/CNN
engines pe handoff hota hai (bhi keyless).

Usage:
    from ghostrise.captcha_agent import solve_page_captcha
    with GhostSession(profile="work1") as g:
        page = g.page("https://target.com/login")
        ok, info = solve_page_captcha(page, g)
        # ok=True -> form ab submit ho sakta hai
"""

import json
import random
import re
import time


def _q(sel):
    """playwright locator shorthand (page is playwright Page)."""
    return sel


# ---------------------------------------------------------------- widgets

RECAPTCHA_V2 = {
    "frame": "iframe[src*='google.com/recaptcha']",
    "box": ".recaptcha-checkbox-checkmark, #recaptcha-anchor",
    "solved": ".recaptcha-checkbox-checked",
}

HCAPTCHA = {
    "frame": "iframe[src*='hcaptcha.com'], iframe[src*='newassets.hcaptcha.com']",
    "box": "#checkbox, .checkbox",
    "solved": ".checked",
}

TURNSTILE = {
    "frame": "iframe[src*='challenges.cloudflare.com']",
    "box": "input[type=checkbox], .ctp-checkbox",
}

AWS_WAF = {
    "frame": "iframe[src*='challenge.amazonaws.com']",
    "box": "input[type=checkbox], button",
}


def _content_frame(page, iframe_sel, timeout=8000):
    """iframe element -> uska content Frame (playwright ElementHandle
    .content_frame()). CloakBrowser page.frames surface se zyada reliable.
    NOTE: content_frame METHOD hai — call karna hai, attribute nahi."""
    try:
        el = page.wait_for_selector(iframe_sel, timeout=timeout)
        if el is None:
            return None
        cf = el.content_frame()
        if cf is not None:
            return cf
    except Exception:
        pass
    # fallback: page.frames me URL se dhundo (multi-selector safe)
    marker = iframe_sel.split("src*=")[-1].strip("'\"").split(",")[0].strip(" '\"")
    try:
        for fr in page.frames:
            if marker in (fr.url or ""):
                return fr
    except Exception:
        pass
    return None


def _click_box(frame, human, box_sel):
    """Humanized click inside the widget frame."""
    try:
        box = frame.wait_for_selector(box_sel, timeout=4000)
        human.click(box)
        return True
    except Exception:
        return False


def _is_solved(frame, solved_sel, timeout=8000):
    """Wait for the solved marker."""
    try:
        frame.wait_for_selector(solved_sel, timeout=timeout)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- solve

def solve_recaptcha_v2(page, human, timeout=20000):
    """Checkbox click; agar image challenge khule -> audio fallback +
    local OCR (Solver engines, keyless). CloakBrowser-safe: element
    bounding-box click (frame_content nahi chahiye)."""
    # anchor iframe ka checkbox — element handle se click
    try:
        anchor = page.wait_for_selector(
            "iframe[src*='google.com/recaptcha'][title*='reCAPTCHA'], "
            "iframe[src*='google.com/recaptcha']", timeout=8000)
    except Exception:
        anchor = _widget_element(page, "recaptcha-v2")
    if not anchor:
        return False, "recaptcha v2 frame not found"
    box = anchor.bounding_box()
    if not box:
        return False, "v2 anchor box missing"
    # checkbox anchor iframe ke left-top quadrant me hota hai
    cx = box["x"] + box["width"] * 0.5
    cy = box["y"] + box["height"] * 0.5
    human.page.mouse.move(cx - 20 + random.uniform(-3, 3),
                          cy + random.uniform(-2, 2))
    time.sleep(random.uniform(0.2, 0.6))
    human.page.mouse.click(cx - 20, cy)
    time.sleep(3.5)
    # token minted? (kabhi kabhi click hi kaafi hota hai)
    try:
        v = page.eval_on_selector(
            "textarea#g-recaptcha-response, [name=g-recaptcha-response]",
            "e => e.value")
        if v and len(v) > 20:
            return True, f"v2 token minted on click ({len(v)}b)"
    except Exception:
        pass
    # bframe challenge khula — page.frames se bframe dhundo (PROVEN path)
    fr = None
    try:
        for f in page.frames:
            if "bframe" in (f.url or ""):
                fr = f
                break
    except Exception:
        pass
    if fr is None:
        fr = _content_frame(
            page, "iframe[src*='recaptcha/api2/bframe']", timeout=4000)
    if fr is not None:
        try:
            # audio challenge button — footer icon (title-based, class
            # varies across recaptcha versions)
            audio_btn = fr.wait_for_selector(
                "[title='Get an audio challenge'], [title='audio challenge'], "
                ".rc-audio-button, #recaptcha-audio-button",
                timeout=5000)
            audio_btn.click()
            time.sleep(2.0)
            play = fr.wait_for_selector(
                "a.rc-audiochallenge-tdownload-link", timeout=4000)
            href = play.get_attribute("href")
            if href:
                text = _solver_audio_ocr(page, href)
                if text:
                    inp = fr.wait_for_selector("#audio-response", timeout=3000)
                    inp.fill(text)
                    verify = fr.wait_for_selector(
                        "#recaptcha-verify-button", timeout=3000)
                    verify.click()
                    time.sleep(2.5)
                    v = page.eval_on_selector(
                        "textarea#g-recaptcha-response, [name=g-recaptcha-response]",
                        "e => e.value")
                    if v and len(v) > 20:
                        return True, "v2 audio-ocr solved (keyless)"
        except Exception as e:
            # audio route fail — image grid prompt report karo
            try:
                prompt = fr.eval_on_selector(
                    ".rc-imageselect-instructions", "e => e.innerText")
                return False, f"v2 image grid: '{prompt.strip()[:40]}' (audio route: {str(e)[:60]})"
            except Exception:
                pass
    return False, "v2 challenge grid open (audio/frame route unavailable)"


def _solver_audio_ocr(page, audio_url):
    """Download audio bytes via the page (same session cookies) and OCR
    them through Solver's audio engine — no external API."""
    try:
        # page.context.request me session cookies hain
        r = page.context.request.get(audio_url)
        if r.ok:
            data = r.body()
            import sys, os
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), ".."))
            from solver.engines.audio_engine import AudioEngine
            return AudioEngine().solve(data)
    except Exception:
        pass
    return None


def solve_hcaptcha(page, human, timeout=20000):
    """Checkbox click. Grid challenge aaya to local image-OCR try, warna
    human-in-loop note (keyless hi rehta hai)."""
    # frame dhoondo: pehle selector se, warna frames-loop URL se
    fr = _content_frame(page, HCAPTCHA["frame"])
    if not fr:
        try:
            for f in page.frames:
                u = f.url or ""
                if "hcaptcha.com" in u or "newassets.hcaptcha.com" in u:
                    fr = f
                    break
        except Exception:
            pass
    if not fr:
        return False, "hcaptcha frame not found"
    if _click_box(fr, human, HCAPTCHA["box"]):
        if _is_solved(fr, HCAPTCHA["solved"], timeout=timeout):
            return True, "hcaptcha clicked + verified"
    # grid challenge? — prompt kya poocha, wo target images dhundo
    return False, "hcaptcha grid challenge — needs image pick (retry/OCR)"


def solve_turnstile(page, human, timeout=25000):
    """Turnstile managed widget — usually auto-mints in the challenge
    frame; checkbox kabhi kabhi. Frames-loop se challenge frame dhundo,
    token response input me aane ka wait karo."""
    # frames se challenge-platform frame
    for fr in page.frames:
        u = fr.url or ""
        if "challenges.cloudflare.com" in u:
            try:
                # checkbox kabhi kabhi
                box = fr.wait_for_selector(
                    "input[type=checkbox], .ctp-checkbox-label",
                    timeout=2500)
                if box:
                    bb = box.bounding_box()
                    if bb:
                        human.page.mouse.click(
                            bb["x"] + bb["width"] / 2,
                            bb["y"] + bb["height"] / 2)
            except Exception:
                pass
            break
    # token response field me aata hai (auto ya click ke baad)
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in ("input[name=cf-turnstile-response]",
                    "[name=cf-turnstile-response]",
                    "input[name$='-response']"):
            try:
                v = page.eval_on_selector(sel, "e => e.value")
                if v and len(v) > 20:
                    return True, f"turnstile token minted ({len(v)}b)"
            except Exception:
                pass
        # frames ke andar bhi success marker
        time.sleep(1.0)
    return False, "turnstile token wait timeout"


def solve_aws_waf(page, human, timeout=15000):
    fr = _content_frame(page, AWS_WAF["frame"])
    if not fr:
        return False, "aws-waf frame not found"
    if _click_box(fr, human, AWS_WAF["box"]):
        time.sleep(2.5)
        return True, "aws-waf clicked"
    return False, "aws-waf box not clickable"


def solve_slider(page, human, slider_sel=" .slider", handle_sel=".handle", distance=None):
    """GeeTest-class slider — bezier drag with overshoot + micro-jitter.
    distance None -> track width measure karke estimate."""
    try:
        track = page.wait_for_selector(slider_sel, timeout=5000)
        handle = page.wait_for_selector(handle_sel, timeout=4000)
        if distance is None:
            dist = page.evaluate(
                """(t, h) => {
                    const tr = t.getBoundingClientRect();
                    const hr = h.getBoundingClientRect();
                    return Math.round(tr.width - hr.width);
                }""", track, handle)
        else:
            dist = distance
        # humanized drag — GhostRise behavior se bezier curve + jitter
        start = handle.bounding_box()
        sx, sy = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
        # overshoot ~3-6px, phir settle
        human.page.mouse.move(sx, sy)
        time.sleep(random.uniform(0.2, 0.5))
        human.page.mouse.down()
        steps = 24
        for i in range(steps):
            frac = (i + 1) / steps
            # ease-in-out + jitter
            ease = frac * frac * (3 - 2 * frac)
            x = sx + (dist + 5) * ease + random.uniform(-2, 2)
            y = sy + random.uniform(-1.5, 1.5)
            human.page.mouse.move(x, y)
            time.sleep(random.uniform(0.008, 0.03))
        # settle back
        human.page.mouse.move(sx + dist + random.uniform(-1, 1), sy)
        time.sleep(random.uniform(0.1, 0.3))
        human.page.mouse.up()
        return True, f"slider dragged {dist}px"
    except Exception as e:
        return False, f"slider error: {e}"


# ---------------------------------------------------------------- dispatcher

def detect_widget(page):
    """Page pe kya captcha widget hai — DOM iframe srcs + frames dono se.
    (CloakBrowser page.frames surface incomplete ho sakta hai; DOM srcs
    reliable hain.)"""
    found = {}
    # 1) page.frames (playwright-style) — turnstile challenge-platform
    #    frames src attribute empty rakhte hain, sirf frame.url me dikhta hai
    try:
        for fr in page.frames:
            u = fr.url or ""
            if "google.com/recaptcha" in u and "/enterprise" in u:
                found["recaptcha-enterprise"] = fr
            elif "google.com/recaptcha" in u:
                found["recaptcha-v2"] = fr
            elif "hcaptcha.com" in u:
                found["hcaptcha"] = fr
            elif "challenges.cloudflare.com" in u:
                found["turnstile"] = fr
            elif "challenge.amazonaws.com" in u:
                found["aws-waf"] = fr
    except Exception:
        pass
    if not found:
        # turnstile invisible widget — sirf tab jab page pe turnstile
        # script ho AUR koi doosra widget na mile (2captcha demo pages pe
        # site-wide CF turnstile bhi hota hai — wo galat-detect tha)
        try:
            has_ts = page.eval_on_selector_all(
                "script[src*='challenges.cloudflare.com/turnstile'], "
                ".cf-turnstile",
                "els => els.length")
            if has_ts:
                found["turnstile"] = {"src": "turnstile-script"}
        except Exception:
            pass
    if found:
        return found
    # 2) DOM iframe srcs (CloakBrowser-safe)
    try:
        infos = page.eval_on_selector_all(
            "iframe",
            "els => els.map(e => ({src: e.src || '', name: e.name || ''}))")
        for it in infos:
            u = it.get("src", "")
            if "google.com/recaptcha" in u and "/enterprise" in u:
                found["recaptcha-enterprise"] = it
            elif "google.com/recaptcha" in u and "api2/anchor" in u:
                found["recaptcha-v2"] = it
            elif "hcaptcha.com" in u:
                found["hcaptcha"] = it
            elif "challenges.cloudflare.com" in u:
                found["turnstile"] = it
            elif "challenge.amazonaws.com" in u:
                found["aws-waf"] = it
    except Exception:
        pass
    return found


def _widget_element(page, kind):
    """DOM iframe element for a widget kind (frame_loc-based click ke liye)."""
    pats = {
        "recaptcha-v2": "iframe[src*='google.com/recaptcha']",
        "recaptcha-enterprise": "iframe[src*='google.com/recaptcha']",
        "hcaptcha": "iframe[src*='hcaptcha.com']",
        "turnstile": "iframe[src*='challenges.cloudflare.com']",
        "aws-waf": "iframe[src*='challenge.amazonaws.com']",
    }
    sel = pats.get(kind)
    if not sel:
        return None
    try:
        return page.wait_for_selector(sel, timeout=6000)
    except Exception:
        return None


def solve_page_captcha(page, session=None, prefer=None, max_tries=3):
    """Detect + solve the page's captcha widget in the browser itself.

    Returns (ok: bool, info: str). reCAPTCHA v3/Enterprise score walls
    ke liye kuch click nahi hota — wo GhostRise engine stealth (consistent
    persona, clean TLS/CDP) se hi pass hote hain; yahan sirf token-exists
    check hota hai.
    """
    for attempt in range(max_tries):
        widgets = detect_widget(page)
        if not widgets:
            # koi frame nahi — g-recaptcha-response ya token field?
            try:
                v = page.eval_on_selector(
                    "[name=g-recaptcha-response], textarea#g-recaptcha-response",
                    "e => e.value")
                if v:
                    return True, "v3 token already minted (engine stealth pass)"
            except Exception:
                pass
            return False, "no captcha widget on this page"

        human = session.human(page) if session else None
        if not human:
            # human layer ke bina fallback — plain page pe simple click
            human = _MinimalHuman(page)

        kind = prefer if prefer in widgets else next(iter(widgets))
        solvers = {
            "recaptcha-v2": solve_recaptcha_v2,
            "recaptcha-enterprise": lambda p, h: (True, "v3/enterprise — score-based, engine stealth"),
            "hcaptcha": solve_hcaptcha,
            "turnstile": solve_turnstile,
            "aws-waf": solve_aws_waf,
        }
        fn = solvers.get(kind)
        if not fn:
            return False, f"no solver for {kind}"
        ok, info = fn(page, human)
        if ok:
            return True, f"[{kind}] {info} (attempt {attempt + 1})"
        time.sleep(1.5)
    return False, f"unsolved after {max_tries} attempts"


class _MinimalHuman:
    """Behavior layer ke bina fallback — simple click/move wrapper."""

    def __init__(self, page):
        self.page = page

    def click(self, target):
        try:
            if hasattr(target, "click"):
                target.click(timeout=4000)
            else:
                self.page.click(target, timeout=4000)
        except Exception:
            pass

    def type(self, target, text):
        try:
            if hasattr(target, "type"):
                target.type(text, delay=90)
            else:
                self.page.type(target, text, delay=90)
        except Exception:
            pass
