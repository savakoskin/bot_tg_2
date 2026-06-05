from __future__ import annotations

from zoneinfo import ZoneInfo

from .classifier import label_for
from .events import human_delta
from .models import AstroEvent, NewsItem, PhotoItem

EVENT_CATEGORY_LABELS = {
    "meteor_shower": "метеорный поток",
    "eclipse": "затмение",
    "planet_conjunction": "соединение планет",
    "conjunction": "соединение",
    "comet": "комета",
    "phenomenon": "явление",
    "launch": "запуск",
    "science": "научное событие",
}


def _short(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def fmt_dt(dt, tz: ZoneInfo) -> str:
    if not dt:
        return "дата не указана"
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


def format_news(item: NewsItem, include_link: bool = True) -> str:
    cats = ", ".join(label_for(cat) for cat in sorted(item.categories))
    title = item.title_ru or item.title
    summary = _short(item.summary_ru or item.summary, 450)
    parts = [
        f"📰 {title}",
        f"Источник: {item.source}",
        f"Категория: {cats}",
    ]
    if item.title_ru and item.title_ru != item.title:
        parts.append(f"Оригинал: {item.title}")
    if summary:
        parts.append(summary)
    if include_link:
        parts.append(item.link)
    return "\n\n".join(parts)


def format_photo(item: PhotoItem) -> str:
    description = _short(item.description, 700)
    parts = [
        f"🖼 {item.title}",
        f"Источник: {item.source}",
    ]
    if description:
        parts.append(description)
    if item.page_url:
        parts.append(item.page_url)
    return "\n\n".join(parts)


def event_type_label(category: str) -> str:
    return EVENT_CATEGORY_LABELS.get(category, category)


def format_event(event: AstroEvent, tz: ZoneInfo, include_id: bool = False) -> str:
    local_dt = event.start_at_utc.astimezone(tz)
    title = event.title_ru or event.title
    description = event.description_ru or event.description
    parts = [
        f"🌌 {title}",
        f"Когда: {local_dt.strftime('%d.%m.%Y %H:%M')} ({tz.key})",
        f"Осталось: {human_delta(event.start_at_utc)}",
        f"Тип: {event_type_label(event.category)}",
    ]
    if include_id:
        parts.append(f"ID: {event.id}")
    if event.title_ru and event.title_ru != event.title:
        parts.append(f"Оригинал: {event.title}")
    if description:
        parts.append(description)
    if event.source_url:
        parts.append(f"Источник: {event.source_url}")
    if event.livestream_url:
        parts.append(f"Трансляция: {event.livestream_url}")
    return "\n\n".join(parts)
