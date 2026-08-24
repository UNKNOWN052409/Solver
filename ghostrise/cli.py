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
    result = open_url(
        a.url, profile=a.profile, proxy=a.proxy,
        headed=a.headed, screenshot=a.shot,
    )
    if a.json:
        print(json.dumps(result))


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
    o.set_defaults(fn=cmd_open)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
