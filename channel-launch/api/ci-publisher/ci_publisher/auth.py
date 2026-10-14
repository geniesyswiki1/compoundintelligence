"""OAuth for YouTube, over the loopback redirect.

Scope note worth keeping: Google's TV/limited-input device flow refuses
youtube.force-ssl with invalid_scope, and that scope is what reading comments
requires. A Desktop app client using the loopback redirect is granted it, so
that is the only client type this tool accepts (see config.load_client_config).

Google accepts any localhost port for a Desktop client, so the registered
redirect_uri of http://localhost needs no port and no real handler.
"""

import json
import urllib.parse

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ci_publisher import store
from ci_publisher.config import load_client_config
from ci_publisher.errors import AuthError

SCOPES = {
    "upload": "https://www.googleapis.com/auth/youtube.upload",
    "readonly": "https://www.googleapis.com/auth/youtube.readonly",
    "force-ssl": "https://www.googleapis.com/auth/youtube.force-ssl",
}

#: force-ssl is requested up front: adding a scope later means a fresh consent,
#: and the loopback flow can grant it where the device flow cannot.
DEFAULT_SCOPES = [SCOPES["upload"], SCOPES["readonly"], SCOPES["force-ssl"]]

SUPPORTED_PLATFORMS = ("youtube",)


def _check_platform(platform: str) -> None:
    if platform not in SUPPORTED_PLATFORMS:
        raise AuthError(
            f"Unknown platform {platform!r}. Supported: "
            + ", ".join(SUPPORTED_PLATFORMS)
        )


def authorize(
    platform: str = "youtube",
    scopes: list[str] | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> tuple[Credentials, str]:
    """Run the consent flow and persist the resulting token.

    Returns (credentials, client_id).
    """
    _check_platform(platform)
    scopes = scopes or DEFAULT_SCOPES
    client_config, client_id, path = load_client_config()
    print(f"Using OAuth client from {path}")

    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)

    if open_browser:
        creds = flow.run_local_server(
            port=port,
            open_browser=True,
            prompt="consent",
            access_type="offline",
            authorization_prompt_message=(
                "Opening your browser for Google consent. If it does not open, "
                "visit:\n\n  {url}\n"
            ),
            success_message=(
                "Authorised. You can close this tab and return to the terminal."
            ),
        )
    else:
        creds = _manual_loopback(flow, port or 8765)

    info = json.loads(creds.to_json())
    saved = store.save_token(platform, info, client_id, list(creds.scopes or scopes))
    print(f"Token saved to {saved}")
    return creds, client_id


def _manual_loopback(flow: InstalledAppFlow, port: int) -> Credentials:
    """Consent without a local listener: the user pastes the redirect URL back.

    The browser lands on a dead page at http://localhost:PORT/ carrying the
    code in its query string. Nothing needs to be listening there.
    """
    flow.redirect_uri = f"http://localhost:{port}/"
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    print(
        "Open this URL in any browser signed in to the channel's Google "
        "account:\n\n"
        f"  {auth_url}\n\n"
        "Approve the request. The browser will fail to load a localhost page, "
        "which is expected. Copy that page's full URL from the address bar and "
        "paste it below.\n"
    )
    pasted = input("Redirect URL (or bare code): ").strip()
    code = _extract_code(pasted)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib raises a family of these
        raise AuthError(
            f"Token exchange failed: {exc}\n\n"
            "An authorisation code is single use and expires in minutes. "
            "Re-run the command for a fresh one."
        ) from exc
    return flow.credentials


def _extract_code(pasted: str) -> str:
    """Pull the authorisation code out of a pasted redirect URL.

    Accepts a full URL, a bare query string, or the bare code. A URL carrying
    ?error= must be reported rather than passed on as if it were a code, so
    the shape is decided before looking for the code itself.
    """
    pasted = pasted.strip()
    if not pasted:
        raise AuthError("Nothing pasted, so there is no code to exchange.")

    is_url_like = pasted.startswith(("http://", "https://", "?")) or "=" in pasted
    if not is_url_like:
        return pasted  # the bare code

    query = urllib.parse.urlparse(pasted).query or pasted.lstrip("?")
    params = urllib.parse.parse_qs(query)

    if "error" in params:
        error = params["error"][0]
        hint = ""
        if error == "access_denied":
            hint = (
                "\n\naccess_denied usually means the channel's Google account "
                "is not a test user on the OAuth consent screen while the app "
                "is in Testing. Add it under APIs & Services -> OAuth consent "
                "screen -> Test users, then retry."
            )
        raise AuthError(
            f"Google returned an error instead of a code: {error}.{hint}"
        )

    codes = params.get("code")
    if not codes:
        raise AuthError(
            "No ?code= parameter found in what you pasted. Paste the full URL "
            "from the browser's address bar after approving, or just the code "
            "itself."
        )
    return codes[0]


def _abbreviate(client_id: str, keep: int = 24) -> str:
    """Shorten a client id for display, keeping the distinguishing prefix.

    Every Google client id ends in .apps.googleusercontent.com, so truncating
    from the right would print the one part that is identical for all of them.
    """
    head = client_id.split(".", 1)[0]
    if len(head) <= keep:
        return f"{head}..."
    return f"{head[:keep]}..."


def load_credentials(platform: str = "youtube", required_scope: str | None = None) -> Credentials:
    """Load, validate and if necessary refresh the stored token."""
    _check_platform(platform)
    envelope = store.load_token(platform)
    if not envelope:
        raise AuthError(
            f"No {platform} token stored. Run:\n\n"
            f"  python3 -m ci_publisher auth {platform}\n"
        )

    _, current_client_id, secrets_path = load_client_config()
    issuing_client_id = envelope.get("client_id")
    if issuing_client_id and issuing_client_id != current_client_id:
        raise AuthError(
            "The stored token was issued by a different OAuth client and "
            "cannot be refreshed against this one.\n\n"
            f"  token was issued by: {_abbreviate(issuing_client_id)}\n"
            f"  client secrets now:  {_abbreviate(current_client_id)}\n"
            f"                       ({secrets_path})\n\n"
            f"Either restore the original client secrets file, or re-run "
            f"'auth {platform}' to mint a token against this client."
        )

    if required_scope and required_scope not in (envelope.get("scopes") or []):
        raise AuthError(
            f"The stored {platform} token lacks the {required_scope} scope.\n"
            f"Re-run 'auth {platform}' to consent again with the full scope set."
        )

    try:
        creds = Credentials.from_authorized_user_info(
            envelope["token"], scopes=envelope.get("scopes") or DEFAULT_SCOPES
        )
    except (KeyError, ValueError) as exc:
        raise AuthError(
            f"Stored {platform} token is unreadable ({exc}). Re-run "
            f"'auth {platform}'."
        ) from exc

    if creds.valid:
        return creds

    if not creds.refresh_token:
        raise AuthError(
            f"The stored {platform} token has expired and carries no refresh "
            f"token. Re-run 'auth {platform}'."
        )

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthError(
            f"Refreshing the {platform} token failed: {exc}\n\n"
            "Common causes: the client secret was reset in Google Cloud "
            "Console, consent was revoked for the account, or the OAuth app is "
            "in Testing (refresh tokens expire after 7 days there). Re-run "
            f"'auth {platform}'."
        ) from exc

    store.save_token(
        platform, json.loads(creds.to_json()), current_client_id, list(creds.scopes or [])
    )
    return creds
