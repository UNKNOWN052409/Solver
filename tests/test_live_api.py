"""LIVE end-to-end test of the hosted solver API: keys + solve + revoke.

Hits the REAL uvicorn server on 127.0.0.1:8899 over real HTTP sockets.
Requires: server running with SOLVER_API_KEY=master1 SOLVER_ADMIN_KEY=master1
           SOLVER_KEYRING=/tmp/livekeys.db SOLVER_MODEL_DIR=/home/kali/data
Run: pytest tests/test_live_api.py -q
"""

import json
import time
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8899"
ADMIN = {"X-Admin-Key": "master1"}
REAL_IMG = "/home/kali/data/lcsd/4-characters-captcha/images/3d14de03-18bc-4968-b78c-13755169721c.png"
REAL_LABEL = "2L?%"


def call(method, path, headers=None, body=None, timeout=30):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture(scope="module")
def wait_server():
    for _ in range(20):
        try:
            s, _ = call("GET", "/health")
            if s == 200:
                return
        except Exception:
            time.sleep(0.5)
    pytest.fail("solver API not reachable on 127.0.0.1:8899 — start it first")


class TestKeyLifecycle:
    def test_generate_and_use_key(self, wait_server):
        # 1. create a key (admin)
        s, created = call("POST", "/keys", headers={**ADMIN, "Content-Type": "application/json"},
                          body=json.dumps({"label": "live-test", "days": 1}).encode())
        assert s == 200, created
        key = created["key"]
        assert key.startswith("sk-solver-") and len(key) > 20

        # 2. key authorizes a REAL solve on real captcha data
        boundary = "----netkit" + str(time.time())
        img = open(REAL_IMG, "rb").read()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"c.png\"\r\nContent-Type: image/png\r\n\r\n").encode() + img + \
               f"\r\n--{boundary}--\r\n".encode()
        s, solved = call(
            "POST",
            "/solve/image?engine=slot&model=lcsd_slot_model.pt&slot_x0=11&slot_x1=69&slot_n=4",
            headers={"X-API-Key": key, "Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=body)
        assert s == 200, solved
        assert solved["text"] == REAL_LABEL  # 81%-accuracy model, this one is known-good

        # 3. wrong key is rejected
        s, _ = call("POST", "/solve/image64",
                    headers={"X-API-Key": "sk-solver-deadbeef", "Content-Type": "application/json"},
                    body=b'{"image_b64": ""}')
        assert s == 401

        # 4. list shows our key metadata (prefix only, never the raw key)
        s, keys = call("GET", "/keys", headers=ADMIN)
        assert s == 200
        mine = [k for k in keys if k["label"] == "live-test"]
        assert mine and mine[0]["prefix"] == key[:14] and mine[0]["uses"] >= 1

        # 5. revoke -> key stops working
        s, _ = call("DELETE", f"/keys/{key}", headers=ADMIN)
        assert s == 200
        s, _ = call("POST", "/solve/image64",
                    headers={"X-API-Key": key, "Content-Type": "application/json"},
                    body=b'{"image_b64": ""}')
        assert s == 401

    def test_admin_endpoints_reject_non_admin(self, wait_server):
        s, _ = call("GET", "/keys", headers={"X-Admin-Key": "wrong"})
        assert s == 401
        s, _ = call("GET", "/keys")  # no header at all
        assert s == 401


class TestMasterKeyStillWorks:
    def test_master_key_solves(self, wait_server):
        s, h = call("GET", "/health", headers={"X-API-Key": "master1"})
        assert s == 200 and h["auth"] is True
