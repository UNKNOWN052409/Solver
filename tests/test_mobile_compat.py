"""Mobile compatibility tests.

Simulates Android/Termux constraints on any dev machine:
  - font discovery must survive missing desktop font paths and resolve
    Android-style candidates
  - vault/profile paths must follow $HOME (Termux home differs)
  - the pure-Python tooling tier must not require OpenCV at all

Run: pytest tests/test_mobile_compat.py -q
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


# ---- fonts -------------------------------------------------------------

def test_font_falls_back_to_default_when_all_candidates_missing(monkeypatch):
    from solver import generator as gen

    monkeypatch.setattr(gen, "FONT_CANDIDATES", ("/nope/a.ttf", "/nope/b.ttf"))
    g = gen.CaptchaGenerator()
    img, text = g.generate()
    assert text and len(text) == g.length          # still fully functional


def test_android_font_candidate_resolves(tmp_path, monkeypatch):
    from PIL import ImageFont

    from solver import generator as gen

    fake_system_fonts = tmp_path / "system" / "fonts"
    fake_system_fonts.mkdir(parents=True)
    real_font = None
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if Path(cand).exists():
            real_font = cand
            break
    if not real_font:
        pytest.skip("no desktop ttf available to copy")
    android_ttf = fake_system_fonts / "Roboto-Regular.ttf"
    shutil.copy(real_font, android_ttf)

    monkeypatch.setattr(gen, "FONT_CANDIDATES", (str(android_ttf),))
    g = gen.CaptchaGenerator()
    # must be a real truetype font, not PIL's built-in bitmap fallback
    from PIL import ImageFont

    assert isinstance(g.font, ImageFont.FreeTypeFont)
    img, _ = g.generate()
    assert img.size == g.size


def test_generator_honors_explicit_font_path(tmp_path):
    src = None
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ):
        if Path(cand).exists():
            src = cand
            break
    if not src:
        pytest.skip("no desktop ttf to copy")
    custom = tmp_path / "my.ttf"
    shutil.copy(src, custom)

    from solver.generator import CaptchaGenerator

    g = CaptchaGenerator(font_path=str(custom))
    img, _ = g.generate()
    assert img.size == g.size


# ---- $HOME-following paths (Termux home != linux home) ------------------

def test_vault_and_profile_paths_follow_home_env(tmp_path):
    fake_home = tmp_path / "data" / "data" / "com.termux" / "files" / "home"
    fake_home.mkdir(parents=True)

    code = (
        "import sys, json; sys.path.insert(0, %r);"
        "from pathlib import Path;"
        "from ghostrise.profiles import create_profile;"
        "e = create_profile('mob1');"
        "p = Path.home() / '.ghostrise' / 'profiles' / 'mob1.json';"
        "assert p.exists(), p;"
        "print(json.dumps({'ok': True, 'home': str(Path.home())}))"
    ) % str(REPO)
    env = {**os.environ, "HOME": str(fake_home)}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env=env, cwd=REPO, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-500:]
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    assert payload["home"] == str(fake_home)


# ---- pure-python tier must not need OpenCV ------------------------------

def test_requests_tier_imports_without_opencv():
    """cf_probe / clearance_session / api_solver must stay cv2-free."""
    for rel in ("recon/cf_probe.py", "recon/clearance_session.py",
                "solver/api_solver.py"):
        src = (REPO / rel).read_text()
        assert "import cv2" not in src and "from cv2" not in src, rel

    code = (
        "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "import recon.cf_probe, recon.clearance_session, solver.api_solver;"
        "assert 'cv2' not in sys.modules, 'opencv leaked into pure-python tier';"
        "print('clean')"
    ) % (str(REPO), str(REPO / "recon"))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, cwd=REPO, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert "clean" in out.stdout
