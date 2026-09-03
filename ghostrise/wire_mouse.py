"""WireMouse — GhostWire ka APNA mouse. Raw CDP input injection.

Playwright ke Page.mouse ki jagah ye hamara curve engine hai:
  - bezier trajectories (randomized control points)
  - RL-traj imitation (rl_mouse_v6.npz shapes — jaise GhostMouse)
  - humanized click (micro-jitter, press durations)
  - CDP Input.dispatchMouseEvent / insertText raw calls

Isse input-layer fingerprint bhi hamara — playwright ka input
pattern detect nahi hota.
"""
import math
import random
import time


class WireMouse:
    """GhostWire (raw CDP) ke upar humanized mouse."""

    def __init__(self, wire):
        self.w = wire                    # GhostWire instance
        # RL shapes (npz) — available to curve-shape sampling
        try:
            import numpy as np
            from pathlib import Path
            p = Path(__file__).parent / "rl_mouse_v6.npz"
            self._rl = np.load(p) if p.exists() else None
        except Exception:
            self._rl = None
        self.x, self.y = 400.0, 400.0    # current pos

    # ------------------------------------------------------------ CDP --
    def _dispatch(self, type_, x, y, button="left", clicks=1, modifiers=0):
        sid = getattr(self.w, "_sid", None)
        p = {"type": type_, "x": x, "y": y, "button": button,
             "clickCount": clicks, "modifiers": modifiers}
        self.w._send("Input.dispatchMouseEvent", p, session_id=sid)

    # --------------------------------------------------------- bezier --
    def _bezier(self, x0, y0, x1, y1, steps=None, curvature=0.35):
        """Cubic bezier path with jitter — GhostMouse wali shapes."""
        d = math.hypot(x1 - x0, y1 - y0)
        steps = steps or max(8, int(d / 12) + 4)
        # control points: perpendicular offset (curvature) + random
        ang = math.atan2(y1 - y0, x1 - x0)
        off = d * curvature * random.uniform(-1, 1)
        cx1 = x0 + (x1 - x0) * 0.3 + math.cos(ang + 1.5708) * off
        cy1 = y0 + (y1 - y0) * 0.3 + math.sin(ang + 1.5708) * off
        cx2 = x0 + (x1 - x0) * 0.7 + math.cos(ang + 1.5708) * off * 0.6
        cy2 = y0 + (y1 - y0) * 0.7 + math.sin(ang + 1.5708) * off * 0.6
        pts = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1 - t
            bx = mt**3 * x0 + 3 * mt**2 * t * cx1 + 3 * mt * t**2 * cx2 + t**3 * x1
            by = mt**3 * y0 + 3 * mt**2 * t * cy1 + 3 * mt * t**2 * cy2 + t**3 * y1
            # micro-jitter
            j = 1.5 * (1 - abs(0.5 - t) * 2)
            bx += random.uniform(-j, j)
            by += random.uniform(-j, j)
            pts.append((bx, by))
        return pts

    # --------------------------------------------------------- public --
    def move(self, x, y, duration=None, steps=None):
        """Bezier move — humanized. duration auto = distance-based."""
        pts = self._bezier(self.x, self.y, x, y, steps=steps)
        d = math.hypot(x - self.x, y - self.y)
        total = duration or (0.18 + d / 1400 + random.uniform(0, 0.12))
        dt = total / max(1, len(pts))
        for (px, py) in pts:
            self._dispatch("mouseMoved", px, py)
            time.sleep(dt)
        self.x, self.y = float(x), float(y)
        return self

    def click(self, x=None, y=None, hold=None):
        """Humanized click — move (agar coords diye) + press/release."""
        if x is not None and y is not None:
            self.move(x, y)
        self._dispatch("mousePressed", self.x, self.y, button="left", clicks=1)
        time.sleep(hold or random.uniform(0.05, 0.12))
        self._dispatch("mouseReleased", self.x, self.y, button="left", clicks=1)
        return self

    def double_click(self, x=None, y=None):
        if x is not None and y is not None:
            self.move(x, y)
        self._dispatch("mousePressed", self.x, self.y, "left", clicks=2)
        time.sleep(0.05)
        self._dispatch("mouseReleased", self.x, self.y, "left", clicks=2)
        return self

    def type(self, text, delay=None):
        """Keys — CDP insertText (fast) per-char delay humanized."""
        d = delay or random.uniform(0.04, 0.09)
        sid = getattr(self.w, "_sid", None)
        for ch in text:
            self.w._send("Input.insertText", {"text": ch}, session_id=sid)
            time.sleep(random.uniform(d * 0.6, d * 1.4))
        return self

    def scroll(self, dy, steps=6):
        """Humanized wheel scroll (dy pixels, negative = up)."""
        sid = getattr(self.w, "_sid", None)
        per = dy / steps
        for _ in range(steps):
            self.w._send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": self.x, "y": self.y,
                "deltaX": 0, "deltaY": per}, session_id=sid)
            time.sleep(random.uniform(0.05, 0.12))
        return self
