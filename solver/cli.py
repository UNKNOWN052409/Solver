"""Command line interface.

    python -m solver.cli generate ./data -n 200
    python -m solver.cli solve image.png [--engine auto] [--model model.pt]
    python -m solver.cli train --out model.pt -n 4000
    python -m solver.cli api-image image.png --key YOURKEY
    python -m solver.cli recaptcha SITEKEY https://target/login --key YOURKEY
"""

import argparse
import sys
from pathlib import Path

import numpy as np


def cmd_generate(a):
    from solver.generator import CaptchaGenerator

    gen = CaptchaGenerator(length=a.length, size=(a.width, a.height))
    paths = gen.save_batch(a.outdir, a.n)
    print(f"[+] Wrote {len(paths)} samples to {a.outdir}/")


def build_engine(a):
    from solver.engines.cnn_engine import CNNEngine
    from solver.engines.tesseract_engine import TesseractEngine

    if a.engine == "cnn":
        return CNNEngine(a.model)
    if a.engine == "tesseract":
        return TesseractEngine()
    # auto
    if CNNEngine.available(a.model):
        return CNNEngine(a.model)
    if TesseractEngine().available():
        return TesseractEngine()
    sys.exit(
        "[!] No engine available. Either:\n"
        "    - install tesseract:  sudo apt-get install tesseract-ocr\n"
        f"    - or train a model:   python -m solver.cli train --out {a.model}"
    )


def cmd_solve(a):
    import cv2

    from solver.preprocessor import Preprocessor

    engine = build_engine(a)
    raw = cv2.imread(str(Path(a.image)))
    if raw is None:
        sys.exit(f"[!] Cannot read image: {a.image}")

    prep = Preprocessor(remove_lines=a.remove_lines)
    binary = prep.run(raw)

    if a.debug:
        prep.save_debug({"1_binary": binary})
        print("[*] Debug images saved to ./debug/")

    text = engine.solve(binary)
    print(f"{engine.name}: {text}")


def cmd_train(a):
    from training.train_cnn import main as train_main
    sys.argv = ["train_cnn.py", "--out", a.out, "-n", str(a.n),
                "--epochs", str(a.epochs), "--length", str(a.length)]
    train_main()


def cmd_api_image(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_image(Path(a.image).read_bytes()))


def cmd_recaptcha(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_recaptcha_v2(a.sitekey, a.pageurl))


def cmd_hcaptcha(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key)
    print(svc.solve_hcaptcha(a.sitekey, a.pageurl))


def cmd_cf_clearance(a):
    from solver.api_solver import TwoCaptchaSolver

    svc = TwoCaptchaSolver(a.key, timeout=300.0)
    result = svc.solve_cloudflare(a.url, a.proxy)
    print(f"cf_clearance: {result['cf_clearance']}")
    print(f"user_agent:   {result['user_agent']}")
    for k, v in result["cookies"].items():
        print(f"cookie:       {k}={v}")


def main():
    p = argparse.ArgumentParser(prog="solver", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="synthesize labeled captcha samples")
    g.add_argument("outdir")
    g.add_argument("-n", type=int, default=100)
    g.add_argument("--length", type=int, default=5)
    g.add_argument("--width", type=int, default=170)
    g.add_argument("--height", type=int, default=60)
    g.set_defaults(fn=cmd_generate)

    s = sub.add_parser("solve", help="solve an image captcha locally")
    s.add_argument("image")
    s.add_argument("--engine", choices=["auto", "tesseract", "cnn"], default="auto")
    s.add_argument("--model", default="model.pt")
    s.add_argument("--remove-lines", action="store_true")
    s.add_argument("--debug", action="store_true")
    s.set_defaults(fn=cmd_solve)

    t = sub.add_parser("train", help="train a CNN on synthetic data")
    t.add_argument("--out", default="model.pt")
    t.add_argument("-n", type=int, default=4000)
    t.add_argument("--epochs", type=int, default=12)
    t.add_argument("--length", type=int, default=5)
    t.set_defaults(fn=cmd_train)

    ai = sub.add_parser("api-image", help="solve image captcha via 2captcha API")
    ai.add_argument("image")
    ai.add_argument("--key", required=True)
    ai.set_defaults(fn=cmd_api_image)

    rc = sub.add_parser("recaptcha", help="solve reCAPTCHA v2 via 2captcha API")
    rc.add_argument("sitekey")
    rc.add_argument("pageurl")
    rc.add_argument("--key", required=True)
    rc.set_defaults(fn=cmd_recaptcha)

    hc = sub.add_parser("hcaptcha", help="solve hCaptcha via 2captcha API")
    hc.add_argument("sitekey")
    hc.add_argument("pageurl")
    hc.add_argument("--key", required=True)
    hc.set_defaults(fn=cmd_hcaptcha)

    cfc = sub.add_parser(
        "cf-clearance",
        help="clear a Cloudflare challenge via 2captcha THROUGH your proxy "
             "(returns cf_clearance bound to that IP)",
    )
    cfc.add_argument("url")
    cfc.add_argument("--proxy", required=True, help="user:pass@host:port")
    cfc.add_argument("--key", required=True)
    cfc.set_defaults(fn=cmd_cf_clearance)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
