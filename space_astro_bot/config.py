from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _admin_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def _named_url_sources(value: str | None) -> tuple[tuple[str, str], ...]:
    """Parse NAME|https://...;Second name|https://... source lists."""
    if not value:
        return tuple()
    result: list[tuple[str, str]] = []
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" not in chunk:
            continue
        name, url = [part.strip() for part in chunk.split("|", 1)]
        if name and url.startswith(("http://", "https://")):
            result.append((name, url))
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    nasa_api_key: str
    timezone_name: str
    db_path: Path
    event_seed_path: Path
    admin_chat_ids: set[int]
    extra_rss_sources: tuple[tuple[str, str], ...]
    event_ics_sources: tuple[tuple[str, str], ...]
    news_check_interval_seconds: int
    photo_check_interval_seconds: int
    event_check_interval_seconds: int
    event_sync_interval_seconds: int
    event_import_days: int
    news_max_per_run: int
    translation_enabled: bool
    translation_target_lang: str
    translation_max_chars: int
    translation_max_items_per_run: int

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token == "put_telegram_bot_token_here":
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot via @BotFather and put the token into .env."
        )

    timezone_name = os.getenv("BOT_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow"

    return Settings(
        telegram_bot_token=token,
        nasa_api_key=os.getenv("NASA_API_KEY", "DEMO_KEY").strip() or "DEMO_KEY",
        timezone_name=timezone_name,
        db_path=Path(os.getenv("DB_PATH", "space_astro_bot.sqlite3")),
        event_seed_path=Path(os.getenv("EVENT_SEED_PATH", "data/events.example.json")),
        admin_chat_ids=_admin_ids(os.getenv("ADMIN_CHAT_IDS")),
        extra_rss_sources=_named_url_sources(os.getenv("EXTRA_RSS_SOURCES")),
        event_ics_sources=_named_url_sources(os.getenv("EVENT_ICS_SOURCES")),
        news_check_interval_seconds=_int_env("NEWS_CHECK_INTERVAL_SECONDS", 1800),
        photo_check_interval_seconds=_int_env("PHOTO_CHECK_INTERVAL_SECONDS", 21600),
        event_check_interval_seconds=_int_env("EVENT_CHECK_INTERVAL_SECONDS", 1800),
        event_sync_interval_seconds=_int_env("EVENT_SYNC_INTERVAL_SECONDS", 21600),
        event_import_days=max(7, _int_env("EVENT_IMPORT_DAYS", 370)),
        news_max_per_run=max(1, _int_env("NEWS_MAX_PER_RUN", 5)),
        translation_enabled=_bool_env("TRANSLATION_ENABLED", True),
        translation_target_lang=os.getenv("TRANSLATION_TARGET_LANG", "ru").strip() or "ru",
        translation_max_chars=max(500, _int_env("TRANSLATION_MAX_CHARS", 3500)),
        translation_max_items_per_run=max(1, _int_env("TRANSLATION_MAX_ITEMS_PER_RUN", 20)),
    )
