"""YouTube Data API v3 operations.

Every call that costs quota debits the ledger first, so a day whose budget is
spent fails locally instead of burning units on a request Google will refuse.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ci_publisher.auth import SCOPES, load_credentials
from ci_publisher.errors import ScheduleError, UploadError
from ci_publisher.quota import QuotaLedger

#: Resumable upload chunk size. Large single-shot bodies fail behind some
#: proxies with SSL record errors; 4MB chunks retry cheaply.
CHUNK_SIZE = 4 * 1024 * 1024

VALID_PRIVACY = ("private", "unlisted", "public")


def service(platform: str = "youtube", required_scope: str | None = None):
    creds = load_credentials(platform, required_scope=required_scope)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def whoami(ledger: QuotaLedger | None = None) -> dict:
    """Return the channel this token controls.

    One token is one channel. Confirm the identity before trusting a token:
    the credential itself says nothing about which channel it reaches.
    """
    ledger = ledger or QuotaLedger()
    api = service(required_scope=SCOPES["readonly"])
    ledger.spend("channels.list")
    response = api.channels().list(part="snippet,status,contentDetails", mine=True).execute()

    items = response.get("items") or []
    if not items:
        raise UploadError(
            "The token is valid but reaches no channel. This happens when the "
            "Google account has no YouTube channel created yet, or when "
            "consent was given by the wrong account. Create the channel at "
            "youtube.com, then re-run auth."
        )

    channel = items[0]
    snippet = channel.get("snippet", {})
    status = channel.get("status", {})
    return {
        "channel_id": channel.get("id"),
        "title": snippet.get("title"),
        "custom_url": snippet.get("customUrl"),
        "published_at": snippet.get("publishedAt"),
        "privacy_status": status.get("privacyStatus"),
        "is_linked": status.get("isLinked"),
        "long_uploads_allowed": status.get("longUploadsStatus"),
    }


def parse_publish_at(value: str, allow_shift: bool = False) -> str:
    """Validate a publishAt slot and return it as ISO-8601 UTC.

    A slot in the past is refused rather than silently published now: the
    whole point of scheduling is that a backlog does not land at once. With
    allow_shift the same clock time on the next day is used instead.
    """
    raw = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ScheduleError(
            f"Could not read {value!r} as a timestamp. Use ISO-8601, for "
            "example 2026-10-20T14:30:00Z or 2026-10-20T14:30:00+01:00."
        ) from exc

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    if moment <= now:
        if not allow_shift:
            raise ScheduleError(
                f"{moment.isoformat()} is in the past. YouTube would publish "
                "the video immediately, which defeats scheduling and dumps a "
                "backlog at once. Pick a future slot, or pass "
                "--shift-past-slot to move it to the same time tomorrow."
            )
        while moment <= now:
            moment += timedelta(days=1)

    return moment.isoformat().replace("+00:00", "Z")


def upload(
    path: str | Path,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    category_id: str = "22",
    privacy: str = "private",
    publish_at: str | None = None,
    made_for_kids: bool = False,
    ledger: QuotaLedger | None = None,
) -> dict:
    """Upload a video. Defaults to private; see the CLI for the public guard.

    Scheduling works by uploading as private with status.publishAt set.
    YouTube flips it public at that instant with nothing running locally.
    """
    media_path = Path(path).expanduser()
    if not media_path.is_file():
        raise UploadError(f"No such file: {media_path}")
    if privacy not in VALID_PRIVACY:
        raise UploadError(
            f"privacy must be one of {', '.join(VALID_PRIVACY)}, got {privacy!r}"
        )

    status: dict = {
        "selfDeclaredMadeForKids": made_for_kids,
    }
    if publish_at:
        # publishAt is only honoured on a private video.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at
    else:
        status["privacyStatus"] = privacy

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": status,
    }

    ledger = ledger or QuotaLedger()
    api = service(required_scope=SCOPES["upload"])
    ledger.spend("videos.insert")

    media = MediaFileUpload(
        str(media_path), chunksize=CHUNK_SIZE, resumable=True, mimetype="video/*"
    )
    request = api.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    last_progress = -1
    try:
        while response is None:
            progress, response = request.next_chunk(num_retries=5)
            if progress and int(progress.progress() * 100) != last_progress:
                last_progress = int(progress.progress() * 100)
                print(f"  uploading... {last_progress}%", flush=True)
    except HttpError as exc:
        raise UploadError(_explain_http_error(exc, "upload")) from exc

    return {
        "video_id": response.get("id"),
        "url": f"https://www.youtube.com/watch?v={response.get('id')}",
        "privacy_status": response.get("status", {}).get("privacyStatus"),
        "publish_at": response.get("status", {}).get("publishAt"),
        "title": response.get("snippet", {}).get("title"),
    }


def set_thumbnail(
    video_id: str, path: str | Path, ledger: QuotaLedger | None = None
) -> dict:
    """Set a custom thumbnail. Requires a phone-verified channel."""
    image_path = Path(path).expanduser()
    if not image_path.is_file():
        raise UploadError(f"No such thumbnail file: {image_path}")

    ledger = ledger or QuotaLedger()
    api = service(required_scope=SCOPES["upload"])
    ledger.spend("thumbnails.set")

    media = MediaFileUpload(str(image_path), resumable=False)
    try:
        response = api.thumbnails().set(videoId=video_id, media_body=media).execute()
    except HttpError as exc:
        raise UploadError(_explain_http_error(exc, "thumbnail")) from exc
    return {"video_id": video_id, "thumbnails": response.get("items", [])}


def _explain_http_error(exc: HttpError, operation: str) -> str:
    """Translate the API's misleading errors into the actual cause."""
    status = getattr(exc.resp, "status", None)
    detail = getattr(exc, "reason", None) or str(exc)
    text = str(exc)

    if operation == "thumbnail" and status == 403:
        return (
            f"403 on thumbnails.set: {detail}\n\n"
            "This reads like a scope problem but it is an account problem: "
            "custom thumbnails require a phone-verified channel. Verify at "
            "https://www.youtube.com/verify, then retry. The upload itself "
            "succeeded; only the thumbnail was rejected."
        )

    if status == 403 and "quotaExceeded" in text:
        return (
            "403 quotaExceeded: Google says the daily allowance is spent even "
            "though the local ledger allowed this call. The ledger has drifted "
            "from reality, usually because requests were made outside this "
            "tool. Check Google Cloud Console -> APIs & Services -> YouTube "
            "Data API v3 -> Quotas, and reconcile with 'ci_publisher quota'."
        )

    if status == 403 and "forbidden" in text.lower() and operation == "upload":
        return (
            f"403 on videos.insert: {detail}\n\n"
            "Usually the channel is not eligible to upload (suspended, or "
            "never created), or consent was granted by an account that does "
            "not control the target channel. Run 'ci_publisher whoami' to see "
            "which channel this token actually reaches."
        )

    if status == 400 and "uploadLimitExceeded" in text:
        return (
            "400 uploadLimitExceeded: the channel has hit YouTube's own daily "
            "upload cap, which is separate from the API quota. It clears after "
            "24 hours; nothing to fix in code."
        )

    return f"{status or 'HTTP'} error on {operation}: {detail}"
