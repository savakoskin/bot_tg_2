from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import AstroEvent


def parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def event_id(title: str, start_at_utc: datetime) -> str:
    raw = f"{title}|{start_at_utc.astimezone(timezone.utc).isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def load_seed_events(path: Path, tz: ZoneInfo) -> list[AstroEvent]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    events: list[AstroEvent] = []
    for item in data:
        try:
            start_at_utc = parse_datetime(item["start_at"], tz)
            title = str(item["title"]).strip()
            events.append(
                AstroEvent(
                    id=item.get("id") or event_id(title, start_at_utc),
                    title=title,
                    start_at_utc=start_at_utc,
                    category=str(item.get("category") or "phenomenon"),
                    description=str(item.get("description") or ""),
                    source_url=item.get("source_url"),
                    livestream_url=item.get("livestream_url"),
                )
            )
        except Exception:
            continue
    return events


def human_delta(target_utc: datetime, now_utc: datetime | None = None) -> str:
    now_utc = now_utc or datetime.now(timezone.utc)
    delta = target_utc - now_utc
    total_seconds = int(delta.total_seconds())

    if total_seconds < 0:
        return "уже началось"

    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes and not days:
        parts.append(f"{minutes} мин.")
    if not parts:
        parts.append("меньше минуты")
    return " ".join(parts)


def _payload_after_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def parse_add_event_command(text: str, tz: ZoneInfo) -> AstroEvent:
    # New format:
    # /addevent 2026-08-12 20:00 | Title | category | Description | source_url | livestream_url
    # Old format is still supported:
    # /addevent 2026-08-12 20:00 | Title | category | Description | livestream_url
    payload = _payload_after_command(text)
    parts = [part.strip() for part in payload.split("|")]
    if len(parts) < 3:
        raise ValueError(
            "Формат: /addevent YYYY-MM-DD HH:MM | Название | category | Описание | source_url | livestream_url"
        )

    start_at_utc = parse_datetime(parts[0], tz)
    title = parts[1]
    category = parts[2] or "phenomenon"
    description = parts[3] if len(parts) >= 4 else ""

    source_url = None
    livestream_url = None
    if len(parts) == 5:
        # Backward compatibility with the first MVP: the 5th field used to be livestream_url.
        livestream_url = parts[4] or None
    elif len(parts) >= 6:
        source_url = parts[4] or None
        livestream_url = parts[5] or None

    return AstroEvent(
        id=event_id(title, start_at_utc),
        title=title,
        start_at_utc=start_at_utc,
        category=category,
        description=description,
        source_url=source_url,
        livestream_url=livestream_url,
    )


def notification_stage(start_at_utc: datetime, now_utc: datetime | None = None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    delta = start_at_utc - now_utc

    if timedelta(days=1) < delta <= timedelta(days=7):
        return "week"
    if timedelta(hours=1) < delta <= timedelta(days=1):
        return "day"
    if timedelta(minutes=0) < delta <= timedelta(hours=1):
        return "hour"
    if timedelta(minutes=-15) <= delta <= timedelta(hours=2):
        return "live"
    return None
