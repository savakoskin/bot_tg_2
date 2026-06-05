from __future__ import annotations

import re
from collections.abc import Iterable

CATEGORY_LABELS: dict[str, str] = {
    "all": "Все новости",
    "comets": "Кометы",
    "meteors": "Звездопады и явления",
    "science": "Научные открытия",
    "spaceflight": "Космонавтика и миссии",
}

CATEGORY_ORDER: tuple[str, ...] = ("all", "comets", "meteors", "science", "spaceflight")

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "comets": (
        "comet",
        "comets",
        "cometary",
        "c/",
        "interstellar comet",
        "swan",
        "atlas",
        "neowise",
        "комета",
        "кометы",
        "кометный",
        "межзвездная комета",
        "межзвездную комету",
    ),
    "meteors": (
        "meteor",
        "meteors",
        "meteor shower",
        "shooting star",
        "fireball",
        "perseid",
        "perseids",
        "geminid",
        "geminids",
        "leonid",
        "leonids",
        "orionid",
        "orionids",
        "eta aquarid",
        "lyrid",
        "lyrids",
        "eclipse",
        "lunar eclipse",
        "solar eclipse",
        "occultation",
        "conjunction",
        "opposition",
        "aurora",
        "skywatching",
        "stargazing",
        "night sky",
        "звездопад",
        "звездопады",
        "метеор",
        "метеоры",
        "метеорный поток",
        "болид",
        "затмение",
        "затмения",
        "сияние",
        "полярное сияние",
        "соединение",
        "противостояние",
        "вспышка на солнце",
        "магнитная буря",
        "солнечная активность",
    ),
    "science": (
        "research",
        "study",
        "scientists",
        "science",
        "discovery",
        "telescope",
        "observatory",
        "jwst",
        "james webb",
        "hubble",
        "roman",
        "black hole",
        "galaxy",
        "galaxies",
        "exoplanet",
        "planet",
        "cosmic",
        "universe",
        "supernova",
        "star formation",
        "dark matter",
        "dark energy",
        "наука",
        "научный",
        "исследование",
        "исследователи",
        "открытие",
        "открыли",
        "телескоп",
        "обсерватория",
        "галактика",
        "галактики",
        "экзопланета",
        "экзопланеты",
        "черная дыра",
        "сверхновая",
        "нейтронная звезда",
        "солнечная система",
        "темная материя",
        "джеймс уэбб",
        "уэбб",
        "хаббл",
    ),
    "spaceflight": (
        "launch",
        "rocket",
        "spacecraft",
        "mission",
        "artemis",
        "spacex",
        "starship",
        "crew",
        "astronaut",
        "cosmonaut",
        "iss",
        "space station",
        "lander",
        "rover",
        "orbiter",
        "satellite",
        "космонавт",
        "астронавт",
        "запуск",
        "ракета",
        "миссия",
        "мкс",
        "станция",
        "космическая станция",
        "корабль",
        "спутник",
        "луноход",
        "марсоход",
        "роскосмос",
        "союз",
        "прогресс",
        "пилотируемый",
        "пилотируемого",
        "космический аппарат",
        "байконур",
        "космодром",
        "выход в открытый космос",
        "орбита",
        "на орбите",
    ),
}


def classify_news(title: str, summary: str = "") -> set[str]:
    text = f"{title} {summary}".lower()
    text = re.sub(r"\s+", " ", text)
    categories: set[str] = set()

    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            categories.add(category)

    if not categories:
        categories.add("science")

    return categories


def normalize_category_set(value: str | Iterable[str] | None) -> set[str]:
    """Convert a DB value or iterable into a safe set of user-selected categories."""
    if value is None:
        return {"all"}

    if isinstance(value, str):
        raw_items = [part.strip().lower() for part in value.split(",")]
    else:
        raw_items = [str(part).strip().lower() for part in value]

    if not raw_items:
        return {"all"}

    if "all" in raw_items:
        return {"all"}

    selected = {item for item in raw_items if item in CATEGORY_LABELS and item != "all"}
    return selected or {"all"}


def category_set_to_db(value: str | Iterable[str] | None) -> str:
    categories = normalize_category_set(value)
    if "all" in categories:
        return "all"
    return ",".join(cat for cat in CATEGORY_ORDER if cat in categories and cat != "all")


def category_matches(user_category: str, item_categories: set[str]) -> bool:
    # Backward-compatible helper for older code paths.
    return categories_match({user_category}, item_categories)


def categories_match(user_categories: set[str], item_categories: set[str]) -> bool:
    normalized = normalize_category_set(user_categories)
    return "all" in normalized or bool(normalized.intersection(item_categories))


def label_for(category: str) -> str:
    return CATEGORY_LABELS.get(category, category)


def labels_for_categories(categories: set[str]) -> str:
    normalized = normalize_category_set(categories)
    if "all" in normalized:
        return CATEGORY_LABELS["all"]
    return ", ".join(label_for(cat) for cat in CATEGORY_ORDER if cat in normalized and cat != "all")
