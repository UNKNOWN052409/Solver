"""GhostRise identity vault - persistent per-profile browser personas."""

import json
import secrets
import time
from pathlib import Path

PROFILES_DIR = Path.home() / ".ghostrise" / "profiles"

DEFAULT_TEMPLATE = {
    "os": "windows",
    "locale": "en-US",
    "screen": [1366, 900],
    "cores": 8,
}


def _path(name: str) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILES_DIR / f"{name}.json"


def create_profile(name: str, os_: str = "windows", locale: str = "en-US",
                   screen=None, cores: int = 8, fp_overrides: dict | None = None):
    """Create a stable identity. The seed guarantees reproducible spoofs."""
    p = _path(name)
    if p.exists():
        raise FileExistsError(f"profile '{name}' already exists")
    entry = {
        "name": name,
        "seed": secrets.token_hex(16),  # future: deterministic fingerprint gen
        "os": os_,
        "locale": locale,
        "screen": screen or DEFAULT_TEMPLATE["screen"],
        "cores": cores,
        "created_at": time.time(),
        "fp_overrides": fp_overrides or {},
    }
    p.write_text(json.dumps(entry, indent=2))
    return entry


def load_profile(name: str) -> dict:
    p = _path(name)
    if not p.exists():
        # Anonymous auto-profile: valid, just not persisted.
        return {"name": name, "os": "windows", "locale": "en-US",
                "screen": [1366, 900], "cores": 8, "fp_overrides": {}}
    return json.loads(p.read_text())


def list_profiles():
    if not PROFILES_DIR.exists():
        return []
    return [json.loads(f.read_text()) for f in sorted(PROFILES_DIR.glob("*.json"))]


def delete_profile(name: str) -> bool:
    p = _path(name)
    if p.exists():
        p.unlink()
        return True
    return False
