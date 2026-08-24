# Mobile Compatibility Guide 📱

Run the arsenal on Android (Termux). Strategic bonus: your phone's own
connection is already a residential/mobile-class IP - the exact reputation
class that passes bot walls. Agents running ON phones exit through
premium IPs for free.

## What runs where

| Component | Termux native | proot-distro Ubuntu |
|---|---|---|
| Solver core (preprocess/segment/generate) | ✅ | ✅ |
| API solvers (2captcha flows) | ✅ | ✅ |
| `cf_probe` requests tier | ✅ | ✅ |
| `clearance_session` test/replay | ✅ | ✅ |
| Tesseract OCR engine | ✅ via pkg | ✅ |
| **GhostRise / CloakBrowser engine** | ❌ desktop binaries only | ✅ |

## Tier A - Termux native (5 minutes)

```bash
pkg update && pkg install python git libjpeg-turbo libcrypt
pip install -U pip wheel
git clone https://github.com/UNKNOWN052409/Solver.git && cd Solver

# opencv on termux comes from the community repo:
pkg install tur-repo
pkg install python-opencv
pip install -r requirements.txt        # numpy/pillow/requests/pytest resolve here

# optional OCR engine:
pkg install tesseract

# verify:
python3 -m pytest tests/ -q
python3 -m solver.cli generate /tmp/cap -n 5
python3 recon/cf_probe.py https://lmarena.ai
```

## Tier B - GhostRise on phone via proot-distro

```bash
pkg install proot-distro
proot-distro install ubuntu
proot-distro login ubuntu
```

Inside Ubuntu (arm64):
```bash
apt update && apt install -y python3-pip git wget xvfb
pip install cloakbrowser ghostrise-deps   # or: pip install -r requirements.txt
python3 -m ghostrise.cli open https://instantproxies.com --shot ip.png
```

Notes:
- Desktop linux-arm64 Chromium builds run under proot because they need
  glibc; Termux native is Bionic and will never load them.
- For headed mode inside proot use Xvfb (`xvfb-run ...`) or Termux:X11.
- First CloakBrowser launch downloads its binary - keep ~500MB free.

## iOS

Not supported: no Chromium/Firefox builds for iOS user space, and Python
runtimes there are sandboxed away from sockets. iPhone users should point
Safari at a self-hosted dashboard instead (roadmap: ghostpipe web UI).

## Battery & network notes

- Long PoW challenges (Vercel ~30s) drain battery; batch mints.
- CGNAT IPs rotate - vault entries store source_ip and warn on mismatch;
  re-mint after airplane-mode toggles.
- Keep screen-on during headed runs; Android freezes background renders.
