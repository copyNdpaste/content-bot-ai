"""Pure scheduling rules for autonomous content publication."""
from __future__ import annotations


def interval_bounds(
    *,
    legacy_hours: str = "",
    min_hours: str = "2.0",
    max_hours: str = "3.0",
    absolute_min_hours: float = 2.0,
    absolute_max_hours: float = 24.0,
) -> tuple[float, float]:
    """Return validated publication interval bounds in hours.

    The lower bound is intentionally clamped to two hours so restarts or bad
    launchd environment values cannot make publication happen every few minutes.
    """
    legacy = (legacy_hours or "").strip()
    if legacy:
        try:
            parsed_min = parsed_max = float(legacy)
        except ValueError:
            parsed_min, parsed_max = 2.0, 3.0
    else:
        try:
            parsed_min = float((min_hours or "2.0").strip())
        except ValueError:
            parsed_min = 2.0
        try:
            parsed_max = float((max_hours or "3.0").strip())
        except ValueError:
            parsed_max = 3.0

    bounded_min = max(absolute_min_hours, min(parsed_min, absolute_max_hours))
    bounded_max = max(bounded_min, min(parsed_max, absolute_max_hours))
    return bounded_min, bounded_max


def seconds_until_min_interval_elapsed(
    *,
    now: float,
    last_started_at: int | float | None,
    min_hours: float,
) -> int:
    """Return seconds to wait before another publication run is allowed."""
    if last_started_at is None:
        return 0
    try:
        last_started = float(last_started_at)
    except (TypeError, ValueError):
        return 0
    remaining = int((last_started + min_hours * 3600) - now)
    return max(0, remaining)

