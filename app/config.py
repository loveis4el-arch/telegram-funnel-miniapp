"""Конфиг воронки (YAML) и настройки окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Step:
    key: str
    delay: int  # секунды после регистрации
    text: str
    title: str = ""  # подпись на дашборде; пусто — key
    button: str = ""  # кнопка под сообщением (необязательно)
    on_click: str = ""  # что бот пришлёт по нажатию кнопки
    after: str = ""  # от чего считать delay: пусто — от телефона, «click:<key>» — от нажатия кнопки шага <key>
    before_webinar: int | None = None  # секунды ДО начала эфира (webinar_at): 600 → за 10 минут, 0 → в момент начала


@dataclass(frozen=True)
class Funnel:
    greeting: str
    ask_phone: str
    phone_button: str
    phone_thanks: str
    phone_not_own: str
    already_registered: str
    schedule: tuple[Step, ...]
    dashboard_button: str
    dashboard_title: str
    webinar_at: datetime | None = None  # начало эфира; к нему привязаны шаги с before_webinar


def load_funnel(path: str | Path | None = None) -> Funnel:
    path = Path(path or os.getenv("FUNNEL_CONFIG", ROOT / "config.yaml"))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = tuple(Step(key=str(s["key"]), delay=int(s.get("delay") or 0), text=str(s["text"]), title=str(s.get("title") or s["key"]),
                       button=str(s.get("button") or ""), on_click=str(s.get("on_click") or "").strip(),
                       after=str(s.get("after") or ""),
                       before_webinar=None if s.get("before_webinar") is None else int(s["before_webinar"]))
                  for s in raw.get("schedule") or [])
    webinar_at = None
    if raw.get("webinar_at"):
        webinar_at = datetime.fromisoformat(str(raw["webinar_at"]))
        if webinar_at.tzinfo is None:
            raise ValueError(f"{path}: webinar_at нужен с часовым поясом, например 2026-11-05T18:00:00+03:00")
    if any(s.before_webinar is not None for s in steps) and webinar_at is None:
        raise ValueError(f"{path}: у шагов с before_webinar нужен webinar_at")
    if any(s.before_webinar is not None and (s.after or s.delay) for s in steps):
        raise ValueError(f"{path}: шаг считается либо от эфира (before_webinar), либо от события (delay/after) — не оба")
    keys = [s.key for s in steps]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{path}: повторяется key в schedule: {keys}")
    if any(bool(s.button) != bool(s.on_click) for s in steps):
        raise ValueError(f"{path}: у шага с button нужен on_click (и наоборот)")
    with_button = {s.key for s in steps if s.button}
    bad = [s.key for s in steps if s.after and not (s.after.startswith("click:") and s.after[6:] in with_button)]
    if bad:
        raise ValueError(f"{path}: after должен быть «click:<key шага с кнопкой>»: {bad}")
    if any(s.delay < 0 for s in steps):
        raise ValueError(f"{path}: delay не может быть отрицательным")
    dash = raw.get("dashboard") or {}
    return Funnel(
        greeting=raw["greeting"].strip(),
        ask_phone=raw["ask_phone"].strip(),
        phone_button=raw["phone_button"].strip(),
        phone_thanks=raw["phone_thanks"].strip(),
        phone_not_own=raw["phone_not_own"].strip(),
        already_registered=raw["already_registered"].strip(),
        # порядок на дашборде: от телефона → после кнопки → по времени эфира (раньше — выше)
        schedule=tuple(sorted(steps, key=lambda s: (s.before_webinar is not None, bool(s.after), s.delay, -(s.before_webinar or 0)))),
        dashboard_button=dash.get("button", "📊 Дашборд"),
        dashboard_title=dash.get("title", "Воронка"),
        webinar_at=webinar_at,
    )


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str
    webapp_url: str  # https-адрес дашборда (тоннель) — нужен, чтобы открыть миниапп в Telegram
    admin_ids: frozenset[int]  # кому показывать дашборд; пусто — всем, кто открыл из Telegram
    allow_browser: bool  # локальная отладка: дашборд в обычном браузере без подписи Telegram


def load_settings() -> Settings:
    ids = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        database_url=os.getenv("DATABASE_URL", "postgresql://funnel:funnel@localhost:5432/funnel"),
        webapp_url=os.getenv("WEBAPP_URL", ""),
        admin_ids=frozenset(ids),
        allow_browser=os.getenv("DASHBOARD_ALLOW_BROWSER", "0") == "1",
    )
