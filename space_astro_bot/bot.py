from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import db
from .classifier import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    categories_match,
    labels_for_categories,
    normalize_category_set,
)
from .config import Settings
from .event_sources import all_event_ics_sources, fetch_ics_events
from .events import human_delta, notification_stage, parse_add_event_command
from .formatting import fmt_dt, format_event, format_news, format_photo
from .sources import all_rss_sources, fetch_news, fetch_photos

logger = logging.getLogger(__name__)

HELP_TEXT = """
Команды:
/start — запустить бота и открыть меню

В нижнем меню всегда есть кнопка 🚀 Старт / Главное меню. Она возвращает на главный экран без поиска старой кнопки «Назад».

/news — показать новости по выбранным категориям
/news all|comets|meteors|science|spaceflight — новости по конкретной категории
/settings — выбрать подписки и уведомления
/status — показать твои настройки
/events — события, до которых осталось меньше 7 дней
/photos — свежие фото NASA APOD и NASA Image of the Day
/links — полезные космические сайты
/sources — список источников новостей и календарей событий
/sync — вручную обновить базу новостей, фото и событий
/translate — перевести в базе свежие непереведенные новости и события
/myid — показать chat_id для ADMIN_CHAT_IDS

Админ-команды для событий:
/addevent YYYY-MM-DD HH:MM | Название | category | Описание | source_url | livestream_url
/listevents — список ближайших событий с ID
/delevent EVENT_ID — удалить событие

Категории новостей:
all — все
comets — кометы
meteors — звездопады, затмения, соединения и другие явления
science — научные открытия
spaceflight — космонавтика, ракеты и миссии
""".strip()


USEFUL_LINKS = (
    ("NASA", "https://www.nasa.gov/"),
    ("NASA APOD — Astronomy Picture of the Day", "https://apod.nasa.gov/apod/astropix.html"),
    ("NASA Eyes — интерактивные симуляции", "https://eyes.nasa.gov/"),
    ("NASA SkyCal", "https://eclipse.gsfc.nasa.gov/SKYCAL/SKYCAL.html"),
    ("ESA", "https://www.esa.int/"),
    ("Российский космос", "https://r-kosmos.ru/"),
    ("Ин-Спейс — новости, карта неба и явления", "https://in-space.ru/"),
    ("Космос-журнал", "https://www.cosmos-journal.ru/"),
    ("Новости Космонавтики", "https://novosti-kosmonavtiki.ru/"),
    ("Pro Космос", "https://prokosmos.ru/"),
    ("Stellarium Web — карта неба", "https://stellarium-web.org/"),
    ("TheSkyLive — небо сегодня", "https://theskylive.com/"),
    ("In-The-Sky.org — календарь явлений", "https://in-the-sky.org/"),
    ("Heavens-Above — спутники и пролеты МКС", "https://www.heavens-above.com/"),
    ("JPL Solar System Dynamics", "https://ssd.jpl.nasa.gov/"),
    ("WorldWide Telescope", "https://worldwidetelescope.org/"),
)

MENU_START = "🚀 Старт / Главное меню"
MENU_NEWS = "📰 Новости"
MENU_EVENTS = "🌌 События"
MENU_PHOTOS = "🖼 Фото NASA"
MENU_SETTINGS = "⚙ Настройки"
MENU_LINKS = "🔗 Полезные сайты"
MENU_STATUS = "📊 Статус"
MENU_HELP = "❓ Помощь"


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _conn(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    return context.application.bot_data["db"]


def _translator(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data.get("translator")


def _can_manage_events(settings: Settings, chat_id: int) -> bool:
    # Empty ADMIN_CHAT_IDS keeps local testing simple. For production, set ADMIN_CHAT_IDS in .env.
    return not settings.admin_chat_ids or chat_id in settings.admin_chat_ids


def reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_START],
            [MENU_NEWS, MENU_EVENTS],
            [MENU_PHOTOS, MENU_SETTINGS],
            [MENU_LINKS, MENU_STATUS],
            [MENU_HELP],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери раздел",
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📰 Новости", callback_data="menu:news"),
                InlineKeyboardButton("🌌 События", callback_data="menu:events"),
            ],
            [
                InlineKeyboardButton("🖼 Фото NASA", callback_data="menu:photos"),
                InlineKeyboardButton("⚙ Настройки", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("🔗 Полезные сайты", callback_data="menu:links")],
            [InlineKeyboardButton("📊 Статус", callback_data="menu:status")],
        ]
    )

def news_mode_menu(category: str | None = None) -> InlineKeyboardMarkup:
    category_value = category or "subscription"

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🆕 5 новых новостей",
                    callback_data=f"news:new:{category_value}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📜 5 прошлых новостей",
                    callback_data=f"news:previous:{category_value}:0",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎚 Выбрать категорию",
                    callback_data="news:choose_category",
                )
            ],
            [
                InlineKeyboardButton(
                    "🚀 Старт / Главное меню",
                    callback_data="menu:start",
                )
            ],
        ]
    )

def news_category_menu(include_back: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("✅ Моя подписка", callback_data="show_news:subscription")])
    rows.append([InlineKeyboardButton(CATEGORY_LABELS["all"], callback_data="show_news:all")])
    for category in CATEGORY_ORDER:
        if category == "all":
            continue
        rows.append([InlineKeyboardButton(CATEGORY_LABELS[category], callback_data=f"show_news:{category}")])
    rows.append([InlineKeyboardButton("⚙ Настроить подписку", callback_data="menu:settings")])
    if include_back:
        rows.append([InlineKeyboardButton("🚀 Старт / Главное меню", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


def settings_menu(user_row) -> InlineKeyboardMarkup:
    selected = db.get_user_categories(user_row)

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                ("✅ " if selected == {"all"} else "☑️ ") + CATEGORY_LABELS["all"],
                callback_data="set_news_cats:all",
            )
        ]
    )
    for key in CATEGORY_ORDER:
        if key == "all":
            continue
        prefix = "✅ " if key in selected else "▫️ "
        rows.append([InlineKeyboardButton(prefix + CATEGORY_LABELS[key], callback_data=f"toggle_news_cat:{key}")])

    flags = [
        ("news_enabled", "Автоновости"),
        ("photos_enabled", "Фото NASA"),
        ("events_enabled", "События"),
    ]
    flag_buttons = []
    for field, label in flags:
        enabled = bool(user_row[field]) if user_row else True
        flag_buttons.append(
            InlineKeyboardButton(("✅ " if enabled else "❌ ") + label, callback_data=f"toggle:{field}")
        )
    rows.append(flag_buttons)
    rows.append([InlineKeyboardButton("📰 Показать новости", callback_data="show_news:subscription")])
    rows.append([InlineKeyboardButton("🚀 Старт / Главное меню", callback_data="menu:start")])
    return InlineKeyboardMarkup(rows)


async def _send_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, **kwargs) -> None:
    # Если сообщение не использует inline-кнопки, автоматически показываем нижнее меню.
    # Так кнопка 🚀 Старт / Главное меню остается под рукой и не нужно искать старую кнопку «Назад».
    if "reply_markup" not in kwargs:
        kwargs["reply_markup"] = reply_menu()
    await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.upsert_user(_conn(context), chat_id)
    text = (
        "Привет! Я бот для новостей астрономии и космонавтики.\n\n"
        "Что умею:\n"
        "• присылать новости по одной или нескольким категориям;\n"
        "• показывать события, до которых осталось меньше недели;\n"
        "• присылать ссылку на трансляцию, если она есть;\n"
        "• уведомлять о новых фото NASA;\n"
        "• давать полезные ссылки на карты неба, симуляции и сайты."
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_menu())
        await update.message.reply_text("Главное меню:", reply_markup=main_menu())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.upsert_user(_conn(context), update.effective_chat.id)
    await update.message.reply_text(HELP_TEXT, reply_markup=reply_menu())


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    db.upsert_user(conn, chat_id)
    user_row = db.get_user(conn, chat_id)
    await update.message.reply_text(
        "Настройки рассылки. Можно выбрать одну или несколько категорий новостей:",
        reply_markup=settings_menu(user_row),
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await send_status_to_chat(context, chat_id)


async def botstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await send_botstats_to_chat(context, chat_id)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"Твой chat_id: {chat_id}\n\n"
        "Чтобы ограничить админ-команды только для себя, добавь в .env:\n"
        f"ADMIN_CHAT_IDS={chat_id}"
    )


async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_sources_to_chat(context, update.effective_chat.id)


async def send_status_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)
    user = db.get_user(conn, chat_id)
    categories = db.get_user_categories(user)

    def on_off(field: str) -> str:
        return "включено" if user and bool(user[field]) else "выключено"

    text = (
        "📊 Статус бота для этого чата\n\n"
        f"chat_id: {chat_id}\n"
        f"Категории новостей: {labels_for_categories(categories)}\n"
        f"Автоновости: {on_off('news_enabled')}\n"
        f"Фото NASA: {on_off('photos_enabled')}\n"
        f"События: {on_off('events_enabled')}\n"
        f"Часовой пояс: {settings.timezone_name}\n"
        f"Источников новостей: {len(all_rss_sources(settings.extra_rss_sources))}\n"
        f"Перевод на русский: {'включен' if settings.translation_enabled else 'выключен'}\n"
        f"Непереведенных новостей: {db.count_untranslated_news(conn)}\n"
        f"Непереведенных событий: {db.count_untranslated_events(conn)}"
    )

    sync_rows = db.get_sync_state(conn)

    await _send_text(context, chat_id, text, reply_markup=main_menu())


async def send_botstats_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    conn = _conn(context)
    settings = _settings(context)

    text = (
        "📊 Статистика базы данных\n\n"
        f"Календарей событий: {len(all_event_ics_sources(settings.event_ics_sources))}\n"
        f"Пользователей в базе: {db.count_users(conn)}\n"
        f"Новостей в архиве: {db.count_news_items(conn)}\n"
        f"Фото в архиве: {db.count_photo_items(conn)}\n"
        f"Событий в базе: {db.count_events(conn)}"
    )

    sync_rows = db.get_sync_state(conn)
    if sync_rows:
        text += "\n\nПоследняя синхронизация:"
        for row in sync_rows:
            status = row["last_success_at"] or f"ошибка: {row['last_error']}"
            text += f"\n• {row['name']}: {status}"

    await _send_text(context, chat_id, text)


async def send_sources_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    settings = _settings(context)
    lines = ["🧾 Источники новостей:"]
    for name, url in all_rss_sources(settings.extra_rss_sources):
        lines.append(f"• {name}: {url}")

    lines.append("\n🌌 iCalendar-источники событий:")
    for name, url in all_event_ics_sources(settings.event_ics_sources):
        lines.append(f"• {name}: {url}")

    lines.append("\nДополнительные источники можно добавить в .env через EXTRA_RSS_SOURCES.")
    lines.append("Дополнительные календари событий можно добавить в .env через EVENT_ICS_SOURCES.")
    await _send_text(context, chat_id, "\n".join(lines), disable_web_page_preview=True)


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.upsert_user(_conn(context), chat_id)

    args = getattr(context, "args", None) or []
    category = None

    if args:
        candidate = args[0].strip().lower()
        if candidate in CATEGORY_LABELS:
            category = candidate

    if category:
        await update.message.reply_text(
            f"Новости: {CATEGORY_LABELS[category]}\n\nЧто показать?",
            reply_markup=news_mode_menu(category),
        )
    else:
        await update.message.reply_text(
            "Новости по твоей подписке.\n\nЧто показать?",
            reply_markup=news_mode_menu(None),
        )


async def send_news_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    category: str | None = None,
    limit: int = 5,
) -> None:
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)
    user = db.get_user(conn, chat_id)

    if category and category in CATEGORY_LABELS:
        selected_categories = normalize_category_set({category})
    else:
        selected_categories = db.get_user_categories(user)

    await _send_text(context, chat_id, "Обновляю базу новостей и ищу новые записи…")

    try:
        fetched = await fetch_news(limit_per_source=6, extra_sources=settings.extra_rss_sources)

        translator = _translator(context)
        if translator:
            fetched = await translator.translate_news_items(fetched)

        db.upsert_news_items(conn, fetched)
        db.set_sync_success(conn, "news")
    except Exception as exc:
        logger.exception("Failed to refresh news for command")
        db.set_sync_error(conn, "news", str(exc))

    filtered = db.get_unseen_news(
        conn,
        chat_id,
        selected_categories,
        limit=limit,
    )

    if not filtered:
        await _send_text(
            context,
            chat_id,
            (
                "Новых новостей пока нет.\n\n"
                f"Категория: {labels_for_categories(selected_categories)}\n"
                "Ты уже видел все свежие новости из этой категории."
            ),
            reply_markup=news_mode_menu(category),
        )
        return

    await _send_text(
        context,
        chat_id,
        f"Категория: {labels_for_categories(selected_categories)}. Показываю {len(filtered)} новых новостей.",
    )

    for item in filtered:
        await send_news_item(context, chat_id, item)
        db.mark_news_delivered(conn, chat_id, item)

    await _send_text(
        context,
        chat_id,
        "Что показать дальше?",
        reply_markup=news_mode_menu(category),
    )

async def send_previous_news_to_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    category: str | None = None,
    offset: int = 0,
    limit: int = 5,
) -> None:
    conn = _conn(context)
    db.upsert_user(conn, chat_id)
    user = db.get_user(conn, chat_id)

    if category and category in CATEGORY_LABELS:
        selected_categories = normalize_category_set({category})
    else:
        selected_categories = db.get_user_categories(user)

    items = db.get_previous_news(
        conn,
        chat_id,
        selected_categories,
        limit=limit,
        offset=offset,
    )

    if not items:
        await _send_text(
            context,
            chat_id,
            (
                "Прошлых новостей пока нет.\n\n"
                "Они появятся после того, как ты хотя бы раз откроешь новые новости."
            ),
            reply_markup=news_mode_menu(category),
        )
        return

    await _send_text(
        context,
        chat_id,
        (
            f"📜 Прошлые новости\n\n"
            f"Категория: {labels_for_categories(selected_categories)}\n"
            f"Страница: {offset // limit + 1}"
        ),
    )

    for item in items:
        await send_news_item(context, chat_id, item)

    next_offset = offset + limit

    category_value = category or "subscription"

    await _send_text(
        context,
        chat_id,
        "Что показать дальше?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📜 Еще 5 прошлых",
                        callback_data=f"news:previous:{category_value}:{next_offset}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆕 5 новых новостей",
                        callback_data=f"news:new:{category_value}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🚀 Старт / Главное меню",
                        callback_data="menu:start",
                    )
                ],
            ]
        ),
    )

async def send_news_item(context: ContextTypes.DEFAULT_TYPE, chat_id: int, item) -> None:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_news(item, include_link=False),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Читать полностью", url=item.link)]]),
            disable_web_page_preview=False,
        )
    except TelegramError:
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_news(item, include_link=True),
            disable_web_page_preview=False,
        )


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_events_to_chat(context, update.effective_chat.id)


async def send_events_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int, days: int = 7) -> None:
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    now = datetime.now(timezone.utc)
    events = db.get_events_between(conn, now, now + timedelta(days=days))
    if not events:
        await _send_text(
            context,
            chat_id,
            "В базе пока нет событий, до которых осталось меньше 7 дней.\n"
            "Добавь событие командой /addevent или обнови data/events.example.json.",
        )
        return

    await _send_text(context, chat_id, f"События на ближайшие {days} дней:")
    for event in events:
        await _send_text(context, chat_id, format_event(event, settings.timezone), disable_web_page_preview=False)


async def list_events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    days = 30
    args = getattr(context, "args", None) or []
    if args:
        try:
            days = max(1, min(365, int(args[0])))
        except ValueError:
            pass

    now = datetime.now(timezone.utc)
    events = db.get_events_between(conn, now - timedelta(days=1), now + timedelta(days=days))
    if not events:
        await update.message.reply_text(f"Событий на ближайшие {days} дней нет.")
        return

    lines = [f"События на ближайшие {days} дней:"]
    for event in events[:20]:
        lines.append(f"• {fmt_dt(event.start_at_utc, settings.timezone)} — {event.title}\n  ID: {event.id}")
    if len(events) > 20:
        lines.append(f"\nПоказаны первые 20 из {len(events)}.")
    await update.message.reply_text("\n".join(lines), disable_web_page_preview=True)


async def photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    await send_photos_to_chat(context, chat_id)


async def send_photos_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    await _send_text(context, chat_id, "Обновляю базу фото NASA…")
    try:
        fetched = await fetch_photos(settings)
        db.upsert_photo_items(conn, fetched)
        db.set_sync_success(conn, "photos")
    except Exception as exc:
        logger.exception("Failed to refresh photos for command")
        db.set_sync_error(conn, "photos", str(exc))

    photos = db.get_latest_photos(conn, limit=4)
    if not photos:
        await _send_text(context, chat_id, "В базе пока нет фото NASA. Попробуй позже.")
        return

    for photo in photos:
        await send_photo_or_link(context, chat_id, photo)
        db.mark_photo_delivered(conn, chat_id, photo)


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_links_to_chat(context, update.effective_chat.id)


async def send_links_to_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    db.upsert_user(_conn(context), chat_id)
    lines = ["🔗 Полезные сайты про космос:"]
    for title, url in USEFUL_LINKS:
        lines.append(f"• {title}: {url}")
    await _send_text(context, chat_id, "\n".join(lines), disable_web_page_preview=True)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    if not _can_manage_events(settings, chat_id):
        await update.message.reply_text("Запускать ручную синхронизацию может только администратор.")
        return

    await update.message.reply_text("Запускаю синхронизацию базы…")

    news_new = photos_new = events_total = 0
    try:
        news = await fetch_news(limit_per_source=10, extra_sources=settings.extra_rss_sources)
        translator = _translator(context)
        if translator:
            news = await translator.translate_news_items(news)
        news_new = db.upsert_news_items(conn, news)
        db.set_sync_success(conn, "news")
    except Exception as exc:
        logger.exception("Manual news sync failed")
        db.set_sync_error(conn, "news", str(exc))

    try:
        photos = await fetch_photos(settings)
        photos_new = db.upsert_photo_items(conn, photos)
        db.set_sync_success(conn, "photos")
    except Exception as exc:
        logger.exception("Manual photos sync failed")
        db.set_sync_error(conn, "photos", str(exc))

    try:
        events = await fetch_ics_events(settings)
        translator = _translator(context)
        if translator:
            events = await translator.translate_events(events)
        db.add_events(conn, events)
        events_total = len(events)
        db.set_sync_success(conn, "events")
    except Exception as exc:
        logger.exception("Manual events sync failed")
        db.set_sync_error(conn, "events", str(exc))

    await update.message.reply_text(
        "Синхронизация завершена.\n"
        f"Новых новостей: {news_new}\n"
        f"Новых фото: {photos_new}\n"
        f"Событий импортировано/обновлено: {events_total}\n\n"
        "Проверить базу можно командой /status"
    )


async def translate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    if not _can_manage_events(settings, chat_id):
        await update.message.reply_text("Переводить архив базы может только администратор.")
        return

    translator = _translator(context)
    if not translator or not settings.translation_enabled:
        await update.message.reply_text("Перевод выключен. Включи TRANSLATION_ENABLED=true в .env.")
        return

    limit = settings.translation_max_items_per_run
    news = db.get_untranslated_news(conn, limit=limit)
    events = db.get_untranslated_events(conn, limit=limit)

    translated_news = await translator.translate_news_items(news, limit=limit)
    translated_events = await translator.translate_events(events, limit=limit)

    if translated_news:
        db.upsert_news_items(conn, translated_news)
    if translated_events:
        db.add_events(conn, translated_events)

    db.set_sync_success(conn, "translation")
    await update.message.reply_text(
        "Перевод завершен.\n"
        f"Обработано новостей: {len(translated_news)}\n"
        f"Обработано событий: {len(translated_events)}\n"
        f"Осталось непереведенных новостей: {db.count_untranslated_news(conn)}\n"
        f"Осталось непереведенных событий: {db.count_untranslated_events(conn)}"
    )


async def add_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    if not _can_manage_events(settings, chat_id):
        await update.message.reply_text("Добавлять события может только администратор.")
        return

    try:
        event = parse_add_event_command(update.message.text, settings.timezone)
    except Exception as exc:
        await update.message.reply_text(
            "Не получилось добавить событие.\n\n"
            "Формат:\n"
            "/addevent 2026-08-12 20:00 | Полное солнечное затмение | eclipse | Описание | https://source.example | https://live.example\n\n"
            "Короткий старый формат тоже работает:\n"
            "/addevent 2026-08-12 20:00 | Полное солнечное затмение | eclipse | Описание | https://live.example\n\n"
            f"Ошибка: {exc}"
        )
        return

    translator = _translator(context)
    if translator:
        translated = await translator.translate_events([event], limit=1)
        event = translated[0]
    db.add_event(conn, event)
    await update.message.reply_text("Событие добавлено:\n\n" + format_event(event, settings.timezone, include_id=True))


async def delete_event_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conn = _conn(context)
    settings = _settings(context)
    db.upsert_user(conn, chat_id)

    if not _can_manage_events(settings, chat_id):
        await update.message.reply_text("Удалять события может только администратор.")
        return

    args = getattr(context, "args", None) or []
    if not args:
        await update.message.reply_text("Формат: /delevent EVENT_ID\nID можно посмотреть через /listevents")
        return

    deleted = db.delete_event_by_prefix(conn, args[0])
    if deleted is None:
        await update.message.reply_text("Событие не найдено. Проверь ID через /listevents.")
        return

    await update.message.reply_text(f"Событие удалено: {deleted.title}")


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    conn = _conn(context)
    db.upsert_user(conn, chat_id)

    data = query.data or ""
    if data == "menu:start":
        await query.message.reply_text("Нижняя кнопка 🚀 Старт / Главное меню всегда возвращает сюда.", reply_markup=reply_menu())
        await query.message.reply_text("Главное меню:", reply_markup=main_menu())
        return
    if data == "menu:settings":
        user = db.get_user(conn, chat_id)
        await query.message.reply_text("Настройки:", reply_markup=settings_menu(user))
        return
    if data == "menu:news":
        await query.message.reply_text(
            "Новости по твоей подписке.\n\nЧто показать?",
            reply_markup=news_mode_menu(None),
        )
        return
    if data == "menu:events":
        await send_events_to_chat(context, chat_id)
        return
    if data == "menu:photos":
        await send_photos_to_chat(context, chat_id)
        return
    if data == "menu:links":
        await send_links_to_chat(context, chat_id)
        return
    if data == "menu:status":
        await send_status_to_chat(context, chat_id)
        return

    if data.startswith("show_news:"):
        category = data.split(":", 1)[1]

        if category == "subscription":
            await query.message.reply_text(
                "Новости по твоей подписке.\n\nЧто показать?",
                reply_markup=news_mode_menu(None),
            )
        elif category in CATEGORY_LABELS:
            await query.message.reply_text(
                f"Новости: {CATEGORY_LABELS[category]}\n\nЧто показать?",
                reply_markup=news_mode_menu(category),
            )
        else:
            await query.message.reply_text("Неизвестная категория.")

        return
    if data == "news:choose_category":
        await query.message.reply_text(
            "Выбери категорию новостей:",
            reply_markup=news_category_menu(),
        )
        return

    if data.startswith("news:new:"):
        category_value = data.split(":", 2)[2]
        category = None if category_value == "subscription" else category_value

        if category is not None and category not in CATEGORY_LABELS:
            await query.message.reply_text("Неизвестная категория.")
            return

        await send_news_to_chat(context, chat_id, category=category)
        return

    if data.startswith("news:previous:"):
        parts = data.split(":")
        category_value = parts[2]
        offset = int(parts[3]) if len(parts) > 3 else 0

        category = None if category_value == "subscription" else category_value

        if category is not None and category not in CATEGORY_LABELS:
            await query.message.reply_text("Неизвестная категория.")
            return

        await send_previous_news_to_chat(
            context,
            chat_id,
            category=category,
            offset=offset,
        )
        return

    if data.startswith("set_news_cats:"):
        category = data.split(":", 1)[1]
        if category not in CATEGORY_LABELS:
            await query.message.reply_text("Неизвестная категория.")
            return
        db.set_user_categories(conn, chat_id, {category})
        user = db.get_user(conn, chat_id)
        await query.message.reply_text(
            f"Категории новостей: {labels_for_categories(db.get_user_categories(user))}",
            reply_markup=settings_menu(user),
        )
        return

    if data.startswith("toggle_news_cat:"):
        category = data.split(":", 1)[1]
        if category not in CATEGORY_LABELS or category == "all":
            await query.message.reply_text("Неизвестная категория.")
            return
        user = db.get_user(conn, chat_id)
        selected = db.get_user_categories(user)
        if selected == {"all"}:
            selected = {category}
        elif category in selected:
            selected.remove(category)
        else:
            selected.add(category)
        if not selected:
            selected = {"all"}
        db.set_user_categories(conn, chat_id, selected)
        user = db.get_user(conn, chat_id)
        await query.message.reply_text(
            f"Категории новостей: {labels_for_categories(db.get_user_categories(user))}",
            reply_markup=settings_menu(user),
        )
        return

    if data.startswith("news_cat:"):
        # Compatibility with buttons from the first MVP.
        category = data.split(":", 1)[1]
        if category not in CATEGORY_LABELS:
            await query.message.reply_text("Неизвестная категория.")
            return
        db.set_user_categories(conn, chat_id, {category})
        user = db.get_user(conn, chat_id)
        await query.message.reply_text(
            f"Категория новостей изменена: {labels_for_categories(db.get_user_categories(user))}",
            reply_markup=settings_menu(user),
        )
        return

    if data.startswith("toggle:"):
        field = data.split(":", 1)[1]
        try:
            enabled = db.toggle_user_flag(conn, chat_id, field)
        except ValueError:
            await query.message.reply_text("Неизвестная настройка.")
            return
        user = db.get_user(conn, chat_id)
        await query.message.reply_text(
            "Настройка обновлена: " + ("включено" if enabled else "выключено"),
            reply_markup=settings_menu(user),
        )


async def text_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    db.upsert_user(_conn(context), chat_id)

    if text in {MENU_START, "Старт", "/start", "Главное меню", "Меню"}:
        await update.message.reply_text("Главное меню:", reply_markup=main_menu())
    elif text in {MENU_NEWS, "Новости"}:
        await update.message.reply_text(
            "Новости по твоей подписке.\n\nЧто показать?",
            reply_markup=news_mode_menu(None),
        )
    elif text in {MENU_EVENTS, "События"}:
        await send_events_to_chat(context, chat_id)
    elif text in {MENU_PHOTOS, "Фото NASA"}:
        await send_photos_to_chat(context, chat_id)
    elif text in {MENU_SETTINGS, "Настройки"}:
        user = db.get_user(_conn(context), chat_id)
        await update.message.reply_text("Настройки:", reply_markup=settings_menu(user))
    elif text in {MENU_LINKS, "Полезные сайты", "Сайты"}:
        await send_links_to_chat(context, chat_id)
    elif text in {MENU_STATUS, "Статус"}:
        await send_status_to_chat(context, chat_id)
    elif text in {MENU_HELP, "Помощь"}:
        await update.message.reply_text(HELP_TEXT, reply_markup=reply_menu())
    else:
        await update.message.reply_text(
            "Я не понял сообщение. Используй кнопки меню или команду /help.",
            reply_markup=reply_menu(),
        )


async def send_photo_or_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo) -> None:
    caption = format_photo(photo)
    if len(caption) > 1000:
        caption = caption[:997] + "…"

    try:
        if photo.image_url:
            await context.bot.send_photo(chat_id=chat_id, photo=photo.image_url, caption=caption)
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption, disable_web_page_preview=False)
    except TelegramError:
        await context.bot.send_message(chat_id=chat_id, text=caption, disable_web_page_preview=False)


async def news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    settings = _settings(context)

    try:
        items = await fetch_news(limit_per_source=5, extra_sources=settings.extra_rss_sources)
        translator = _translator(context)
        if translator:
            items = await translator.translate_news_items(items)
        db.upsert_news_items(conn, items)
        db.set_sync_success(conn, "news")
    except Exception as exc:
        logger.exception("Failed to fetch news")
        db.set_sync_error(conn, "news", str(exc))
        return

    users = db.get_users(conn, flag="news_enabled")
    for user in users:
        chat_id = int(user["chat_id"])
        user_categories = db.get_user_categories(user)
        sent = 0
        for item in items:
            if sent >= settings.news_max_per_run:
                break
            if not categories_match(user_categories, item.categories):
                continue
            if db.was_news_delivered(conn, chat_id, item.id):
                continue

            try:
                await send_news_item(context, chat_id, item)
                db.mark_news_delivered(conn, chat_id, item)
                sent += 1
            except TelegramError:
                logger.exception("Failed to send news to chat_id=%s", chat_id)


async def photos_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    settings = _settings(context)

    try:
        photos = await fetch_photos(settings)
        db.upsert_photo_items(conn, photos)
        db.set_sync_success(conn, "photos")
    except Exception as exc:
        logger.exception("Failed to fetch NASA photos")
        db.set_sync_error(conn, "photos", str(exc))
        return

    users = db.get_users(conn, flag="photos_enabled")
    for user in users:
        chat_id = int(user["chat_id"])
        for photo in photos:
            if db.was_photo_delivered(conn, chat_id, photo.id):
                continue
            try:
                await send_photo_or_link(context, chat_id, photo)
                db.mark_photo_delivered(conn, chat_id, photo)
            except TelegramError:
                logger.exception("Failed to send photo to chat_id=%s", chat_id)


async def event_sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    settings = _settings(context)
    try:
        events = await fetch_ics_events(settings)
        translator = _translator(context)
        if translator:
            events = await translator.translate_events(events)
        db.add_events(conn, events)
        db.set_sync_success(conn, "events")
        if events:
            logger.info("Imported/updated %s astronomy events", len(events))
    except Exception as exc:
        logger.exception("Failed to sync astronomy events")
        db.set_sync_error(conn, "events", str(exc))


async def events_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = _conn(context)
    settings = _settings(context)
    users = db.get_users(conn, flag="events_enabled")
    if not users:
        return

    now = datetime.now(timezone.utc)
    events = db.get_events_between(conn, now - timedelta(minutes=15), now + timedelta(days=7))
    if not events:
        return

    for user in users:
        chat_id = int(user["chat_id"])
        for event in events:
            stage = notification_stage(event.start_at_utc, now)
            if stage is None:
                continue
            if stage == "live" and not event.livestream_url:
                continue
            if db.was_event_notification_delivered(conn, chat_id, event.id, stage):
                continue

            if stage == "live":
                text = f"🔴 Событие началось или начинается прямо сейчас!\n\n{format_event(event, settings.timezone)}"
            else:
                text = f"⏳ Скоро астрономическое событие — осталось {human_delta(event.start_at_utc, now)}.\n\n{format_event(event, settings.timezone)}"

            try:
                await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=False)
                db.mark_event_notification(conn, chat_id, event.id, stage)
            except TelegramError:
                logger.exception("Failed to send event notification to chat_id=%s", chat_id)
