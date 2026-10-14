"""Local quota ledger for the YouTube Data API.

The daily allowance is the binding constraint on this pipeline, not disk or
bandwidth: 10,000 units/day against 1,600 for a single upload, so roughly six
uploads a day. Google charges for a request whether or not it succeeds, so the
ledger debits *before* the call and refuses outright once the day's budget
cannot cover the next operation. Firing a request that is certain to fail
costs the same as one that works.

The quota day resets at midnight Pacific, not UTC and not local time.
"""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ci_publisher.config import state_home
from ci_publisher.errors import QuotaExceeded

QUOTA_TZ = ZoneInfo("America/Los_Angeles")

DAILY_BUDGET = 10_000

#: Documented unit costs, by API method.
COSTS = {
    "videos.insert": 1600,
    "videos.update": 50,
    "thumbnails.set": 50,
    "playlistItems.insert": 50,
    "search.list": 100,
    "channels.list": 1,
    "videos.list": 1,
    "commentThreads.list": 1,
}


def quota_day(now: datetime | None = None) -> str:
    """The current quota day as an ISO date, in Pacific time."""
    moment = now.astimezone(QUOTA_TZ) if now else datetime.now(QUOTA_TZ)
    return moment.date().isoformat()


def cost_of(operation: str) -> int:
    try:
        return COSTS[operation]
    except KeyError:
        raise QuotaExceeded(
            f"Unknown API operation {operation!r}; add its documented unit "
            "cost to ci_publisher.quota.COSTS before calling it."
        ) from None


class QuotaLedger:
    """A per-day tally of units spent, persisted as JSON."""

    def __init__(self, path: Path | None = None, budget: int = DAILY_BUDGET):
        self.path = path or (state_home() / "quota.json")
        self.budget = budget

    def _read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        # Keep only the last 30 quota days so the file stays small but recent
        # history is still readable when reconciling against the Console.
        for platform, days in list(data.items()):
            if isinstance(days, dict) and len(days) > 30:
                keep = sorted(days)[-30:]
                data[platform] = {day: days[day] for day in keep}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def used(self, platform: str = "youtube", day: str | None = None) -> int:
        day = day or quota_day()
        entry = self._read().get(platform, {})
        value = entry.get(day, 0) if isinstance(entry, dict) else 0
        return int(value) if isinstance(value, (int, float)) else 0

    def remaining(self, platform: str = "youtube", day: str | None = None) -> int:
        return max(0, self.budget - self.used(platform, day))

    def can_afford(self, operation: str, platform: str = "youtube") -> bool:
        return cost_of(operation) <= self.remaining(platform)

    def spend(self, operation: str, platform: str = "youtube", times: int = 1) -> int:
        """Debit an operation, or raise QuotaExceeded without debiting.

        Call this immediately before the API request it pays for.
        """
        cost = cost_of(operation) * times
        day = quota_day()
        used = self.used(platform, day)
        remaining = max(0, self.budget - used)

        if cost > remaining:
            raise QuotaExceeded(
                f"{operation} costs {cost} units; only {remaining} of the "
                f"{self.budget}-unit daily budget is left for {platform} on "
                f"{day} (Pacific). The budget resets at midnight Pacific. "
                "Nothing was sent."
            )

        data = self._read()
        days = data.setdefault(platform, {})
        if not isinstance(days, dict):
            days = {}
            data[platform] = days
        days[day] = used + cost
        self._write(data)
        return self.budget - (used + cost)

    def refund(self, operation: str, platform: str = "youtube", times: int = 1) -> None:
        """Credit units back after a request that provably never reached Google.

        Only for local failures (a missing file, a validation error raised
        before the request). A rejection from Google has already been charged.
        """
        cost = cost_of(operation) * times
        day = quota_day()
        data = self._read()
        days = data.setdefault(platform, {})
        if not isinstance(days, dict):
            return
        days[day] = max(0, int(days.get(day, 0)) - cost)
        self._write(data)

    def summary(self, platform: str = "youtube") -> dict:
        day = quota_day()
        used = self.used(platform, day)
        remaining = max(0, self.budget - used)
        return {
            "platform": platform,
            "quota_day": day,
            "timezone": str(QUOTA_TZ),
            "budget": self.budget,
            "used": used,
            "remaining": remaining,
            "uploads_left": remaining // COSTS["videos.insert"],
        }
