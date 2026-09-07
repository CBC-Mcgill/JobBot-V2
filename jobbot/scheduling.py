"""Pure per-board scheduling policy; all intervals are in seconds."""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import MAX_PUBLICATION_AGE, Company

# A board is only guaranteed to see a listing if it is re-checked while that listing is
# still fresh, so no tier may outlast the freshness window.  Half the window is the
# ceiling; the shipped ladder stops a day below it, which is the slack that absorbs a
# late or skipped scan.  A tier set exactly at the ceiling has no such slack.  Deriving
# the ceiling from MAX_PUBLICATION_AGE keeps the two policies from drifting apart again.
MAX_QUIET_INTERVAL = int(MAX_PUBLICATION_AGE.total_seconds()) // 2
DEFAULT_TIERS = (1800, 7200, 28800, 86400, 259200)
MAX_PRIORITY_INTERVAL = 7200
MAX_FAILURE_RETRY = 86400


def validate_board_key(key: str):
    parts = key.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] not in {"ashby", "greenhouse", "lever", "jsonld"}
        or parts[1] not in {"global", "eu"}
        or not parts[2]
        or any(c.isspace() for c in key)
        or (parts[0] != "jsonld" and not re.fullmatch(r"[A-Za-z0-9_.-]+", parts[2]))
    ):
        raise ValueError(f"Invalid PRIORITY_BOARDS key: {key!r}; use provider:region:board")


def parse_priority_boards(value: str, default_interval: int) -> dict[str, int]:
    """Comma-separated board keys, optionally followed by =interval_seconds."""
    result = {}
    if not value.strip():
        return result
    for entry in value.split(","):
        key, separator, raw_interval = entry.strip().partition("=")
        validate_board_key(key)
        try:
            interval = int(raw_interval) if separator else default_interval
        except ValueError as exc:
            raise ValueError("PRIORITY_BOARDS intervals must be positive integers") from exc
        if not 0 < interval <= MAX_PRIORITY_INTERVAL:
            raise ValueError("PRIORITY_BOARDS intervals must be between 1 and 7200 seconds")
        if key in result:
            raise ValueError(f"Duplicate PRIORITY_BOARDS key: {key}")
        result[key] = interval
    return result


def after(timestamp: str, seconds: int) -> str:
    return (datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)).isoformat()


def bounded_tiers(tiers: tuple[int, ...]) -> tuple[int, ...]:
    """Drop tiers that would sleep past the freshness window, keeping at least one.

    The surviving ladder is never empty and never exceeds the ceiling: when every
    configured tier is too generous the shortest one is lowered to the ceiling rather
    than kept as-is, which would leave the board asleep past its own listings.
    """
    kept = tuple(delay for delay in tiers if delay <= MAX_QUIET_INTERVAL)
    return kept or (min(tiers[0], MAX_QUIET_INTERVAL),)


@dataclass(frozen=True)
class SchedulingPolicy:
    tiers: tuple[int, ...] = DEFAULT_TIERS
    priority_interval: int = 7200
    priority_boards: dict[str, int] = field(default_factory=dict)
    failure_retry: int = 1800

    def __post_init__(self):
        if (
            not self.tiers
            or any(type(t) is not int or t <= 0 for t in self.tiers)
            or any(a >= b for a, b in zip(self.tiers, self.tiers[1:], strict=False))
        ):
            raise ValueError("QUIET_TIERS_SECONDS must be strictly increasing positive integers")
        # Enforced rather than validated: an unattended bot should keep scanning on a safe
        # cadence instead of refusing to start over a too-generous interval.
        object.__setattr__(self, "tiers", bounded_tiers(self.tiers))
        if type(self.priority_interval) is not int or not 0 < self.priority_interval <= 7200:
            raise ValueError("PRIORITY_SCAN_INTERVAL_SECONDS must be between 1 and 7200")
        if type(self.failure_retry) is not int or self.failure_retry <= 0:
            raise ValueError("FAILURE_RETRY_SECONDS must be a positive integer")
        for key, interval in self.priority_boards.items():
            validate_board_key(key)
            if type(interval) is not int or not 0 < interval <= MAX_PRIORITY_INTERVAL:
                raise ValueError("PRIORITY_BOARDS intervals must be between 1 and 7200 seconds")

    def priority(self, company: Company) -> int:
        if company.key in self.priority_boards:
            return self.priority_boards[company.key]
        # Resolve real identifiers from the registry instead of inventing provider endpoints.
        if {company.name.casefold(), company.board.casefold()} & {"nvidia", "amazon"}:
            return self.priority_interval
        return 0

    def cap_tier(self, tier: int, priority: int = 0) -> int:
        maximum = len(self.tiers) - 1
        if priority:
            maximum = max((i for i, delay in enumerate(self.tiers) if delay <= priority), default=0)
        return min(max(0, tier), maximum)

    def success(self, tier: int, qualifying: bool, priority: int = 0) -> tuple[int, int]:
        tier = self.cap_tier(0 if qualifying else tier + 1, priority)
        delay = min(self.tiers[tier], priority) if priority else self.tiers[tier]
        return tier, delay

    def retry(self, failures: int) -> int:
        # The first failed scan retries in 30 minutes by default, then backs off to one day.
        return min(
            self.failure_retry * 2 ** min(max(failures - 1, 0), 16),
            max(self.failure_retry, MAX_FAILURE_RETRY),
        )
