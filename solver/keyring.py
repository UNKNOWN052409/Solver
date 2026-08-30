"""API key management: generate, store, revoke solver keys (SQLite).

Keys look like: sk-solver-<16hex>  (e.g. sk-solver-3fa9c2e01b8d4f7a)
Every key carries a label, creation time, optional expiry, and usage counters.

    from solver.keyring import Keyring
    kr = Keyring("~/.solver/keys.db")
    key = kr.create(label="lo-laptop", days=30)      # -> "sk-solver-..."
    kr.verify("sk-solver-...")                       # -> True/False
    kr.revoke("sk-solver-...")                       # -> gone
    kr.list()                                         # -> metadata rows

Server integration: keys are checked in solver.server.require_key
(SOLVER_API_KEY env takes precedence as a master key).
"""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

PREFIX = "sk-solver-"


class Keyring:
    def __init__(self, db_path: str = "~/.solver/keys.db"):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS keys (
                    key_hash TEXT PRIMARY KEY,
                    prefix   TEXT NOT NULL,
                    label    TEXT NOT NULL DEFAULT '',
                    created  REAL NOT NULL,
                    expires  REAL,
                    revoked  INTEGER NOT NULL DEFAULT 0,
                    uses     INTEGER NOT NULL DEFAULT 0,
                    last_use REAL
                )"""
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _hash(key: str) -> str:
        # store SHA-256, never the raw key — DB leak != key leak
        return hashlib.sha256(key.encode()).hexdigest()

    def create(self, label: str = "", days: int | None = None) -> dict:
        key = PREFIX + secrets.token_hex(16)
        now = time.time()
        expires = now + days * 86400 if days else None
        with self._conn() as c:
            c.execute(
                "INSERT INTO keys (key_hash, prefix, label, created, expires) VALUES (?,?,?,?,?)",
                (self._hash(key), key[:14], label, now, expires),
            )
        return {"key": key, "label": label, "created": now,
                "expires": expires, "note": "store this now — it is not recoverable later"}

    def verify(self, key: str) -> bool:
        """True if key exists, is unrevoked, unexpired. Bumps usage counters."""
        h = self._hash(key)
        with self._conn() as c:
            row = c.execute(
                "SELECT revoked, expires FROM keys WHERE key_hash = ?", (h,)
            ).fetchone()
            if not row or row[0]:
                return False
            if row[1] and time.time() > row[1]:
                return False
            c.execute(
                "UPDATE keys SET uses = uses + 1, last_use = ? WHERE key_hash = ?",
                (time.time(), h),
            )
        return True

    def revoke(self, key: str) -> bool:
        h = self._hash(key)
        with self._conn() as c:
            cur = c.execute(
                "UPDATE keys SET revoked = 1 WHERE key_hash = ?", (h,)
            )
        return cur.rowcount > 0

    def list(self) -> list[dict]:
        """Metadata for every key (prefix only — raw keys never leave the DB)."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT prefix, label, created, expires, revoked, uses, last_use
                   FROM keys ORDER BY created DESC"""
            ).fetchall()
        return [
            {"prefix": r[0], "label": r[1], "created": r[2],
             "expires": r[3], "revoked": bool(r[4]), "uses": r[5], "last_use": r[6]}
            for r in rows
        ]


def default_keyring() -> Keyring:
    env = os.environ.get("SOLVER_KEYRING", "")
    return Keyring(env if env else "~/.solver/keys.db")
