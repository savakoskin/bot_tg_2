from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    filters,
)

from . import db
from .bot import (
    add_event_command,
    callback_handler,
    delete_event_command,
    events_command,
    event_sync_job,
    events_job,
    help_command,
    links_command,
    list_events_command,
    myid_command,
    news_command,
    news_job,
    photos_command,
    photos_job,
    settings_command,
    sources_command,
    start,
    status_command,
    botstats_command,
    sync_command,
    translate_command,
    text_menu_handler,
)
from .config import load_settings
from .events import load_seed_events
from .translation import TranslatorService


def build_application() -> Application:
    settings = load_settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    db.add_events(conn, load_seed_events(settings.event_seed_path, settings.timezone))

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .defaults(Defaults(tzinfo=settings.timezone))
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = conn
    application.bot_data["translator"] = TranslatorService(settings)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("botstats", botstats_command))
    application.add_handler(CommandHandler("myid", myid_command))
    application.add_handler(CommandHandler("sources", sources_command))
    application.add_handler(CommandHandler("sync", sync_command))
    application.add_handler(CommandHandler("translate", translate_command))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("photos", photos_command))
    application.add_handler(CommandHandler("links", links_command))
    application.add_handler(CommandHandler("addevent", add_event_command))
    application.add_handler(CommandHandler("listevents", list_events_command))
    application.add_handler(CommandHandler("delevent", delete_event_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_menu_handler))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.job_queue.run_repeating(
        news_job,
        interval=settings.news_check_interval_seconds,
        first=15,
        name="news_job",
    )
    application.job_queue.run_repeating(
        photos_job,
        interval=settings.photo_check_interval_seconds,
        first=30,
        name="photos_job",
    )
    application.job_queue.run_repeating(
        event_sync_job,
        interval=settings.event_sync_interval_seconds,
        first=5,
        name="event_sync_job",
    )
    application.job_queue.run_repeating(
        events_job,
        interval=settings.event_check_interval_seconds,
        first=45,
        name="events_job",
    )

    return application


def main() -> None:
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
