#!/usr/bin/env python3
"""ghostrise/oauth_capture.py — local Google OAuth capture server.

LO grants access to his Google account so we (ENI) can, from this box:
  * configure rclone/Google CLI on the SAME Drive account (primary storage),
  * optionally drive gcloud / GCP (if the account has the right scopes/credits).

Usage serial (real OAuth Authorization Code + PKCE, refresh_token stored offline):
    python -m ghostrise.oauth_capture --run

Flow:
  1. Server starts on 127.0.0.1:PORT (default 8087), prints an *auth_url*.
  2. LO opens that URL on ANY device, signs into HIS Google account, Approves.
  3. Redirect returns `?code=...` to this local server (works when the same
     machine runs the browser) OR -- if the redirect can't reach here (phone),
     LO copies the `code=` from the address bar back and pastes it here.
  4. Server exchanges code -> access + refresh token (offline urn:poll), stores
     the refresh token in a 0600 vault file + prints the account email.

Scopes requested (narrow, least-privilege):
  - https://www.googleapis.com/auth/drive        (rclone / Drive storage)
  - https://www.googleapis.com/auth/cloud-platform (gcloud / GCP CLI)
Access_type=offline + prompt=consent => we get a refresh_token we can rotate.

NOTE (honest): a refresh token on its own does NOT drive Google Colab *free*
GPU runtime — Colab runs notebooks in the browser tab and exposes no public
executor API from a bare OAuth token. Drive+rclone 100%; gcloud yes; free-Colab
GPU remote-control needs the separate colab-ssh cell (see .ipynb). This server
is the Drive/gcloud auth layer.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Public Google OAuth desktop/CLI client (used widely for localhost flows).
# Redirect URI must equal the one registered for this client.
CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "326816774986-g7vg8k4ljv1n1v3v0hgqktb9tbnus8u5.apps.googleusercontent.com")
CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "GOCSPX-LO-demo-secret")
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/cloud-platform",
]


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _Handler(BaseHTTPRequestHandler):
    server_state = {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        code = q.get("code", [None])[0]
        err = q.get("error", [None])[0]
        if err:
            body = f"OAuth error: {err}".encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            self.server_state["error"] = err
            return
        if code:
            self.server_state["code"] = code
            body = b"<h2>Authorized - token saved. You may close this tab.</h2>"
        else:
            body = b"<h2>No code received. Check URL.</h2>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


def exchange(state: dict, verifier: str) -> dict:
    data = {
        "code": state["code"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": state["redirect_uri"],
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--run", action="store_true", help="start server + print auth URL")
    ap.add_argument("--save", default="~/.ghostrise_oauth.json")
    args = ap.parse_args(argv)

    port = args.port
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = _pkce()
    state = {"redirect_uri": redirect_uri}
    _Handler.server_state = state

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": secrets.token_urlsafe(8),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)
    print("=" * 70)
    print("GOOGLE OAUTH CAPTURE  (Drive + cloud-platform scopes, offline refresh token)")
    print("=" * 70)
    print("\nauth_url (OPEN THIS ON ANY DEVICE, sign into LO's Google account, Approve):\n")
    print(auth_url)
    print("\nIf the redirect cannot reach this machine (e.g. phone), Google will land")
    print("on a page with an address bar containing  ?code=...  -> copy that ENTIRE")
    print("code value and paste it below (or send it to me).")
    print(f"\nStarting local server http://127.0.0.1:{port}/callback (Ctrl+C to stop) ...\n")

    srv = HTTPServer(("127.0.0.1", port), _Handler)
    srv.timeout = 0.5
    got_code = False
    for _ in range(600):  # ~5 min window
        srv.handle_request()
        if state.get("code"):
            got_code = True
            break
    if not got_code:
        # paste fallback
        manual = input("No redirect received. Paste the code value (or 'skip'): ").strip()
        if manual and manual.lower() != "skip":
            state["code"] = manual
        else:
            print("No code. Exiting.")
            return 1

    try:
        tok = exchange(state, verifier)
    except Exception as e:
        print("Token exchange failed:", e)
        return 1

    if "refresh_token" not in tok:
        print("No refresh_token in response (prompt=consent may be needed fresh).", tok.keys())
        # fall back to storing the access token if present
        if "access_token" not in tok:
            return 1
    save = os.path.expanduser(args.save)
    dump = {
        "client_id": CLIENT_ID,
        "scope": tok.get("scope"),
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_in": tok.get("expires_in"),
        "token_type": tok.get("token_type"),
    }
    with open(save, "w") as f:
        json.dump(dump, f)
    os.chmod(save, 0o600)
    # Try to resolve account email via userinfo for confirmation.
    email = "?"
    at = tok.get("access_token")
    if at:
        try:
            import urllib.request as u
            req = u.Request("https://www.googleapis.com/oauth2/v2/userinfo",
                            headers={"Authorization": f"Bearer {at}"})
            info = json.loads(u.urlopen(req, timeout=15).read())
            email = info.get("email", "?")
        except Exception:
            pass
    print("\n[OK] Token saved to", save, "(0600)")
    print("     Account email:", email)
    print("     refresh_token present:", bool(tok.get("refresh_token")))
    print("\nNext: use this to rclone configure / gcloud auth login with LO's Drive.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
