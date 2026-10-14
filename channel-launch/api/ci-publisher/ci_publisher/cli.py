"""Command line interface for ci-publisher."""

import argparse
import json
import sys

from ci_publisher import __version__, store, youtube
from ci_publisher.auth import (
    DEFAULT_SCOPES,
    SCOPES,
    authorize,
    begin_manual_auth,
    complete_manual_auth,
    load_credentials,
)
from ci_publisher.config import load_client_config
from ci_publisher.errors import CiPublisherError
from ci_publisher.quota import QuotaLedger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m ci_publisher",
        description="Publishing tools for the Compound Intelligence channels.",
    )
    parser.add_argument("--version", action="version", version=f"ci-publisher {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="run the OAuth consent flow for a platform")
    p_auth.add_argument("platform", nargs="?", default="youtube", choices=["youtube"])
    p_auth.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port (default: an ephemeral one chosen by the OS)",
    )
    auth_mode = p_auth.add_mutually_exclusive_group()
    auth_mode.add_argument(
        "--no-browser",
        action="store_true",
        help="print the consent URL and wait for a pasted redirect URL "
        "instead of opening a browser (for a headless or remote machine)",
    )
    auth_mode.add_argument(
        "--print-url",
        action="store_true",
        help="print the consent URL and exit without waiting, parking the "
        "flow so --code can finish it later or elsewhere",
    )
    auth_mode.add_argument(
        "--code",
        help="finish a flow started by --print-url: pass the redirect URL "
        "the browser landed on, or the bare code",
    )
    p_auth.add_argument(
        "--scope",
        action="append",
        choices=sorted(SCOPES),
        help="limit the scopes requested (default: upload, readonly, force-ssl)",
    )

    p_who = sub.add_parser("whoami", help="show which channel the stored token controls")
    p_who.add_argument("platform", nargs="?", default="youtube", choices=["youtube"])
    p_who.add_argument("--json", action="store_true", help="emit JSON")

    p_doc = sub.add_parser("doctor", help="check each layer of setup, stopping at the first failure")
    p_doc.add_argument("platform", nargs="?", default="youtube", choices=["youtube"])

    p_quota = sub.add_parser("quota", help="show today's quota ledger")
    p_quota.add_argument("platform", nargs="?", default="youtube", choices=["youtube"])
    p_quota.add_argument("--json", action="store_true", help="emit JSON")

    p_up = sub.add_parser("upload", help="upload a video")
    p_up.add_argument("file", help="path to the video file")
    p_up.add_argument("--title", required=True)
    p_up.add_argument("--description", default="")
    p_up.add_argument("--tags", default="", help="comma separated")
    p_up.add_argument("--category-id", default="22", help="YouTube category id (default 22, People & Blogs)")
    p_up.add_argument(
        "--privacy",
        default="private",
        choices=list(youtube.VALID_PRIVACY),
        help="default private, so a test upload cannot go out by accident",
    )
    p_up.add_argument(
        "--publish-at",
        help="ISO-8601 slot to go public, e.g. 2026-10-20T14:30:00Z. Uploads "
        "as private and lets YouTube flip it at that instant.",
    )
    p_up.add_argument(
        "--shift-past-slot",
        action="store_true",
        help="if --publish-at has passed, use the same time tomorrow instead "
        "of failing",
    )
    p_up.add_argument("--thumbnail", help="image to set as the custom thumbnail after upload")
    p_up.add_argument("--made-for-kids", action="store_true")
    p_up.add_argument(
        "--confirm-public",
        action="store_true",
        help="required alongside --privacy public: publishing to a live "
        "channel needs an explicit decision for this specific video",
    )

    p_logout = sub.add_parser("logout", help="delete a stored token")
    p_logout.add_argument("platform", nargs="?", default="youtube", choices=["youtube"])

    return parser


def cmd_auth(args) -> int:
    scopes = [SCOPES[name] for name in args.scope] if args.scope else DEFAULT_SCOPES

    if args.print_url:
        auth_url, secrets_path = begin_manual_auth(
            args.platform, scopes=scopes, port=args.port or 8765
        )
        print(f"Using OAuth client from {secrets_path}")
        print()
        print("Open this URL in a browser signed in to the channel's Google account:")
        print()
        print(f"  {auth_url}")
        print()
        print(
            "Approve the request. The browser will then fail to load a\n"
            "localhost page, which is expected and means it worked. Copy that\n"
            "page's full URL from the address bar and finish with:\n\n"
            f"  python3 -m ci_publisher auth {args.platform} --code '<paste the URL>'\n\n"
            "The code expires within minutes, so do it promptly."
        )
        return 0

    if args.code:
        complete_manual_auth(args.platform, args.code)
    else:
        authorize(
            args.platform,
            scopes=scopes,
            port=args.port,
            open_browser=not args.no_browser,
        )

    identity = youtube.whoami()
    print()
    print(f"Authorised channel: {identity['title']}  ({identity['channel_id']})")
    print("If that is not the channel you meant, run 'logout' and authorise "
          "again with the right Google account.")
    return 0


def cmd_whoami(args) -> int:
    identity = youtube.whoami()
    if args.json:
        print(json.dumps(identity, indent=2))
        return 0
    for key, value in identity.items():
        print(f"{key:>22}: {value}")
    return 0


def cmd_quota(args) -> int:
    summary = QuotaLedger().summary(args.platform)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    print(f"quota day {summary['quota_day']} ({summary['timezone']})")
    print(f"  used      {summary['used']} of {summary['budget']} units")
    print(f"  remaining {summary['remaining']} units")
    print(f"  that is   {summary['uploads_left']} upload(s) left today")
    return 0


def cmd_doctor(args) -> int:
    """Check layers in order and stop at the first failure.

    Everything below a broken layer reports a misleading error, so continuing
    past one wastes time chasing causes that are not there.
    """
    platform = args.platform
    checks = []

    def report(ok: bool, label: str, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        checks.append(ok)
        print(f"[{mark}] {label}")
        if detail:
            for line in detail.splitlines():
                print(f"       {line}")

    try:
        _, client_id, path = load_client_config()
    except CiPublisherError as exc:
        report(False, "OAuth client secrets", str(exc))
        return 1
    report(True, "OAuth client secrets", f"Desktop app client at {path}")

    envelope = store.load_token(platform)
    if not envelope:
        report(
            False,
            f"{platform} token stored",
            f"None found. Run: python3 -m ci_publisher auth {platform}",
        )
        return 1
    report(True, f"{platform} token stored", f"issued {envelope.get('saved_at')}")

    granted = set(envelope.get("scopes") or [])
    missing = [name for name, uri in SCOPES.items() if uri not in granted]
    if missing:
        report(
            False,
            "scopes granted",
            "Missing: " + ", ".join(missing) + f"\nRe-run: auth {platform}",
        )
    else:
        report(True, "scopes granted", "upload, readonly, force-ssl")

    try:
        load_credentials(platform)
    except CiPublisherError as exc:
        report(False, "token usable (refresh if needed)", str(exc))
        return 1
    report(True, "token usable (refresh if needed)")

    try:
        identity = youtube.whoami()
    except CiPublisherError as exc:
        report(False, "channel reachable", str(exc))
        return 1
    report(
        True,
        "channel reachable",
        f"{identity['title']} ({identity['channel_id']})",
    )

    summary = QuotaLedger().summary(platform)
    enough = summary["uploads_left"] >= 1
    report(
        enough,
        "quota available",
        f"{summary['remaining']} of {summary['budget']} units left on "
        f"{summary['quota_day']} ({summary['uploads_left']} upload(s))",
    )

    print()
    if all(checks):
        print("All layers healthy.")
        return 0
    print("Some layers need attention; see the FAIL lines above.")
    return 1


def cmd_upload(args) -> int:
    if args.privacy == "public" and not args.confirm_public:
        print(
            "Refusing to publish publicly without --confirm-public.\n\n"
            "Publishing to a live channel is not reversible in the way a "
            "private upload is. Either pass --confirm-public for this "
            "specific video, or upload as private (the default) and review it "
            "on YouTube first. To go out on a schedule, use --publish-at.",
            file=sys.stderr,
        )
        return 2

    publish_at = None
    if args.publish_at:
        publish_at = youtube.parse_publish_at(
            args.publish_at, allow_shift=args.shift_past_slot
        )

    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    ledger = QuotaLedger()

    result = youtube.upload(
        args.file,
        title=args.title,
        description=args.description,
        tags=tags,
        category_id=args.category_id,
        privacy=args.privacy,
        publish_at=publish_at,
        made_for_kids=args.made_for_kids,
        ledger=ledger,
    )

    print()
    print(f"Uploaded: {result['url']}")
    print(f"  privacy: {result['privacy_status']}")
    if result.get("publish_at"):
        print(f"  goes public at: {result['publish_at']}")

    exit_code = 0
    if args.thumbnail:
        try:
            youtube.set_thumbnail(result["video_id"], args.thumbnail, ledger=ledger)
            print("  thumbnail: set")
        except CiPublisherError as exc:
            # The video is up; a thumbnail failure must not read as a lost upload.
            print(f"  thumbnail: FAILED\n{exc}", file=sys.stderr)
            exit_code = 1

    summary = ledger.summary("youtube")
    print(
        f"  quota: {summary['remaining']} units left today "
        f"({summary['uploads_left']} upload(s))"
    )
    return exit_code


def cmd_logout(args) -> int:
    if store.forget_token(args.platform):
        print(f"Deleted the stored {args.platform} token.")
    else:
        print(f"No {args.platform} token was stored.")
    return 0


HANDLERS = {
    "auth": cmd_auth,
    "whoami": cmd_whoami,
    "doctor": cmd_doctor,
    "quota": cmd_quota,
    "upload": cmd_upload,
    "logout": cmd_logout,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return HANDLERS[args.command](args)
    except CiPublisherError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
