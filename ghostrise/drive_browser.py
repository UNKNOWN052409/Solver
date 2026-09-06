"""GhostRise Browser Drive storage — Drive = persistent, local = runtime cache.

LO-ARCH: Google Drive is the primary/persistent home for browser heavy data
(profiles, extensions, downloads, binaries/models).  The live browser executes
LOCALLY out of /tmp/gwcache (a throwaway cache), and the browser's default
download dir is Drive-backed via DownloadRedirect.  Everything durable lives on
gdrive:ghostbrowse/... ; nothing important is trusted to the local box.

Storage map (all under remote `gdrive:ghostbrowse/`):
    profiles/<id>/<id>.tar.gz   BrowserProfileStore  (zip of one profile dir)
    downloads/<name>            DownloadRedirect     (browser downloads)
    assets/<name>               HeavyAssetStore      (binaries/models)

This module IMPORTS store.py for the real rclone verbs (drive_upload /
drive_download / drive_exists) and does NOT modify it.  NOTE (found 2026-09-06):
store.drive_list() is broken on rclone v1.75.1 because it passes the removed
`--no-banner` flag, so this module ships its own plain `rclone lsf` list helper
(list_files) instead of relying on store.drive_list.  upload/download/exists are
untouched and verified real.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile

from ghostrise import store

RCLONE = store.RCLONE
DEFAULT_REMOTE = store.DEFAULT_REMOTE
REMOTE_BASE = "ghostbrowse"

CACHE_ROOT = "/tmp/gwcache"
PROFILE_ROOT = os.path.join(CACHE_ROOT, "profiles")   # <browser_id> dirs
ASSET_ROOT = os.path.join(CACHE_ROOT, "assets")       # heavy binaries/models
DOWNLOAD_STAGING = os.path.join(CACHE_ROOT, "staging")  # browser download sink


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([RCLONE, *args], capture_output=True, text=True,
                          check=False)


def list_files(remote_path: str, remote: str = DEFAULT_REMOTE) -> list[str]:
    """Plain `rclone lsf` (NO --no-banner — broken on rclone >=1.71).

    store.drive_list() passes --no-banner, which rclone v1.75.1 rejects
    ("unknown flag").  That breaks the sibling's list; this module provides its
    own working list so uploads can be confirmed.  rclone may print a
    NOTICE/retirement warning on stderr — that is harmless, stdout is trusted.
    """
    p = remote_path.strip().strip("/")
    src = f"{remote}:{p}" if p else f"{remote}:"
    r = _run(["lsf", "--files-only", src])
    if r.returncode != 0:
        raise RuntimeError(f"rclone lsf failed: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


# -------------------------------------------------------------------------- #
# 1) BrowserProfileStore — one profile dir, zip/upload + fetch/unzip
# -------------------------------------------------------------------------- #
class BrowserProfileStore:
    """Persists a browser profile dir to gdrive:ghostbrowse/profiles/<id>/.

    Local profile_dir() lives under /tmp/gwcache/<id>/ (runtime cache).  The
    durable copy is a single <id>.tar.gz blob on Drive.  sync_up() pushes the
    whole dir; sync_down() pulls + unzips it back (skips if not on Drive).
    """

    def __init__(self, browser_id: str, remote: str = DEFAULT_REMOTE):
        self.browser_id = str(browser_id).strip("/")
        if not self.browser_id:
            raise ValueError("browser_id must not be empty")
        self.remote = remote
        self.remote_dir = f"{REMOTE_BASE}/profiles/{self.browser_id}"
        self.blob = f"{self.browser_id}.tar.gz"
        self.remote_blob = f"{self.remote_dir}/{self.blob}"

    def profile_dir(self) -> str:
        """Local cache dir for this browser_id -> /tmp/gwcache/<id>/."""
        d = os.path.join(CACHE_ROOT, self.browser_id)
        os.makedirs(d, exist_ok=True)
        return d

    def sync_up(self) -> str:
        """Zip profile_dir and upload to Drive. Returns remote blob path."""
        d = self.profile_dir()
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, self.blob)
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(d, arcname=self.browser_id)
            if not store.drive_upload(tar_path, self.remote_blob, self.remote):
                raise RuntimeError(
                    f"sync_up verification failed for {self.remote_blob}")
        return self.remote_blob

    def sync_down(self) -> str | None:
        """Download blob from Drive and unzip into profile_dir.

        Returns the populated local profile dir, or None if not on Drive.
        """
        if not store.drive_exists(self.remote_blob, self.remote):
            return None
        d = self.profile_dir()
        with tempfile.TemporaryDirectory() as tmp:
            tar_path = os.path.join(tmp, self.blob)
            if not store.drive_download(self.remote_blob, tar_path, self.remote):
                raise RuntimeError(
                    f"sync_down failed for {self.remote_blob}")
            with tarfile.open(tar_path, "r:gz") as tf:
                members = tf.getmembers()
                if not members:
                    return None
                top = members[0].name.split("/")[0]
                tf.extractall(d)
        return os.path.join(d, top)

    def drive_exists(self) -> bool:
        return store.drive_exists(self.remote_blob, self.remote)


# -------------------------------------------------------------------------- #
# 2) DownloadRedirect — browser sink; Drive keeps it, local file is deleted
# -------------------------------------------------------------------------- #
class DownloadRedirect:
    """Browser downloads land in a staging dir, then get pushed to Drive.

    Drive is persistent; the local staged copy is REMOVED (rm local) once the
    upload is confirmed — the local box is only a throwaway cache.  A future
    stage_download with the same name is pulled back onto Drive if present.
    """

    def __init__(self, remote: str = DEFAULT_REMOTE):
        self.remote = remote
        self.staging_dir = DOWNLOAD_STAGING
        self.remote_dir = f"{REMOTE_BASE}/downloads"
        os.makedirs(self.staging_dir, exist_ok=True)

    def stage_download(self, local_path: str) -> str:
        """Upload local download to Drive, then rm the local copy.

        local_path may be a file (or a dir, which is tarballed). Returns the
        remote path stored.
        """
        if not os.path.exists(local_path):
            raise FileNotFoundError(local_path)
        name = os.path.basename(os.path.abspath(local_path))
        remote_path = f"{self.remote_dir}/{name}"
        if os.path.isdir(local_path):
            # dir: zip into a temp blob, upload that, clean up temp
            with tempfile.TemporaryDirectory() as tmp:
                blob = f"{name}.tar.gz"
                tar_path = os.path.join(tmp, blob)
                with tarfile.open(tar_path, "w:gz") as tf:
                    tf.add(local_path, arcname=name)
                remote_path = f"{self.remote_dir}/{blob}"
                if not store.drive_upload(tar_path, remote_path, self.remote):
                    raise RuntimeError(
                        f"stage_download upload failed for {remote_path}")
        else:
            if not store.drive_upload(local_path, remote_path, self.remote):
                raise RuntimeError(
                    f"stage_download upload failed for {remote_path}")
        # Drive=persistent; local=cleanup
        if os.path.isdir(local_path):
            shutil.rmtree(local_path, ignore_errors=True)
        else:
            os.remove(local_path)
        return remote_path

    def confirm(self, remote_path: str) -> bool:
        """True if the staged download now exists on Drive."""
        return store.drive_exists(remote_path, self.remote)


# -------------------------------------------------------------------------- #
# 3) HeavyAssetStore — binaries/models persist on Drive, execute locally
# -------------------------------------------------------------------------- #
class HeavyAssetStore:
    """Ensures a heavy asset (browser binary, model, extension) is available.

    ensure_asset(name, build_local):
      - if asset already on Drive   -> download into /tmp/gwcache/assets/  (no build)
      - else                         -> build_local(local_path) then upload (+verify)
    The asset PERSISTS on Drive and EXECUTES from the LOCAL cache (/tmp/gwcache/assets/).
    """

    def __init__(self, remote: str = DEFAULT_REMOTE):
        self.remote = remote
        self.local_dir = ASSET_ROOT
        self.remote_dir = f"{REMOTE_BASE}/assets"
        os.makedirs(self.local_dir, exist_ok=True)

    def local_path(self, name: str) -> str:
        return os.path.join(self.local_dir, name)

    def remote_path(self, name: str) -> str:
        return f"{self.remote_dir}/{name}"

    def on_drive(self, name: str) -> bool:
        return store.drive_exists(self.remote_path(name), self.remote)

    def ensure_asset(self, name: str, build_local) -> str:
        """Return a LOCAL path to a ready asset, building/uploading if needed.

        build_local(local_path: str) -> str|None: must materialize the asset at
        local_path (returning local_path) or return None to abort.  It is only
        invoked when the asset is NOT already on Drive.
        """
        name = os.path.basename(name.strip("/"))  # no path traversal
        local = self.local_path(name)
        rp = self.remote_path(name)

        if self.on_drive(name):
            # Drive = source of truth; pull down and run locally.
            if not store.drive_download(rp, local, self.remote):
                raise RuntimeError(f"ensure_asset download failed for {rp}")
            if not os.path.exists(local):
                raise RuntimeError(f"ensure_asset missing after download: {local}")
            return local

        # Not on Drive: build it locally, then persist.
        os.makedirs(self.local_dir, exist_ok=True)
        if build_local is None:
            raise RuntimeError(
                f"ensure_asset({name}): not on Drive and no build_local given")
        built = build_local(local)
        if built is None:
            built = local
        if not os.path.exists(built):
            raise RuntimeError(
                f"ensure_asset({name}): build_local did not produce {built}")
        if not store.drive_upload(built, rp, self.remote):
            raise RuntimeError(f"ensure_asset upload failed for {rp}")
        return built if os.path.exists(built) else local


# -------------------------------------------------------------------------- #
# CLI
# -------------------------------------------------------------------------- #
def _cli(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: python -m ghostrise.drive_browser {--sync-up DIR | --sync-down ID | --self-test}")
        return 2
    cmd = argv[0]
    if cmd == "--self-test":
        return self_test()
    if cmd == "--sync-up":
        if len(argv) < 2:
            print("usage: --sync-up <dir>")
            return 2
        d = argv[1].rstrip("/")
        bid = os.path.basename(os.path.abspath(d))
        p = BrowserProfileStore(bid)
        rp = p.sync_up()
        print(f"synced up {d} -> {p.remote}:{rp}")
        return 0
    if cmd == "--sync-down":
        if len(argv) < 2:
            print("usage: --sync-down <id>")
            return 2
        p = BrowserProfileStore(argv[1])
        out = p.sync_down()
        if out is None:
            print(f"no profile on Drive for {argv[1]}")
            return 1
        print(f"synced down -> {out}")
        return 0
    print(f"unknown cmd: {cmd}")
    return 2


def self_test() -> int:
    """REAL test: tmp profile dir+file -> upload -> list-confirm -> download -> sha256== -> rm local."""
    print("=== drive_browser self-test (real Drive) ===")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # 1) build a fake profile dir WITH REAL content placed in the
            #    actual local cache dir so sync_up actually ships it.
            ps = BrowserProfileStore("selftest")
            pf = ps.profile_dir()  # /tmp/gwcache/selftest
            for old in (os.path.join(pf, "selftest"), os.path.join(pf, "profile.cookie")):
                if os.path.exists(old):
                    (shutil.rmtree(old) if os.path.isdir(old) else os.remove(old))
            blob = os.urandom(4096)
            with open(os.path.join(pf, "profile.cookie"), "wb") as fh:
                fh.write(blob)

            # 2) upload via BrowserProfileStore
            rp = ps.sync_up()
            print(f"sync_up ok -> {ps.remote}:{rp}")

            # 3) list remote to CONFIRM (own list_files, since store.drive_list is broken)
            names = list_files(ps.remote_dir, ps.remote)
            print(f"drive_list({ps.remote}:{ps.remote_dir}) -> {names}")
            if ps.blob not in names:
                print("FAIL: uploaded blob not present in remote listing")
                return 1

            # 4) download back + sha256 match
            restored_root = ps.profile_dir()  # /tmp/gwcache/selftest
            out = ps.sync_down()
            restored = os.path.join(restored_root, "selftest", "profile.cookie")
            if out is None or not os.path.exists(restored):
                print(f"FAIL: sync_down did not restore file (out={out})")
                return 1
            got = open(restored, "rb").read()
            match = hashlib.sha256(got).hexdigest() == hashlib.sha256(blob).hexdigest()
            print(f"sync_down ok -> {out} sha256_match={match}")

            # 5) rm local temp (the in-memory profile dir; Drive copy stays)
            shutil.rmtree(restored_root, ignore_errors=True)
            local_gone = not os.path.exists(restored_root)
            print(f"local temp removed={local_gone} (Drive copy kept at {ps.remote}:{rp})")

            if not (match and local_gone):
                print("FAIL: sha256 or local-cleanup check failed")
                return 1

            # 6) DownloadRedirect spot-check: stage + confirm + local removed
            dl = os.path.join(tmp, "download.bin")
            with open(dl, "wb") as fh:
                fh.write(os.urandom(256))
            dr = DownloadRedirect()
            dl_rp = dr.stage_download(dl)
            confirmed = dr.confirm(dl_rp)
            local_removed = not os.path.exists(dl)
            print(f"DownloadRedirect: remote={dl_rp} confirmed={confirmed} local_removed={local_removed}")

            # 7) HeavyAssetStore: build+upload (not present), then ensure pulls from Drive
            hs = HeavyAssetStore()
            asset = "selftest_asset.bin"
            # force-clean any prior so we exercise the build path
            rp_a = hs.remote_path(asset)
            # rm from local only; Drive may already have it -> use a unique name instead
            asset = "selftest_asset_v2.bin"
            rp_a = hs.remote_path(asset)
            def _build(path):
                with open(path, "wb") as fh:
                    fh.write(os.urandom(1024))
                return path
            lp = hs.ensure_asset(asset, _build)
            on_drive = hs.on_drive(asset)
            loc_exists = os.path.exists(lp)
            print(f"HeavyAssetStore: local={lp} on_drive={on_drive} local_exists={loc_exists}")

            ok = all([confirmed, local_removed, on_drive, loc_exists])
            print("SELF-TEST " + ("PASS" if ok else "FAIL"))
            return 0 if ok else 1
    except Exception as e:  # noqa: BLE001
        print(f"SELF-TEST FAIL (exception): {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(_cli(sys.argv[1:]))
