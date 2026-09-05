"""Human behavior primitives for agents driving GhostRise pages.

Agents die on behavioral detection because they act instantly and
linearly. This module wraps playwright-style pages with human-shaped
actions:

    - Bezier-curve mouse travel with overshoot and micro-jitter
    - lognormal keystroke cadence (60-180ms), rare typo + correction
    - eased chunked scrolling with variable pauses
    - randomized reading dwells between actions

Usage:
    with GhostSession(...) as g:
        page = g.page("https://target.com")
        human = g.human(page)
        human.click("button#submit")
        human.type("input#q", "residential proxies")
        human.scroll(900)
"""

import math
import random
import time


def _pt(p):
    return {"x": float(p["x"]), "y": float(p["y"])}


def bezier_path(start, end, segments=28, curvature=None):
    """Quadratic Bezier from start to end with a random control point."""
    start, end = _pt(start), _pt(end)
    dist = math.hypot(end["x"] - start["x"], end["y"] - start["y"])
    curvature = curvature if curvature is not None else random.uniform(-0.35, 0.35)
    # control point offset perpendicular to the straight line
    mx, my = (start["x"] + end["x"]) / 2, (start["y"] + end["y"]) / 2
    dx, dy = end["x"] - start["x"], end["y"] - start["y"]
    nx, ny = -dy / max(dist, 1), dx / max(dist, 1)
    off = dist * curvature
    ctrl = {"x": mx + nx * off, "y": my + ny * off}

    pts = []
    for i in range(segments + 1):
        t = i / segments
        # ease-in-out so motion starts and ends slow
        te = t * t * (3 - 2 * t)
        x = (1 - te) ** 2 * start["x"] + 2 * (1 - te) * te * ctrl["x"] + te ** 2 * end["x"]
        y = (1 - te) ** 2 * start["y"] + 2 * (1 - te) * te * ctrl["y"] + te ** 2 * end["y"]
        # micro-jitter grows mid-flight, settles at target
        jitter = math.sin(math.pi * t) * min(dist / 60, 2.2)
        x += random.uniform(-jitter, jitter)
        y += random.uniform(-jitter, jitter)
        pts.append({"x": x, "y": y})
    return pts


class HumanActions:
    """Human-shaped interaction wrappers over a playwright-style page."""

    def __init__(self, page, typo_rate: float = 0.02):
        self.page = page
        self.typo_rate = typo_rate

    # ---- low level -------------------------------------------------

    def move_to(self, target, overshoot=False):
        # locator/handle OBJECT (wire _WireLocator) ya playwright-string
        if hasattr(target, "bounding_box"):
            box = target.bounding_box()
        elif hasattr(target, "count"):  # wire locator — JS-rect fallback
            rects = getattr(target, "rects", None)
            box = rects()[0] if rects and rects() else None
        else:
            box = self.page.locator(target).bounding_box()
        if not box:
            raise ValueError(f"element not found: {target}")
        dest = {
            "x": box["x"] + box["width"] * random.uniform(0.3, 0.7),
            "y": box["y"] + box["height"] * random.uniform(0.3, 0.7),
        }
        path = bezier_path(
            self.page.evaluate("({x: window.mouseX || 0, y: window.mouseY || 0})"), dest
        )
        for p in path[:-1]:
            self.page.mouse.move(p["x"], p["y"])
            time.sleep(random.uniform(0.004, 0.016))
        if overshoot:  # real users sometimes overshoot then correct
            ov = {
                "x": dest["x"] + random.uniform(-18, 18),
                "y": dest["y"] + random.uniform(-10, 10),
            }
            self.page.mouse.move(ov["x"], ov["y"])
            time.sleep(random.uniform(0.03, 0.09))
            self.page.mouse.move(dest["x"], dest["y"])
        self.page.evaluate(f"window.mouseX={dest['x']};window.mouseY={dest['y']}")
        return dest

    def _pause(self, lo=0.05, hi=0.22):
        time.sleep(random.lognormvariate(math.log((lo + hi) / 2), 0.4))

    # ---- public actions ---------------------------------------------

    def click(self, target, pause_after=(0.15, 0.7)):
        self.move_to(target, overshoot=random.random() < 0.25)
        self._pause(0.04, 0.14)  # aim-settle before firing
        self.page.mouse.down()
        time.sleep(random.uniform(0.055, 0.13))  # real presses aren't instant
        self.page.mouse.up()
        self._pause(*pause_after)

    def type(self, target, text: str, submit=False):
        self.click(target, pause_after=(0.2, 0.6))
        for ch in text:
            if random.random() < self.typo_rate and ch.isalpha():
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                self.page.keyboard.type(wrong, delay=random.uniform(50, 120))
                self._pause(0.12, 0.35)  # notice mistake
                self.page.keyboard.press("Backspace")
                self._pause(0.08, 0.2)
            self.page.keyboard.type(ch, delay=random.uniform(55, 165))
        if submit:
            self._pause(0.25, 0.8)
            self.page.keyboard.press("Enter")

    def scroll(self, amount: int, direction: str = "down"):
        sign = -1 if direction == "up" else 1
        remaining = abs(amount)
        while remaining > 0:
            chunk = min(int(abs(remaining) * random.uniform(0.2, 0.45)) + 40, remaining)
            self.page.mouse.wheel(0, sign * chunk)
            remaining -= chunk
            time.sleep(random.uniform(0.08, 0.3))  # deceleration gaps

    def dwell(self, seconds_range=(1.2, 4.5)):
        """Reading/idle pause - agents forget these exist."""
        time.sleep(random.uniform(*seconds_range))

    def visit_like_human(self, url, read_first=True):
        """Full human-ish flow: load, scan down a bit, dwell, settle."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if read_first:
            self.dwell((1.5, 4.0))
            self.scroll(random.randint(200, 700))
            self.dwell((1.0, 3.0))
        return self.page
