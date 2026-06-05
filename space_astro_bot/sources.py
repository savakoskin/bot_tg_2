from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

import feedparser
import httpx

from .classifier import classify_news
from .config import Settings
from .models import NewsItem, PhotoItem

# The function name all_rss_sources is kept for compatibility with older bot code.
# Some Russian sources below do not always expose a stable RSS feed, so fetch_news
# automatically falls back to parsing public HTML listing pages.
RSS_SOURCES: tuple[tuple[str, str], ...] = (
    ("NASA News Releases", "https://www.nasa.gov/news-release/feed/"),
    ("NASA Recently Published", "https://www.nasa.gov/feed/"),
    ("NASA Artemis", "https://www.nasa.gov/missions/artemis/feed/"),
    ("ESA Space News", "https://www.esa.int/rssfeed/Our_Activities/Space_News"),
    ("ESA Space Science", "https://www.esa.int/rssfeed/Our_Activities/Space_Science"),
    ("Sky & Telescope Astronomy News", "https://skyandtelescope.com/astronomy-news/feed/"),
    ("Sky & Telescope Night Sky News", "https://skyandtelescope.com/astronomy-news/observing-news/feed/"),
    ("Российский космос", "https://r-kosmos.ru/"),
    ("Ин-Спейс", "https://in-space.ru/news/"),
    ("Космос-журнал", "https://www.cosmos-journal.ru/news/"),
    ("Новости Космонавтики", "https://novosti-kosmonavtiki.ru/news/"),
    ("Pro Космос", "https://prokosmos.ru/"),
)

NASA_IOTD_RSS = "https://www.nasa.gov/feeds/iotd-feed/"
USER_AGENT = "space-astro-telegram-bot/0.4 (+https://telegram.org)"

NOISE_LINK_TEXTS = {
    "главная",
    "новости",
    "статьи",
    "видео",
    "вселенная",
    "люди",
    "организации",
    "космическая техника",
    "о проекте",
    "контакты",
    "обратная связь",
    "карта сайта",
    "поиск",
    "далее",
    "читать далее",
    "показать еще",
    "показать ещё",
    "показать все материалы",
    "показать ещё материалы",
    "войти",
    "регистрация",
    "телеграм",
    "вконтакте",
    "одноклассники",
    "форум",
    "rss",
}

NOISE_PATH_PARTS = (
    "/tag/",
    "/tags/",
    "/rubric/",
    "/category/",
    "/author/",
    "/page/",
    "/pages/",
    "/contacts",
    "/contact",
    "/about",
    "/search",
    "/login",
    "/register",
    "/forum",
    "/rss",
    "/feed",
    "/privacy",
    "/cookie",
    "/issue/",
)

RU_MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}


def all_rss_sources(extra_sources: Iterable[tuple[str, str]] = ()) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for name, url in [*RSS_SOURCES, *tuple(extra_sources)]:
        if not name or not url or url in seen:
            continue
        seen.add(url)
        result.append((name, url))
    return tuple(result)


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<script\b.*?</script>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<style\b.*?</style>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _hash_id(*parts: str) -> str:
    raw = "|".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _entry_datetime(entry: object) -> datetime | None:
    for key in ("published", "updated", "created"):
        value = getattr(entry, key, None) if not isinstance(entry, dict) else entry.get(key)
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass

    parsed = None
    if isinstance(entry, dict):
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    else:
        parsed = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)

    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except Exception:
            return None
    return None


@dataclass(frozen=True)
class _Anchor:
    href: str
    text: str


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[_Anchor] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = None
        for key, value in attrs:
            if key and key.lower() == "href" and value:
                href = value.strip()
                break
        self._current_href = href
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._current_href:
            return
        text = _strip_html(" ".join(self._current_text))
        if text:
            self.anchors.append(_Anchor(self._current_href, text))
        self._current_href = None
        self._current_text = []


def _netloc_key(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc.removeprefix("www.")


def _normalized_link(base_url: str, href: str) -> str | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:")):
        return None
    absolute = urljoin(base_url, href)
    absolute, _fragment = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return absolute.rstrip("/") + ("/" if parsed.path == "/" else "")


def _is_noise_title(title: str) -> bool:
    title = _strip_html(title).strip(" \t\n\r—»«|•")
    lower = title.casefold()
    if not title or lower in NOISE_LINK_TEXTS:
        return True
    if len(title) < 18 or len(title) > 190:
        return True
    if re.fullmatch(r"[\d\s.,:;|/\\\-]+", title):
        return True
    if lower.startswith(("image", "рисунок", "фото:", "image:")) and len(title) < 50:
        return True
    if "cookie" in lower or "персональных данных" in lower:
        return True
    return False


def _is_probable_news_link(base_url: str, link: str, title: str) -> bool:
    if _is_noise_title(title):
        return False

    parsed = urlparse(link)
    base_host = _netloc_key(base_url)
    link_host = _netloc_key(link)
    if link_host != base_host:
        return False

    path = parsed.path or "/"
    path_lower = path.casefold()
    if path_lower in {"/", "/news", "/news/", "/articles", "/articles/"}:
        return False
    if any(part in path_lower for part in NOISE_PATH_PARTS):
        return False
    if re.search(r"\.(?:jpg|jpeg|png|webp|gif|svg|pdf|zip|rar|mp4|mp3)$", path_lower):
        return False

    # If a site uses numeric article URLs (for example r-kosmos.ru/1359/), accept them.
    if re.fullmatch(r"/\d+/?", path_lower):
        return True

    # Common article paths: /news/slug, /articles/slug, /YYYY/MM/DD/slug or /slug/.
    path_parts = [part for part in path_lower.strip("/").split("/") if part]
    if not path_parts:
        return False
    if path_parts[0] in {"news", "article", "articles"} and len(path_parts) >= 2:
        return True
    if len(path_parts) >= 4 and re.fullmatch(r"20\d{2}", path_parts[0]):
        return True
    if len(path_parts) == 1 and not path_parts[0].isdigit():
        return True

    return True


def _dt_from_parts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime | None:
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_date_from_text(text: str) -> datetime | None:
    match = re.search(
        r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*,?\s*(\d{1,2}):(\d{2}))?\b",
        text,
    )
    if match:
        day, month, year = map(int, match.group(1, 2, 3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        return _dt_from_parts(year, month, day, hour, minute)

    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})(?:[T\s](\d{1,2}):(\d{2}))?\b", text)
    if match:
        year, month, day = map(int, match.group(1, 2, 3))
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        return _dt_from_parts(year, month, day, hour, minute)

    match = re.search(
        r"\b(\d{1,2})\s+"
        r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
        r"\s+(20\d{2})\b",
        text.casefold(),
    )
    if match:
        day = int(match.group(1))
        month = RU_MONTHS.get(match.group(2), 0)
        year = int(match.group(3))
        if month:
            return _dt_from_parts(year, month, day)

    return None


def _nearby_date(html: str, raw_href: str, title: str) -> datetime | None:
    indexes: list[int] = []
    for needle in (raw_href, unescape(raw_href), title[:80]):
        if not needle:
            continue
        idx = html.find(needle)
        if idx >= 0:
            indexes.append(idx)

    for idx in indexes:
        chunk = _strip_html(html[max(0, idx - 900) : idx + 900])
        parsed = _parse_date_from_text(chunk)
        if parsed:
            return parsed
    return None


def _parse_html_news(html: str, source_name: str, source_url: str, limit_per_source: int) -> list[NewsItem]:
    collector = _AnchorCollector()
    try:
        collector.feed(html)
    except Exception:
        return []

    items: list[NewsItem] = []
    seen: set[str] = set()
    for anchor in collector.anchors:
        title = _strip_html(anchor.text)
        link = _normalized_link(source_url, anchor.href)
        if not link or not _is_probable_news_link(source_url, link, title):
            continue
        key = link or title.casefold()
        if key in seen:
            continue
        seen.add(key)
        published_at = _nearby_date(html, anchor.href, title)
        categories = classify_news(title, "")
        items.append(
            NewsItem(
                id=_hash_id(source_name, link, title),
                title=title,
                link=link,
                source=source_name,
                summary="",
                published_at=published_at,
                categories=categories,
            )
        )
        if len(items) >= limit_per_source:
            break

    return items


def _news_items_from_feed(source_name: str, content: bytes, limit_per_source: int) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    if not parsed.entries:
        return []

    items: list[NewsItem] = []
    for entry in parsed.entries[:limit_per_source]:
        title = _strip_html(entry.get("title", ""))
        link = entry.get("link", "")
        summary = _strip_html(entry.get("summary", entry.get("description", "")))
        if not title or not link:
            continue
        published_at = _entry_datetime(entry)
        categories = classify_news(title, summary)
        items.append(
            NewsItem(
                id=_hash_id(source_name, link, title),
                title=title,
                link=link,
                source=source_name,
                summary=summary,
                published_at=published_at,
                categories=categories,
            )
        )
    return items


async def fetch_news(
    limit_per_source: int = 5,
    extra_sources: Iterable[tuple[str, str]] = (),
) -> list[NewsItem]:
    items: list[NewsItem] = []
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        for source_name, url in all_rss_sources(extra_sources):
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception:
                continue

            source_items = _news_items_from_feed(source_name, response.content, limit_per_source)
            if not source_items:
                content_type = response.headers.get("content-type", "")
                if "html" in content_type.casefold() or response.text.lstrip().startswith(("<!", "<html", "<HTML")):
                    source_items = _parse_html_news(response.text, source_name, str(response.url), limit_per_source)

            items.extend(source_items)

    # Stable de-duplication by URL/title.
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in sorted(items, key=lambda i: i.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        key = item.link or item.title
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


async def fetch_apod(settings: Settings) -> PhotoItem | None:
    params = {"api_key": settings.nasa_api_key, "thumbs": "True"}
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        try:
            response = await client.get("https://api.nasa.gov/planetary/apod", params=params)
            response.raise_for_status()
            data = response.json()
        except Exception:
            return None

    date_value = data.get("date") or datetime.now(timezone.utc).date().isoformat()
    media_type = data.get("media_type")
    image_url = None
    if media_type == "image":
        image_url = data.get("hdurl") or data.get("url")
    elif data.get("thumbnail_url"):
        image_url = data.get("thumbnail_url")

    page_url = data.get("url")
    if media_type == "image":
        page_url = data.get("hdurl") or data.get("url")

    return PhotoItem(
        id=f"apod:{date_value}",
        title=_strip_html(data.get("title") or "NASA Astronomy Picture of the Day"),
        source="NASA APOD",
        image_url=image_url,
        page_url=page_url,
        description=_strip_html(data.get("explanation") or ""),
        published_at=datetime.fromisoformat(date_value).replace(tzinfo=timezone.utc),
    )


async def fetch_nasa_image_of_the_day() -> PhotoItem | None:
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:
        try:
            response = await client.get(NASA_IOTD_RSS)
            response.raise_for_status()
        except Exception:
            return None

    parsed = feedparser.parse(response.content)
    if not parsed.entries:
        return None

    entry = parsed.entries[0]
    title = _strip_html(entry.get("title", "NASA Image of the Day"))
    link = entry.get("link", "")
    summary = _strip_html(entry.get("summary", entry.get("description", "")))
    published_at = _entry_datetime(entry)

    image_url = None
    # RSS feeds may include a media thumbnail/content URL or an image in the HTML summary.
    media_content: Iterable[dict] = entry.get("media_content", []) or []
    for media in media_content:
        if media.get("url"):
            image_url = media["url"]
            break

    if not image_url:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', entry.get("summary", "") or "")
        if match:
            image_url = match.group(1)

    return PhotoItem(
        id=_hash_id("nasa-iotd", link, title),
        title=title,
        source="NASA Image of the Day",
        image_url=image_url,
        page_url=link,
        description=summary,
        published_at=published_at,
    )


async def fetch_photos(settings: Settings) -> list[PhotoItem]:
    photos: list[PhotoItem] = []
    apod = await fetch_apod(settings)
    if apod:
        photos.append(apod)
    iotd = await fetch_nasa_image_of_the_day()
    if iotd:
        photos.append(iotd)
    return photos
