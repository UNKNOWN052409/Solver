"""GhostRise command line.

    python3 -m ghostrise.cli create work1 --os windows --locale en-US
    python3 -m ghostrise.cli list
    python3 -m ghostrise.cli open https://target.com -p work1 \
        --proxy user:pass@host:port [--headed] [--shot page.png]
"""

import argparse
import json

from ghostrise.engine import open_url
from ghostrise.profiles import create_profile, delete_profile, list_profiles
from ghostrise.runtime import resolve
from ghostrise import store


def cmd_create(a):
    entry = create_profile(
        a.name, os_=a.os, locale=a.locale,
        screen=[a.width, a.height], cores=a.cores,
    )
    print(f"[+] identity '{a.name}' created (seed={entry['seed'][:12]}...)")


def cmd_list(a):
    profiles = list_profiles()
    if not profiles:
        print("[*] no identities yet - create one: ghostrise create <name>")
        return
    for p in profiles:
        print(f"  {p['name']:14s} os={p['os']:8s} locale={p['locale']} "
              f"screen={p['screen'][0]}x{p['screen'][1]}")


def cmd_delete(a):
    print("[+] deleted" if delete_profile(a.name) else "[!] not found")


def cmd_open(a):
    # Resolve the run-mode from flags (GPU / low-RAM / auto-detect).
    prof = resolve(gpu=a.gpu or None, low_ram=a.low_ram or None)
    print(f"[+] run-mode: gpu={prof['gpu']} igpu={prof['igpu']} "
          f"low_ram={prof['low_ram']} tier={prof['tier']}")
    result = open_url(
        a.url, profile=a.profile, proxy=a.proxy,
        headed=a.headed, screenshot=a.shot,
        browser_args=prof["browser_args"],
    )
    # Optional Drive sync: push a copy of the run config+cookies+cache up.
    stored = None
    if a.store:
        try:
            sid = store.store_session_dir(
                result.get("session_dir", a.profile),
                remote_path=a.store,
            )
            stored = sid
            print(f"[+] stored session -> gdrive:{a.store}/{sid.split('/')[-1]}")
        except Exception as e:  # noqa: BLE001 — never break browsing
            print(f"[!] store sync skipped: {e}")
    if a.json:
        out = dict(result)
        if stored:
            out["stored"] = stored
        print(json.dumps(out))


def cmd_profile(a):
    """Print the resolved GPU/low-RAM/tier profile (run-mode)."""
    prof = resolve(gpu=a.gpu or None, low_ram=a.low_ram or None)
    print(json.dumps(prof, sort_keys=True))


def main():
    ap = argparse.ArgumentParser(prog="ghostrise", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a persistent identity")
    c.add_argument("name")
    c.add_argument("--os", default="windows", choices=["windows", "macos", "linux"])
    c.add_argument("--locale", default="en-US")
    c.add_argument("--width", type=int, default=1366)
    c.add_argument("--height", type=int, default=900)
    c.add_argument("--cores", type=int, default=8)
    c.set_defaults(fn=cmd_create)

    l = sub.add_parser("list", help="list identities")
    l.set_defaults(fn=cmd_list)

    d = sub.add_parser("delete", help="delete an identity")
    d.add_argument("name")
    d.set_defaults(fn=cmd_delete)

    o = sub.add_parser("open", help="browse a URL as an identity")
    o.add_argument("url")
    o.add_argument("-p", "--profile", default="default")
    o.add_argument("--proxy", help="user:pass@host:port | host:port:user:pass")
    o.add_argument("--headed", action="store_true", help="visible window")
    o.add_argument("--shot", help="save screenshot to path")
    o.add_argument("--json", action="store_true", dest="json")
    # --- run-mode (task D3) ---
    o.add_argument("--gpu", action="store_true",
                   help="force GPU run-mode (nvidia-smi / mounted GPU)")
    o.add_argument("--low-ram", action="store_true",
                   help="force low-RAM mode (<1GB posture), disable GPU")
    o.add_argument("--store", metavar="DRIVE_DIR",
                   help="sync session config+cookies+cache to this gdrive dir "
                        "after browsing (rclone, no fuse)")
    o.set_defaults(fn=cmd_open)

    pr = sub.add_parser("profile", help="resolve & print the run-mode profile")
    pr.add_argument("--gpu", action="store_true", help="force GPU")
    pr.add_argument("--low-ram", action="store_true", help="force low-RAM")
    pr.set_defaults(fn=cmd_profile)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
