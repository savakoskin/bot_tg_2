from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    link: str
    source: str
    summary: str
    published_at: datetime | None
    categories: set[str]
    title_ru: str | None = None
    summary_ru: str | None = None


@dataclass(frozen=True)
class PhotoItem:
    id: str
    title: str
    source: str
    image_url: str | None
    page_url: str | None
    description: str
    published_at: datetime | None


@dataclass(frozen=True)
class AstroEvent:
    id: str
    title: str
    start_at_utc: datetime
    category: str
    description: str
    source_url: str | None = None
    livestream_url: str | None = None
    title_ru: str | None = None
    description_ru: str | None = None
