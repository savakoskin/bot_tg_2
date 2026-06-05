from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

import httpx

from .config import Settings
from .models import AstroEvent

DEFAULT_ICS_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "In-The-Sky.org",
        "https://in-the-sky.org/newscalyear_ical.php?maxdiff=7&year={year}",
    ),
)
USER_AGENT = "space-astro-telegram-bot/0.4 (+https://telegram.org)"


def all_event_ics_sources(extra_sources: Iterable[tuple[str, str]] = ()) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for name, url in [*DEFAULT_ICS_SOURCES, *tuple(extra_sources)]:
        if not name or not url or url in seen:
            continue
        seen.add(url)
        result.append((name, url))
    return tuple(result)


def _hash_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _clean_ics_value(value: str) -> str:
    return (
        value.replace(r"\n", "\n")
        .replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\\", "\\")
        .strip()
    )


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        else:
            lines.append(raw_line)
    return lines


def _parse_ics_datetime(key_part: str, value: str, local_tz) -> datetime | None:
    value = value.strip()
    params = key_part.split(";")[1:]
    is_date = any(param.upper() == "VALUE=DATE" for param in params)

    try:
        if is_date or re.fullmatch(r"\d{8}", value):
            # All-day calendar events do not have an exact time. Noon local time is a practical reminder time.
            return datetime.strptime(value[:8], "%Y%m%d").replace(hour=12, tzinfo=local_tz).astimezone(timezone.utc)

        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

        # Floating local time: use the bot timezone.
        if re.fullmatch(r"\d{8}T\d{6}", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=local_tz).astimezone(timezone.utc)
    except ValueError:
        return None
    return None


def _category_from_title(title: str) -> str:
    lowered = title.lower()
    if "meteor" in lowered or "shower" in lowered or "звездопад" in lowered:
        return "meteor_shower"
    if "eclipse" in lowered or "затм" in lowered:
        return "eclipse"
    if "comet" in lowered or "комет" in lowered:
        return "comet"
    if "occultation" in lowered or "покрыт" in lowered:
        return "occultation"
    if "conjunction" in lowered or "close approach" in lowered or "соедин" in lowered:
        return "conjunction"
    if "full moon" in lowered or "new moon" in lowered or "moon at" in lowered or "moon" in lowered:
        return "moon"
    if "asteroid" in lowered:
        return "asteroid"
    if "opposition" in lowered:
        return "opposition"
    if "solstice" in lowered or "equinox" in lowered:
        return "season"
    return "sky_event"


def parse_ics_events(text: str, source_name: str, local_tz) -> list[AstroEvent]:
    events: list[AstroEvent] = []
    current: dict[str, str] | None = None

    for line in _unfold_ics(text):
        if not line:
            continue
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current:
                title = _clean_ics_value(current.get("SUMMARY", ""))
                start_raw = current.get("DTSTART")
                start_key = current.get("DTSTART_KEY", "DTSTART")
                if title and start_raw:
                    start_at = _parse_ics_datetime(start_key, start_raw, local_tz)
                    if start_at:
                        uid = current.get("UID") or _hash_id(source_name, title, start_at.isoformat())
                        url = current.get("URL") or current.get("SOURCE_URL")
                        description = _clean_ics_value(current.get("DESCRIPTION", ""))
                        events.append(
                            AstroEvent(
                                id=_hash_id(source_name, uid, title, start_at.isoformat()),
                                title=title,
                                start_at_utc=start_at,
                                category=_category_from_title(title),
                                description=description or f"Автоматически импортировано из календаря {source_name}.",
                                source_url=url,
                                livestream_url=None,
                            )
                        )
            current = None
            continue
        if current is None or ":" not in line:
            continue

        key_part, value = line.split(":", 1)
        key = key_part.split(";", 1)[0].upper()
        if key == "DTSTART":
            current["DTSTART"] = value.strip()
            current["DTSTART_KEY"] = key_part
        elif key in {"SUMMARY", "DESCRIPTION", "URL", "UID"}:
            current[key] = value.strip()

    return events


def _expand_source_url(url: str, now_local: datetime, import_days: int) -> list[str]:
    if "{year}" not in url:
        return [url]
    end_local = now_local + timedelta(days=import_days)
    years = range(now_local.year, end_local.year + 1)
    return [url.replace("{year}", str(year)) for year in years]


async def fetch_ics_events(settings: Settings) -> list[AstroEvent]:
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(settings.timezone)
    end_utc = now_utc + timedelta(days=settings.event_import_days)
    imported: list[AstroEvent] = []

    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        for source_name, source_url in all_event_ics_sources(settings.event_ics_sources):
            for url in _expand_source_url(source_url, now_local, settings.event_import_days):
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except Exception:
                    continue

                for event in parse_ics_events(response.text, source_name, settings.timezone):
                    if now_utc - timedelta(days=1) <= event.start_at_utc <= end_utc:
                        imported.append(event)

    result: list[AstroEvent] = []
    seen: set[str] = set()
    for event in sorted(imported, key=lambda item: item.start_at_utc):
        if event.id in seen:
            continue
        seen.add(event.id)
        result.append(event)
    return result
