"""Locating the OAuth client secrets and the local state directory.

Secrets live on disk beside the code or in the state directory; they are
never committed and never printed. See the repository .gitignore.
"""

import json
import os
from pathlib import Path

from ci_publisher.errors import ConfigError

#: Env var pointing straight at a client secrets JSON file.
CLIENT_SECRETS_ENV = "CI_PUBLISHER_CLIENT_SECRETS"

#: Env var overriding where tokens and the quota ledger are kept.
HOME_ENV = "CI_PUBLISHER_HOME"

#: Filenames tried, in order, when the env var is unset.
CLIENT_SECRETS_NAMES = (
    "client_secret.json",
    "credentials.json",
)


def state_home() -> Path:
    """Directory holding tokens and the quota ledger, created mode 0700."""
    raw = os.environ.get(HOME_ENV)
    path = Path(raw).expanduser() if raw else Path.home() / ".ci-publisher"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        # Some filesystems (mounted volumes, Windows) refuse chmod. The
        # directory is still usable; permissions are best effort.
        pass
    return path


def _candidate_paths() -> list[Path]:
    env = os.environ.get(CLIENT_SECRETS_ENV)
    if env:
        return [Path(env).expanduser()]

    roots = [Path.cwd(), Path(__file__).resolve().parent.parent, state_home()]
    candidates = [root / name for root in roots for name in CLIENT_SECRETS_NAMES]

    # Also accept the file under the name Google Cloud Console gives it, so a
    # straight move out of ~/Downloads works without renaming.
    for root in roots:
        candidates.extend(sorted(root.glob("client_secret_*.apps.googleusercontent.com.json")))
    return candidates


def find_client_secrets() -> Path:
    """Return the first client secrets file found, or raise ConfigError."""
    candidates = _candidate_paths()
    for path in candidates:
        if path.is_file():
            return path

    looked = "\n  ".join(str(p) for p in candidates)
    raise ConfigError(
        "No OAuth client secrets file found. Looked for:\n  "
        + looked
        + "\n\nDownload a Desktop app client from Google Cloud Console "
        "(APIs & Services -> Credentials -> Create credentials -> OAuth client "
        "ID -> Desktop app), then save it as client_secret.json beside this "
        f"package, or set {CLIENT_SECRETS_ENV} to its path."
    )


def load_client_config(path: Path | None = None) -> tuple[dict, str, Path]:
    """Load and validate a client secrets file.

    Returns (client_config, client_id, path). Raises ConfigError if the file
    is not a Desktop app ("installed") client, because the TV/limited-input
    device flow cannot grant the youtube.force-ssl scope that comment reading
    needs, and a Web client cannot use the loopback redirect at all.
    """
    path = path or find_client_secrets()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Could not read client secrets at {path}: {exc}") from exc

    if "installed" not in raw:
        kind = ", ".join(sorted(raw)) or "nothing recognisable"
        raise ConfigError(
            f"{path} is not a Desktop app client (its top-level key is {kind}, "
            "expected 'installed').\n\n"
            "Create a new OAuth client of type 'Desktop app'. A Web client "
            "cannot use the loopback redirect this tool needs, and a TV/"
            "limited-input client is refused the youtube.force-ssl scope "
            "required to read comments."
        )

    client_id = raw["installed"].get("client_id")
    if not client_id:
        raise ConfigError(f"{path} has no client_id under 'installed'.")

    return raw, client_id, path
