"""Token storage.

One token represents one channel or account. A token is only refreshable
against the OAuth client that minted it, so the issuing client_id is recorded
alongside it and checked on load.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from ci_publisher.config import state_home


def _tokens_dir() -> Path:
    path = state_home() / "tokens"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def token_path(platform: str) -> Path:
    return _tokens_dir() / f"{platform}.json"


def save_token(platform: str, authorized_user_info: dict, client_id: str, scopes: list[str]) -> Path:
    """Persist a token mode 0600, recording which client issued it."""
    path = token_path(platform)
    payload = {
        "platform": platform,
        "client_id": client_id,
        "scopes": sorted(scopes),
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "token": authorized_user_info,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_token(platform: str) -> dict | None:
    """Return the stored token envelope, or None when the platform is unauthorised."""
    path = token_path(platform)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def forget_token(platform: str) -> bool:
    path = token_path(platform)
    if path.is_file():
        path.unlink()
        return True
    return False
