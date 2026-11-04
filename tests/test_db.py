"""Тесты на настоящем PostgreSQL: `docker compose run --rm tests`. Без TEST_DATABASE_URL — пропускаются."""
import os
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.bot import send_due
from app.config import load_funnel

URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="нужен TEST_DATABASE_URL (docker compose run --rm tests)")


@pytest.fixture
async def pool():
    p = await db.connect(URL)
    await p.execute("TRUNCATE events, scheduled_messages, users")
    yield p
    await p.close()


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))
        self.markups = getattr(self, 'markups', []) + [reply_markup]


async def test_funnel_end_to_end(pool):
    f = load_funnel()
    assert await db.touch_start(pool, 1, "anna", "Анна") is False
    await db.touch_start(pool, 2, "bob", "Боб")  # нажал /start, телефон не дал

    t0 = datetime.now(timezone.utc) - timedelta(seconds=30)  # «зарегистрировался 30 с назад»
    assert await db.register(pool, 1, "+79990000000", f.schedule, now=t0) is True
    assert await db.register(pool, 1, "+79990000000", f.schedule) is False  # второй раз очередь не ставим

    bot = FakeBot()
    assert await send_due(bot, f, pool) == 1  # +10 с — пора, +60 с — ещё нет
    assert bot.sent == [(1, f.schedule[0].text)]
    assert await send_due(bot, f, pool) == 0  # не дублирует

    await pool.execute("UPDATE scheduled_messages SET due_at = now() - interval '1 second'")  # «прошла минута»
    assert await send_due(bot, f, pool) == 1
    assert bot.sent[-1] == (1, f.schedule[1].text)

    s = await db.stats(pool, f.schedule)
    assert [r["count"] for r in s["funnel"]] == [2, 1, 1, 1, 0, 0, 0]  # дальше: нажали кнопку, сообщения 3 и 4 (после кнопки)
    assert bot.markups[0] is None and bot.markups[1].inline_keyboard[0][0].callback_data == "get:reminder_2"
    assert s["pending"] == 0 and s["failed"] == 0
    assert s["recent"][0]["event"] == "sent:reminder_2"


async def test_returning_user_is_recognized(pool):
    f = load_funnel()
    await db.touch_start(pool, 7, None, "Ира")
    await db.register(pool, 7, "+7999", f.schedule)
    assert await db.touch_start(pool, 7, None, "Ира") is True


class BrokenBot:
    async def send_message(self, chat_id, text, reply_markup=None):
        from aiogram.exceptions import TelegramBadRequest
        raise TelegramBadRequest(method=None, message="Bad Request: chat not found")


async def test_undeliverable_is_marked_failed_not_retried_forever(pool):
    f = load_funnel()
    await db.touch_start(pool, 9, None, "X")
    await db.register(pool, 9, "+7", f.schedule, now=datetime.now(timezone.utc) - timedelta(minutes=5))
    await send_due(BrokenBot(), f, pool)
    assert await db.due(pool) == []  # не висит в очереди
    assert (await db.stats(pool, f.schedule))["failed"] == 2


async def test_contact_without_start_still_registers(pool):
    """04.11 прогон: база очищена между /start и телефоном → бот ответил «уже зарегистрированы» и ничего не прислал."""
    f = load_funnel()
    assert await db.register(pool, 55, "+7", f.schedule) is True
    assert len(await pool.fetch("SELECT 1 FROM scheduled_messages WHERE tg_id = 55")) == len([s for s in f.schedule if not s.after and s.before_webinar is None])


async def test_checklist_button_click_is_counted_once(pool):
    f = load_funnel()
    step = next(s for s in f.schedule if s.button)
    await db.register(pool, 66, "+7", f.schedule)
    assert await db.click(pool, 66, step.key) is True
    assert await db.click(pool, 66, step.key) is False  # второй клик — не новый человек
    rows = {r["key"]: r["count"] for r in (await db.stats(pool, f.schedule))["funnel"]}
    assert rows[f"click:{step.key}"] == 1


async def test_click_starts_tail_messages_once(pool):
    """Демо-конфиг (видео): после «Получить чек-лист» через 10 с — «эфир через 10 минут», ещё через 10 с — «мы начали»."""
    from app.config import ROOT
    f = load_funnel(ROOT / "config.demo.yaml")
    tail = [s for s in f.schedule if s.after]
    assert [s.delay for s in tail] == [10, 20]
    await db.register(pool, 77, "+7", f.schedule)
    assert len(await pool.fetch("SELECT 1 FROM scheduled_messages WHERE tg_id = 77")) == len(f.schedule) - len(tail)
    await db.click(pool, 77, "reminder_2", f.schedule)
    await db.click(pool, 77, "reminder_2", f.schedule)  # второй клик хвост не дублирует
    assert len(await pool.fetch("SELECT 1 FROM scheduled_messages WHERE tg_id = 77")) == len(f.schedule)


async def test_webinar_messages_are_tied_to_webinar_time(pool):
    """Боевой config.yaml: «эфир через 10 минут» — в 17:50, «мы начали» — в 18:00, от какого бы момента ни считать регистрацию."""
    f = load_funnel()
    w = f.webinar_at
    assert w is not None
    await db.register(pool, 81, "+7", f.schedule, now=w - timedelta(hours=6), webinar_at=w)
    due = {r["step_key"]: r["due_at"] for r in await pool.fetch("SELECT step_key, due_at FROM scheduled_messages WHERE tg_id = 81")}
    assert due["live_soon"] == w - timedelta(minutes=10)
    assert due["live_now"] == w


async def test_late_registration_skips_past_webinar_messages(pool):
    f = load_funnel()
    w = f.webinar_at
    await db.register(pool, 82, "+7", f.schedule, now=w - timedelta(minutes=5), webinar_at=w)  # пришёл в 17:55
    keys = {r["step_key"] for r in await pool.fetch("SELECT step_key FROM scheduled_messages WHERE tg_id = 82")}
    assert "live_soon" not in keys and "live_now" in keys
    await db.register(pool, 83, "+7", f.schedule, now=w + timedelta(minutes=1), webinar_at=w)  # пришёл после начала
    keys = {r["step_key"] for r in await pool.fetch("SELECT step_key FROM scheduled_messages WHERE tg_id = 83")}
    assert not keys & {"live_soon", "live_now"}
