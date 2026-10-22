"""Configuration helpers for request rate limits."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class RateLimit:
    """One fixed-window quota, keyed by IP, anonymous IP, or verified identity."""

    scope: str
    count: int
    window_seconds: int


_RATE_PATTERN = re.compile(
    r"^\s*(\d+)\s*/\s*(s|sec|second|seconds|m|min|minute|minutes|h|hour|hours|d|day|days)\s*$",
    re.I,
)
_PERIODS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_SCOPES = {"ip", "anonymous", "authenticated"}


def parse_rate(scope: str, value: str) -> RateLimit:
    if not isinstance(scope, str) or scope not in _SCOPES:
        raise ValueError("throttle keys must be 'ip', 'anonymous', or 'authenticated'")
    if not isinstance(value, str):
        raise TypeError("throttle rates must be strings such as '10/minute'")
    match = _RATE_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            f"Invalid throttle rate '{value}'. Use forms such as '10/second', '60/minute', or '1000/day'."
        )
    count = int(match.group(1))
    if count <= 0:
        raise ValueError("throttle rate count must be positive")
    period = _PERIODS[match.group(2).lower()]
    return RateLimit(scope, count, period)


def normalize_throttle(value: Mapping[str, str] | None) -> tuple[RateLimit, ...] | None:
    """Validate configured scope-to-rate mappings; None means inherit."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("throttle must be a mapping, for example {'anonymous': '10/minute'}")
    if not value:
        return ()
    return tuple(parse_rate(scope, rate) for scope, rate in value.items())
