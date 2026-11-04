"""Telegram-бот (aiogram 3): /start → приветствие → телефон → сообщения по расписанию из config.yaml.

Запуск: python -m app.bot
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

from . import db
from .config import Funnel, Settings, load_funnel, load_settings

log = logging.getLogger("funnel.bot")
TICK = 1.0  # как часто проверяем очередь, секунды


def phone_keyboard(f: Funnel) -> ReplyKeyboardMarkup:
    # is_persistent: кнопка видна всегда, а не прячется за значком клавиатуры (Telegram Web прятал её — прогон 04.11).
    # Убираем её сами (ReplyKeyboardRemove) после того, как телефон получен.
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f.phone_button, request_contact=True)]],
                               resize_keyboard=True, is_persistent=True)


def build_router(f: Funnel, s: Settings, pool) -> Router:
    r = Router()

    @r.message(CommandStart())
    async def start(m: Message) -> None:
        u = m.from_user
        already = await db.touch_start(pool, u.id, u.username, u.first_name)
        log.info("start tg_id=%s already=%s", u.id, already)
        if already:
            await m.answer(f.already_registered)
            return
        await m.answer(f.greeting.format(name=u.first_name or "друг"))
        await m.answer(f.ask_phone, reply_markup=phone_keyboard(f))

    @r.message(F.contact)
    async def contact(m: Message) -> None:
        if m.contact.user_id != m.from_user.id:  # чужой контакт из записной книжки не засчитываем
            await m.answer(f.phone_not_own, reply_markup=phone_keyboard(f))
            return
        new = await db.register(pool, m.from_user.id, m.contact.phone_number, f.schedule, webinar_at=f.webinar_at)
        log.info("phone tg_id=%s new=%s", m.from_user.id, new)
        await m.answer(f.phone_thanks if new else f.already_registered, reply_markup=ReplyKeyboardRemove())

    @r.callback_query(F.data.startswith("get:"))
    async def button(q: CallbackQuery) -> None:
        step = next((s for s in f.schedule if s.button and s.key == q.data[4:]), None)
        if step is None:
            await q.answer("Кнопка устарела")
            return
        first = await db.click(pool, q.from_user.id, step.key, f.schedule)
        log.info("click %s tg_id=%s first=%s", step.key, q.from_user.id, first)
        await q.answer()
        await q.message.answer(step.on_click)

    @r.message(Command("stats"))
    async def stats(m: Message) -> None:
        if not s.webapp_url.startswith("https://"):
            await m.answer("WEBAPP_URL не задан: миниапп открывается только по https (см. README, «Тоннель»).")
            return
        if s.admin_ids and m.from_user.id not in s.admin_ids:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f.dashboard_button, web_app=WebAppInfo(url=s.webapp_url))]])
        await m.answer(f.dashboard_title, reply_markup=kb)

    return r


async def send_due(bot: Bot, f: Funnel, pool) -> int:
    """Один проход: отправить всё, у чего наступило время. → сколько отправлено."""
    steps, sent = {st.key: st for st in f.schedule}, 0
    for row in await db.due(pool):
        step = steps.get(row["step_key"])
        if step is None:  # шаг убрали из конфига — не шлём, но и не висим в очереди
            await db.mark_sent(pool, row["id"], row["tg_id"], row["step_key"], error="шаг удалён из config.yaml")
            continue
        try:
            kb = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=step.button, callback_data=f"get:{step.key}")]])
                  if step.button else None)
            await bot.send_message(row["tg_id"], step.text, reply_markup=kb)
            await db.mark_sent(pool, row["id"], row["tg_id"], row["step_key"])
            log.info("sent %s tg_id=%s", row["step_key"], row["tg_id"])
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest) as e:  # заблокировал бота / чата нет — повтор не поможет
            await db.mark_sent(pool, row["id"], row["tg_id"], row["step_key"], error=str(e))
            log.warning("undeliverable tg_id=%s: %s", row["tg_id"], e)
    return sent


async def scheduler(bot: Bot, f: Funnel, pool) -> None:
    """Раз в секунду проверяет очередь в базе. Очередь в базе, а не в памяти: перезапуск бота сообщения не теряет."""
    while True:
        try:
            await send_due(bot, f, pool)
        except Exception:  # noqa: BLE001 — сеть/база мигнули: пишем в лог и пробуем на следующем тике
            log.exception("scheduler tick failed")
        await asyncio.sleep(TICK)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s, f = load_settings(), load_funnel()
    if not s.bot_token:
        raise SystemExit("BOT_TOKEN не задан (.env)")
    pool = await db.connect(s.database_url)
    bot, dp = Bot(s.bot_token), Dispatcher()
    dp.include_router(build_router(f, s, pool))
    if s.webapp_url.startswith("https://"):
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text=f.dashboard_button, web_app=WebAppInfo(url=s.webapp_url)))
    task = asyncio.create_task(scheduler(bot, f, pool))
    log.info("bot started, steps=%s", [(st.key, st.delay) for st in f.schedule])
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
