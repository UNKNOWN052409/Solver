"""GhostRise Drive storage — rclone direct upload/download, NO FUSE.

rclone can mount a remote (needs /dev/fuse) *and* it can also just copy
files to/from a remote directly (no fuse required).  Proot boxes / tabs
rarely expose /dev/fuse, so this module deliberately uses only the
fuse-free transfer verbs:

    rclone copy  LOCAL   gdrive:path/file   # upload
    rclone copy  gdrive:path/file  LOCAL    # download
    rclone lsf   gdrive:path                # list
    rclone lsl   gdrive:path/file           # stat/exists

A run's config+cookies+cache is zipped into one .tar.gz and pushed to a
flat path in Drive (store_session_dir), and pulled back + unzipped by
fetch_session_dir — so a session survives a box change or a tab reset.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile

RCLONE = shutil.which("rclone") or "/usr/local/bin/rclone"
DEFAULT_REMOTE = "gdrive"


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run rclone, never directly expose a shell-injectable string."""
    return subprocess.run(
        [RCLONE, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _remote_path(remote: str, path: str) -> str:
    p = path.strip().strip("/")
    return f"{remote}:{p}" if p else f"{remote}:"


def drive_list(remote: str = DEFAULT_REMOTE, path: str = "") -> list[str]:
    """List files under gdrive:path (single :remote listing)."""
    r = _run(["--no-banner", "lsf", "--files-only",
              _remote_path(remote, path)])
    if r.returncode != 0:
        raise RuntimeError(f"rclone lsf failed: {r.stderr.strip()}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def drive_exists(remote_path: str, remote: str = DEFAULT_REMOTE,
                 pattern: str = "") -> bool:
    """True if remote_path exists (exact file, or pattern matches)."""
    if not pattern:
        pattern = os.path.basename(remote_path.rstrip("/"))
    parent = os.path.dirname(remote_path.rstrip("/"))
    r = _run(["lsf", "--files-only", _remote_path(remote, parent)])
    if r.returncode != 0:
        return False
    return any(ln.strip() == pattern for ln in r.stdout.splitlines())


def drive_upload(local_path: str, remote_path: str,
                 remote: str = DEFAULT_REMOTE) -> bool:
    """Upload local_path -> gdrive:remote_path (direct copy, no mount)."""
    if not os.path.exists(local_path):
        raise FileNotFoundError(local_path)
    dest_dir = os.path.dirname(remote_path.rstrip("/"))
    r = _run(["copy", local_path, _remote_path(remote, dest_dir)], check=False)
    if r.returncode != 0:
        raise RuntimeError(f"rclone copy up failed: {r.stderr.strip()}")
    return drive_exists(remote_path, remote)


def drive_download(remote_path: str, local_path: str,
                   remote: str = DEFAULT_REMOTE) -> bool:
    """Download gdrive:remote_path -> local_path (direct copy, no mount)."""
    os.makedirs(os.path.dirname(os.path.abspath(local_path)) or ".", exist_ok=True)
    src_dir = os.path.dirname(remote_path.rstrip("/")) or ""
    base = os.path.basename(remote_path.rstrip("/"))
    # rclone `copy <remote>:<dir> <local_dir>` writes the file under its
    # ORIGINAL basename — so stage into a temp dir and rename to local_path
    with tempfile.TemporaryDirectory() as tmp:
        r = _run(["copy", _remote_path(remote, src_dir), tmp], check=False)
        if r.returncode != 0:
            raise RuntimeError(f"rclone copy down failed: {r.stderr.strip()}")
        staged = os.path.join(tmp, base)
        if not os.path.exists(staged):
            return False
        shutil.move(staged, local_path)
    return os.path.exists(local_path)


# ------------------------------------------------------------ session dir ---
DEFAULT_EXTS = (".json", ".cookie", ".cookies", ".pkl", ".npz")


def store_session_dir(session_dir: str,
                      remote_path: str = "ghostrise/sessions",
                      remote: str = DEFAULT_REMOTE) -> str:
    """Zip a run's config+cookies+cache and upload to Drive.

    Returns the uploaded blob name (remote dir + '<basename>.tar.gz').
    """
    if not os.path.isdir(session_dir):
        raise NotADirectoryError(session_dir)
    base = os.path.basename(os.path.abspath(session_dir.rstrip("/")))
    blob = f"{base}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, blob)
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(session_dir, arcname=base)
        dst = f"{remote_path.rstrip('/')}/{blob}"
        if not drive_upload(tar_path, dst, remote):
            raise RuntimeError(f"upload verification failed for {dst}")
    return dst


def fetch_session_dir(dst_dir: str,
                      remote_path: str = "ghostrise/sessions",
                      remote: str = DEFAULT_REMOTE,
                      blob: str | None = None) -> str | None:
    """Download + unzip a session blob from Drive into dst_dir.

    When `blob` is None, picks the newest '<name>.tar.gz' in remote_path.
    Returns the extracted directory path, or None if nothing to fetch.
    """
    os.makedirs(dst_dir, exist_ok=True)
    path = remote_path.rstrip("/")
    if blob is None:
        files = [
            ln for ln in drive_list(remote, path)
            if ln.endswith(".tar.gz")
        ]
        if not files:
            return None
        blob = sorted(files)[-1]
    with tempfile.TemporaryDirectory() as tmp:
        tar_path = os.path.join(tmp, blob)
        if not drive_download(f"{path}/{blob}", tar_path, remote):
            return None
        with tarfile.open(tar_path, "r:gz") as tf:
            members = tf.getmembers()
            if not members:
                return None
            top = members[0].name.split("/")[0]
            tf.extractall(dst_dir)
    return os.path.join(dst_dir, top)


def _self_test() -> int:
    """Upload a temp file, download it back, checksum-match. Real proof."""
    import hashlib

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "probe.bin")
        with open(src, "wb") as fh:
            fh.write(os.urandom(4096))  # non-trivial random content
        up_ok = drive_upload(src, "ghostrise/selftest/probe.bin")
        got = os.path.join(tmp, "probe.dl")
        down_ok = drive_download("ghostrise/selftest/probe.bin", got)
        if not (up_ok and down_ok):
            print("FAIL: upload/download did not succeed")
            return 1
        a = hashlib.sha256(open(src, "rb").read()).hexdigest()
        b = hashlib.sha256(open(got, "rb").read()).hexdigest()
        match = a == b
        print(f"upload={up_ok} download={down_ok} sha256_match={match}")
        return 0 if match else 1


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_self_test())
    # quick health check
    try:
        print("remote root entries:", drive_list(DEFAULT_REMOTE, ""))
    except Exception as e:  # noqa: BLE001
        print("rclone not usable:", e)
        sys.exit(1)
