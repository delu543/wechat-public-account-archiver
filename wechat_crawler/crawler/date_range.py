from __future__ import annotations

from datetime import date, datetime
import re


def normalize_date(value: str | None, field_name: str = "date") -> str | None:
    if value is None or not value.strip():
        return None
    match = re.fullmatch(r"(\d{4})[-_/](\d{1,2})[-_/](\d{1,2})", value.strip())
    if not match:
        raise ValueError(f"{field_name} must be a valid date in YYYY-MM-DD format")
    raw = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date in YYYY-MM-DD format") from exc


def validate_date_range(since: str | None, until: str | None = None) -> tuple[str | None, str | None]:
    normalized_since = normalize_date(since, "since")
    normalized_until = normalize_date(until, "until")
    if normalized_since and normalized_until and normalized_since > normalized_until:
        raise ValueError("since must be on or before until")
    return normalized_since, normalized_until


def extract_publish_date(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"\d{4}[-_/]\d{1,2}[-_/]\d{1,2}", value)
    if not match:
        return None
    parts = re.split(r"[-_/]", match.group(0))
    candidate = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def publish_timestamp_to_date(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def article_in_range(publish_time: str | None, since: str | None, until: str | None = None) -> bool:
    if not since and not until:
        return True
    publish_date = extract_publish_date(publish_time)
    if not publish_date:
        return False
    if since and publish_date < since:
        return False
    if until and publish_date > until:
        return False
    return True
