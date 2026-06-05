from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .classifier import category_set_to_db, categories_match, normalize_category_set
from .models import AstroEvent, NewsItem, PhotoItem

USER_FLAG_FIELDS = {"news_enabled", "photos_enabled", "events_enabled"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            news_category TEXT NOT NULL DEFAULT 'all',
            news_categories TEXT NOT NULL DEFAULT 'all',
            news_enabled INTEGER NOT NULL DEFAULT 1,
            photos_enabled INTEGER NOT NULL DEFAULT 1,
            events_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS news_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_ru TEXT,
            link TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            summary_ru TEXT,
            published_at TEXT,
            categories TEXT NOT NULL DEFAULT 'all',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_news_items_published
            ON news_items(published_at DESC);

        CREATE INDEX IF NOT EXISTS idx_news_items_source
            ON news_items(source);

        CREATE TABLE IF NOT EXISTS photo_items (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            image_url TEXT,
            page_url TEXT,
            description TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_photo_items_published
            ON photo_items(published_at DESC);

        CREATE TABLE IF NOT EXISTS sync_state (
            name TEXT PRIMARY KEY,
            last_success_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS delivered_news (
            chat_id INTEGER NOT NULL,
            entry_id TEXT NOT NULL,
            source TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, entry_id)
        );

        CREATE TABLE IF NOT EXISTS delivered_photos (
            chat_id INTEGER NOT NULL,
            photo_id TEXT NOT NULL,
            source TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, photo_id)
        );

        CREATE TABLE IF NOT EXISTS astro_events (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            title_ru TEXT,
            start_at_utc TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            description_ru TEXT,
            source_url TEXT,
            livestream_url TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_notifications (
            chat_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            delivered_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, event_id, stage)
        );
        """
    )

    # Migrations for old local databases from the first MVP.
    user_columns = _table_columns(conn, "users")
    if "news_categories" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN news_categories TEXT NOT NULL DEFAULT 'all'")
        conn.execute("UPDATE users SET news_categories = COALESCE(news_category, 'all')")

    news_columns = _table_columns(conn, "news_items")
    if "title_ru" not in news_columns:
        conn.execute("ALTER TABLE news_items ADD COLUMN title_ru TEXT")
    if "summary_ru" not in news_columns:
        conn.execute("ALTER TABLE news_items ADD COLUMN summary_ru TEXT")

    event_columns = _table_columns(conn, "astro_events")
    if "title_ru" not in event_columns:
        conn.execute("ALTER TABLE astro_events ADD COLUMN title_ru TEXT")
    if "description_ru" not in event_columns:
        conn.execute("ALTER TABLE astro_events ADD COLUMN description_ru TEXT")

    conn.commit()


def upsert_user(conn: sqlite3.Connection, chat_id: int) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO users(chat_id, created_at, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (chat_id, now, now),
    )
    conn.commit()


def get_user(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()


def get_user_categories(row: sqlite3.Row | None) -> set[str]:
    if row is None:
        return {"all"}
    keys = set(row.keys())
    if "news_categories" in keys:
        return normalize_category_set(row["news_categories"])
    if "news_category" in keys:
        return normalize_category_set(row["news_category"])
    return {"all"}


def set_user_category(conn: sqlite3.Connection, chat_id: int, category: str) -> None:
    set_user_categories(conn, chat_id, {category})


def set_user_categories(conn: sqlite3.Connection, chat_id: int, categories: set[str]) -> None:
    normalized = normalize_category_set(categories)
    db_value = category_set_to_db(normalized)
    legacy_value = "all" if "all" in normalized else sorted(normalized)[0]
    conn.execute(
        """
        UPDATE users
        SET news_category = ?, news_categories = ?, updated_at = ?
        WHERE chat_id = ?
        """,
        (legacy_value, db_value, utcnow_iso(), chat_id),
    )
    conn.commit()


def toggle_user_flag(conn: sqlite3.Connection, chat_id: int, field: str) -> int:
    if field not in USER_FLAG_FIELDS:
        raise ValueError(f"Unsupported flag: {field}")
    row = conn.execute(f"SELECT {field} FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    new_value = 0 if row and int(row[field]) else 1
    conn.execute(
        f"UPDATE users SET {field} = ?, updated_at = ? WHERE chat_id = ?",
        (new_value, utcnow_iso(), chat_id),
    )
    conn.commit()
    return new_value


def get_users(conn: sqlite3.Connection, flag: str | None = None) -> list[sqlite3.Row]:
    if flag is None:
        return list(conn.execute("SELECT * FROM users").fetchall())
    if flag not in USER_FLAG_FIELDS:
        raise ValueError(f"Unsupported flag: {flag}")
    return list(conn.execute(f"SELECT * FROM users WHERE {flag} = 1").fetchall())


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"] if row else 0)


def upsert_news_items(conn: sqlite3.Connection, items: Iterable[NewsItem]) -> int:
    """Save fetched news into the local archive. Returns how many rows were new."""
    new_rows = 0
    now = utcnow_iso()
    for item in items:
        exists = conn.execute("SELECT 1 FROM news_items WHERE id = ?", (item.id,)).fetchone()
        if exists is None:
            new_rows += 1
        conn.execute(
            """
            INSERT INTO news_items(
                id, title, title_ru, link, source, summary, summary_ru, published_at, categories, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                title_ru = COALESCE(excluded.title_ru, news_items.title_ru),
                link = excluded.link,
                source = excluded.source,
                summary = excluded.summary,
                summary_ru = COALESCE(excluded.summary_ru, news_items.summary_ru),
                published_at = excluded.published_at,
                categories = excluded.categories,
                updated_at = excluded.updated_at
            """,
            (
                item.id,
                item.title,
                item.title_ru,
                item.link,
                item.source,
                item.summary,
                item.summary_ru,
                _dt_to_iso(item.published_at),
                category_set_to_db(item.categories),
                now,
                now,
            ),
        )
    conn.commit()
    return new_rows


def get_latest_news(
    conn: sqlite3.Connection,
    categories: set[str] | None = None,
    limit: int = 10,
    scan_limit: int = 300,
) -> list[NewsItem]:
    selected = normalize_category_set(categories or {"all"})
    rows = conn.execute(
        """
        SELECT * FROM news_items
        ORDER BY COALESCE(published_at, created_at) DESC
        LIMIT ?
        """,
        (max(limit, scan_limit),),
    ).fetchall()
    result: list[NewsItem] = []
    for row in rows:
        item = _row_to_news(row)
        if categories_match(selected, item.categories):
            result.append(item)
        if len(result) >= limit:
            break
    return result

def has_delivered_news(conn: sqlite3.Connection, chat_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM delivered_news WHERE chat_id = ? LIMIT 1",
        (chat_id,),
    ).fetchone()
    return row is not None


def get_unseen_news(
    conn: sqlite3.Connection,
    chat_id: int,
    categories: set[str] | None = None,
    limit: int = 5,
    scan_limit: int = 500,
) -> list[NewsItem]:
    selected = normalize_category_set(categories or {"all"})

    if has_delivered_news(conn, chat_id):
        order_direction = "ASC"
    else:
        order_direction = "DESC"

    rows = conn.execute(
        f"""
        SELECT n.*
        FROM news_items n
        LEFT JOIN delivered_news d
            ON d.entry_id = n.id
            AND d.chat_id = ?
        WHERE d.entry_id IS NULL
        ORDER BY COALESCE(n.published_at, n.created_at) {order_direction}
        LIMIT ?
        """,
        (chat_id, max(limit, scan_limit)),
    ).fetchall()

    result: list[NewsItem] = []

    for row in rows:
        item = _row_to_news(row)

        if categories_match(selected, item.categories):
            result.append(item)

        if len(result) >= limit:
            break

    return result


def get_previous_news(
    conn: sqlite3.Connection,
    chat_id: int,
    categories: set[str] | None = None,
    limit: int = 5,
    offset: int = 0,
    scan_limit: int = 500,
) -> list[NewsItem]:
    selected = normalize_category_set(categories or {"all"})

    rows = conn.execute(
        """
        SELECT n.*
        FROM news_items n
        INNER JOIN delivered_news d
            ON d.entry_id = n.id
            AND d.chat_id = ?
        ORDER BY d.delivered_at DESC
        LIMIT ?
        OFFSET ?
        """,
        (chat_id, max(limit, scan_limit), offset),
    ).fetchall()

    result: list[NewsItem] = []

    for row in rows:
        item = _row_to_news(row)

        if categories_match(selected, item.categories):
            result.append(item)

        if len(result) >= limit:
            break

    return result

def get_unseen_news(
    conn: sqlite3.Connection,
    chat_id: int,
    categories: set[str] | None = None,
    limit: int = 10,
    scan_limit: int = 500,
) -> list[NewsItem]:
    selected = normalize_category_set(categories or {"all"})

    rows = conn.execute(
        """
        SELECT n.*
        FROM news_items n
        LEFT JOIN delivered_news d
            ON d.entry_id = n.id
            AND d.chat_id = ?
        WHERE d.entry_id IS NULL
        ORDER BY COALESCE(n.published_at, n.created_at) DESC
        LIMIT ?
        """,
        (chat_id, max(limit, scan_limit)),
    ).fetchall()

    result: list[NewsItem] = []

    for row in rows:
        item = _row_to_news(row)

        if categories_match(selected, item.categories):
            result.append(item)

        if len(result) >= limit:
            break

    return result

def count_news_items(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()
    return int(row["n"] if row else 0)


def upsert_photo_items(conn: sqlite3.Connection, items: Iterable[PhotoItem]) -> int:
    """Save fetched photos into the local archive. Returns how many rows were new."""
    new_rows = 0
    now = utcnow_iso()
    for item in items:
        exists = conn.execute("SELECT 1 FROM photo_items WHERE id = ?", (item.id,)).fetchone()
        if exists is None:
            new_rows += 1
        conn.execute(
            """
            INSERT INTO photo_items(
                id, title, source, image_url, page_url, description, published_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                source = excluded.source,
                image_url = excluded.image_url,
                page_url = excluded.page_url,
                description = excluded.description,
                published_at = excluded.published_at,
                updated_at = excluded.updated_at
            """,
            (
                item.id,
                item.title,
                item.source,
                item.image_url,
                item.page_url,
                item.description,
                _dt_to_iso(item.published_at),
                now,
                now,
            ),
        )
    conn.commit()
    return new_rows


def get_latest_photos(conn: sqlite3.Connection, limit: int = 5) -> list[PhotoItem]:
    rows = conn.execute(
        """
        SELECT * FROM photo_items
        ORDER BY COALESCE(published_at, created_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_photo(row) for row in rows]


def count_photo_items(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM photo_items").fetchone()
    return int(row["n"] if row else 0)


def set_sync_success(conn: sqlite3.Connection, name: str) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO sync_state(name, last_success_at, last_error, updated_at)
        VALUES (?, ?, NULL, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_success_at = excluded.last_success_at,
            last_error = NULL,
            updated_at = excluded.updated_at
        """,
        (name, now, now),
    )
    conn.commit()


def set_sync_error(conn: sqlite3.Connection, name: str, error: str) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO sync_state(name, last_success_at, last_error, updated_at)
        VALUES (?, NULL, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (name, error[:500], now),
    )
    conn.commit()


def get_sync_state(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM sync_state ORDER BY name").fetchall())


def was_news_delivered(conn: sqlite3.Connection, chat_id: int, entry_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM delivered_news WHERE chat_id = ? AND entry_id = ?",
        (chat_id, entry_id),
    ).fetchone()
    return row is not None


def mark_news_delivered(conn: sqlite3.Connection, chat_id: int, item: NewsItem) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO delivered_news(chat_id, entry_id, source, delivered_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, item.id, item.source, utcnow_iso()),
    )
    conn.commit()


def was_photo_delivered(conn: sqlite3.Connection, chat_id: int, photo_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM delivered_photos WHERE chat_id = ? AND photo_id = ?",
        (chat_id, photo_id),
    ).fetchone()
    return row is not None


def mark_photo_delivered(conn: sqlite3.Connection, chat_id: int, item: PhotoItem) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO delivered_photos(chat_id, photo_id, source, delivered_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, item.id, item.source, utcnow_iso()),
    )
    conn.commit()


def add_event(conn: sqlite3.Connection, event: AstroEvent) -> None:
    conn.execute(
        """
        INSERT INTO astro_events(
            id, title, title_ru, start_at_utc, category, description, description_ru, source_url, livestream_url, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            title_ru = COALESCE(excluded.title_ru, astro_events.title_ru),
            start_at_utc = excluded.start_at_utc,
            category = excluded.category,
            description = excluded.description,
            description_ru = COALESCE(excluded.description_ru, astro_events.description_ru),
            source_url = excluded.source_url,
            livestream_url = excluded.livestream_url
        """,
        (
            event.id,
            event.title,
            event.title_ru,
            event.start_at_utc.astimezone(timezone.utc).isoformat(),
            event.category,
            event.description,
            event.description_ru,
            event.source_url,
            event.livestream_url,
            utcnow_iso(),
        ),
    )
    conn.commit()


def add_events(conn: sqlite3.Connection, events: Iterable[AstroEvent]) -> None:
    for event in events:
        add_event(conn, event)


def get_events_between(
    conn: sqlite3.Connection,
    start_utc: datetime,
    end_utc: datetime,
) -> list[AstroEvent]:
    rows = conn.execute(
        """
        SELECT * FROM astro_events
        WHERE start_at_utc >= ? AND start_at_utc <= ?
        ORDER BY start_at_utc ASC
        """,
        (
            start_utc.astimezone(timezone.utc).isoformat(),
            end_utc.astimezone(timezone.utc).isoformat(),
        ),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def get_event_by_prefix(conn: sqlite3.Connection, event_id_or_prefix: str) -> AstroEvent | None:
    prefix = event_id_or_prefix.strip()
    if not prefix:
        return None
    rows = conn.execute(
        """
        SELECT * FROM astro_events
        WHERE id = ? OR id LIKE ?
        ORDER BY start_at_utc ASC
        LIMIT 2
        """,
        (prefix, prefix + "%"),
    ).fetchall()
    if len(rows) != 1:
        return None
    return _row_to_event(rows[0])


def delete_event_by_prefix(conn: sqlite3.Connection, event_id_or_prefix: str) -> AstroEvent | None:
    event = get_event_by_prefix(conn, event_id_or_prefix)
    if event is None:
        return None
    conn.execute("DELETE FROM astro_events WHERE id = ?", (event.id,))
    conn.execute("DELETE FROM event_notifications WHERE event_id = ?", (event.id,))
    conn.commit()
    return event


def was_event_notification_delivered(
    conn: sqlite3.Connection,
    chat_id: int,
    event_id: str,
    stage: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM event_notifications
        WHERE chat_id = ? AND event_id = ? AND stage = ?
        """,
        (chat_id, event_id, stage),
    ).fetchone()
    return row is not None


def mark_event_notification(
    conn: sqlite3.Connection,
    chat_id: int,
    event_id: str,
    stage: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO event_notifications(chat_id, event_id, stage, delivered_at)
        VALUES (?, ?, ?, ?)
        """,
        (chat_id, event_id, stage, utcnow_iso()),
    )
    conn.commit()


def count_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM astro_events").fetchone()
    return int(row["n"] if row else 0)


def _row_to_news(row: sqlite3.Row) -> NewsItem:
    return NewsItem(
        id=row["id"],
        title=row["title"],
        link=row["link"],
        source=row["source"],
        summary=row["summary"] or "",
        published_at=_dt_from_iso(row["published_at"]),
        categories=normalize_category_set(row["categories"] or "all"),
        title_ru=row["title_ru"] if "title_ru" in row.keys() else None,
        summary_ru=row["summary_ru"] if "summary_ru" in row.keys() else None,
    )


def _row_to_photo(row: sqlite3.Row) -> PhotoItem:
    return PhotoItem(
        id=row["id"],
        title=row["title"],
        source=row["source"],
        image_url=row["image_url"],
        page_url=row["page_url"],
        description=row["description"] or "",
        published_at=_dt_from_iso(row["published_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> AstroEvent:
    return AstroEvent(
        id=row["id"],
        title=row["title"],
        start_at_utc=datetime.fromisoformat(row["start_at_utc"]).astimezone(timezone.utc),
        category=row["category"],
        description=row["description"] or "",
        source_url=row["source_url"],
        livestream_url=row["livestream_url"],
        title_ru=row["title_ru"] if "title_ru" in row.keys() else None,
        description_ru=row["description_ru"] if "description_ru" in row.keys() else None,
    )


def get_untranslated_news(conn: sqlite3.Connection, limit: int = 20) -> list[NewsItem]:
    rows = conn.execute(
        """
        SELECT * FROM news_items
        WHERE (title_ru IS NULL OR title_ru = '')
        ORDER BY COALESCE(published_at, created_at) DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_news(row) for row in rows]


def get_untranslated_events(conn: sqlite3.Connection, limit: int = 20) -> list[AstroEvent]:
    rows = conn.execute(
        """
        SELECT * FROM astro_events
        WHERE (title_ru IS NULL OR title_ru = '')
        ORDER BY start_at_utc ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def count_untranslated_news(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM news_items WHERE title_ru IS NULL OR title_ru = ''").fetchone()
    return int(row["n"] if row else 0)


def count_untranslated_events(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM astro_events WHERE title_ru IS NULL OR title_ru = ''").fetchone()
    return int(row["n"] if row else 0)
