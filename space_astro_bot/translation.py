from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import Iterable

from .config import Settings
from .models import AstroEvent, NewsItem

logger = logging.getLogger(__name__)

_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LETTER_RE = re.compile(r"[A-Za-zА-Яа-яЁё]")


def looks_russian(text: str) -> bool:
    """Return True when the text is already mostly Russian/Cyrillic."""
    text = (text or "").strip()
    if not text:
        return True
    letters = _LETTER_RE.findall(text)
    if not letters:
        return True
    cyrillic = _CYRILLIC_RE.findall(text)
    return len(cyrillic) / max(1, len(letters)) >= 0.45


def _shorten_for_translation(text: str, max_chars: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


class TranslatorService:
    """Small optional translation layer.

    The bot keeps original text in the database and stores Russian text in separate *_ru fields.
    If the translation package/API is unavailable, the bot continues to work and simply shows originals.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.enabled = settings.translation_enabled
        self.target_lang = settings.translation_target_lang or "ru"
        self.max_chars = settings.translation_max_chars
        self._translator = None
        self._available = False

        if not self.enabled:
            return

        try:
            # Optional dependency from requirements.txt. Imported lazily so the bot can run with
            # TRANSLATION_ENABLED=false even if the package is not installed.
            from deep_translator import GoogleTranslator  # type: ignore

            self._translator = GoogleTranslator(source="auto", target=self.target_lang)
            self._available = True
        except Exception as exc:  # pragma: no cover - depends on runtime installation/network
            logger.warning("Translation is enabled, but translator is unavailable: %s", exc)
            self._available = False

    async def translate_text(self, text: str) -> str | None:
        if not self.enabled or not self._available:
            return None
        text = _shorten_for_translation(text, self.max_chars)
        if not text or looks_russian(text):
            return text or None

        def _translate() -> str | None:
            try:
                result = self._translator.translate(text) if self._translator else None
                if not result:
                    return None
                result = " ".join(str(result).split())
                return result or None
            except Exception as exc:
                logger.warning("Failed to translate text: %s", exc)
                return None

        return await asyncio.to_thread(_translate)

    async def translate_news_items(self, items: Iterable[NewsItem], limit: int | None = None) -> list[NewsItem]:
        result: list[NewsItem] = []
        translated_count = 0
        max_items = limit if limit is not None else self.settings.translation_max_items_per_run

        for item in items:
            needs_translation = not item.title_ru and not looks_russian(item.title)
            if needs_translation and translated_count < max_items:
                title_ru = await self.translate_text(item.title)
                summary_ru = await self.translate_text(item.summary) if item.summary else None
                result.append(replace(item, title_ru=title_ru, summary_ru=summary_ru))
                translated_count += 1
            else:
                result.append(item)
        return result

    async def translate_events(self, events: Iterable[AstroEvent], limit: int | None = None) -> list[AstroEvent]:
        result: list[AstroEvent] = []
        translated_count = 0
        max_items = limit if limit is not None else self.settings.translation_max_items_per_run

        for event in events:
            needs_translation = not event.title_ru and not looks_russian(event.title)
            if needs_translation and translated_count < max_items:
                title_ru = await self.translate_text(event.title)
                description_ru = await self.translate_text(event.description) if event.description else None
                result.append(replace(event, title_ru=title_ru, description_ru=description_ru))
                translated_count += 1
            else:
                result.append(event)
        return result
