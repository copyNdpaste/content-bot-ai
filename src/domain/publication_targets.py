"""Pure rules for publication target selection."""
from __future__ import annotations


DEFAULT_PLATFORMS = ["threads", "instagram", "x"]
DEFAULT_ACCOUNTS = ["jp", "kr"]
DEFAULT_IMAGE_PLATFORMS = ["instagram", "threads", "x"]


def parse_csv_list(value: str, *, default: list[str] | None = None) -> list[str]:
    """Return a normalized comma-separated list while preserving order."""
    raw = (value or "").strip()
    if not raw and default is not None:
        return list(default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def expand_targets(value: str, env_value: str, default: list[str]) -> list[str]:
    """Expand `all` to configured targets, otherwise parse the explicit value."""
    if (value or "").strip().lower() == "all":
        return parse_csv_list(env_value, default=default)
    return parse_csv_list(value)


def disabled_target_set(value: str) -> set[tuple[str, str]]:
    """Parse ROUTINE_DISABLED_TARGETS style `platform:account` entries."""
    disabled: set[tuple[str, str]] = set()
    for item in parse_csv_list(value):
        if ":" not in item:
            continue
        platform, account = [part.strip().lower() for part in item.split(":", 1)]
        if platform and account:
            disabled.add((platform, account))
    return disabled


def is_target_disabled(platform: str, account: str, disabled: set[tuple[str, str]]) -> bool:
    """Return whether a platform/account target is disabled."""
    return ((platform or "").lower(), (account or "").lower()) in disabled


def filter_disabled_targets(
    platforms: list[str],
    account: str,
    disabled: set[tuple[str, str]],
) -> list[str]:
    """Drop disabled platforms for one account."""
    return [
        platform for platform in platforms
        if not is_target_disabled(platform, account, disabled)
    ]


def image_enabled_for(platform: str, value: str) -> bool:
    """Return whether images should be generated for a platform."""
    enabled = set(parse_csv_list(value, default=DEFAULT_IMAGE_PLATFORMS))
    return "all" in enabled or (platform or "").lower() in enabled
