"""PostgreSQL: схема и запросы. Расписание хранится в базе — перезапуск бота сообщения не теряет."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg

from .config import Step

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    tg_id          BIGINT PRIMARY KEY,
    username       TEXT,
    first_name     TEXT,
    phone          TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    registered_at  TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS scheduled_messages (
    id        BIGSERIAL PRIMARY KEY,
    tg_id     BIGINT NOT NULL REFERENCES users(tg_id),
    step_key  TEXT NOT NULL,
    due_at    TIMESTAMPTZ NOT NULL,
    sent_at   TIMESTAMPTZ,
    error     TEXT,
    UNIQUE (tg_id, step_key)
);
CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_messages (due_at) WHERE sent_at IS NULL;
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    tg_id       BIGINT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def connect(url: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
    return pool


async def event(pool: asyncpg.Pool, tg_id: int, name: str) -> None:
    await pool.execute("INSERT INTO events (tg_id, name) VALUES ($1, $2)", tg_id, name)


async def touch_start(pool: asyncpg.Pool, tg_id: int, username: str | None, first_name: str | None) -> bool:
    """/start → True, если человек уже зарегистрирован (дал телефон)."""
    row = await pool.fetchrow(
        """INSERT INTO users (tg_id, username, first_name) VALUES ($1, $2, $3)
           ON CONFLICT (tg_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
           RETURNING registered_at""",
        tg_id, username, first_name)
    await event(pool, tg_id, "start")
    return row["registered_at"] is not None


async def register(pool: asyncpg.Pool, tg_id: int, phone: str, steps: tuple[Step, ...], now: datetime | None = None,
                   webinar_at: datetime | None = None) -> bool:
    """Телефон получен → ставим сообщения по расписанию. False — уже был зарегистрирован (второй раз не ставим)."""
    now = now or datetime.now(timezone.utc)
    async with pool.acquire() as c, c.transaction():
        # INSERT … ON CONFLICT: телефон может прийти и без строки users (база очищена после /start) — тоже регистрация
        done = await c.fetchval(
            """INSERT INTO users (tg_id, phone, registered_at) VALUES ($1, $2, $3)
               ON CONFLICT (tg_id) DO UPDATE SET phone = EXCLUDED.phone, registered_at = EXCLUDED.registered_at
               WHERE users.registered_at IS NULL
               RETURNING tg_id""",
            tg_id, phone, now)
        if done is None:
            return False
        await _enqueue(c, tg_id, [s for s in steps if not s.after and s.before_webinar is None], now)
        await _enqueue_webinar(c, tg_id, steps, webinar_at, now)
        await c.execute("INSERT INTO events (tg_id, name) VALUES ($1, 'phone')", tg_id)
    return True


async def _enqueue(c, tg_id: int, steps, now: datetime) -> None:
    await c.executemany(
        "INSERT INTO scheduled_messages (tg_id, step_key, due_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        [(tg_id, s.key, now + timedelta(seconds=s.delay)) for s in steps])


async def _enqueue_webinar(c, tg_id: int, steps, webinar_at: datetime | None, now: datetime) -> None:
    """Шаги «за N секунд до эфира» встают на webinar_at - N. Время уже прошло (поздно зарегистрировался) — шаг не ставим."""
    if webinar_at is None:
        return
    rows = [(tg_id, s.key, webinar_at - timedelta(seconds=s.before_webinar)) for s in steps if s.before_webinar is not None]
    rows = [r for r in rows if r[2] >= now]
    if rows:
        await c.executemany(
            "INSERT INTO scheduled_messages (tg_id, step_key, due_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", rows)


async def due(pool: asyncpg.Pool, limit: int = 50) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT id, tg_id, step_key FROM scheduled_messages WHERE sent_at IS NULL AND due_at <= now() ORDER BY due_at LIMIT $1",
        limit)


async def mark_sent(pool: asyncpg.Pool, msg_id: int, tg_id: int, step_key: str, error: str | None = None) -> None:
    await pool.execute("UPDATE scheduled_messages SET sent_at = now(), error = $2 WHERE id = $1", msg_id, error)
    await event(pool, tg_id, f"sent:{step_key}" if error is None else f"failed:{step_key}")


async def click(pool: asyncpg.Pool, tg_id: int, step_key: str, steps: tuple[Step, ...] = ()) -> bool:
    """Нажал кнопку под сообщением → True, если впервые (на дашборде — люди, а не нажатия).
    Первое нажатие ставит в очередь шаги с after: click:<step_key>."""
    async with pool.acquire() as c, c.transaction():
        await c.execute("SELECT pg_advisory_xact_lock($1)", tg_id)
        seen = await c.fetchval("SELECT 1 FROM events WHERE tg_id = $1 AND name = $2", tg_id, f"click:{step_key}")
        if seen:
            return False
        await c.execute("INSERT INTO events (tg_id, name) VALUES ($1, $2)", tg_id, f"click:{step_key}")
        await _enqueue(c, tg_id, [s for s in steps if s.after == f"click:{step_key}"], datetime.now(timezone.utc))
    return True


def funnel_rows(started: int, registered: int, sent: dict[str, int], steps: tuple[Step, ...],
                clicks: dict[str, int] | None = None) -> list[dict]:
    """Шаги воронки: сколько дошло и % от предыдущего шага и от /start."""
    clicks = clicks or {}
    rows = [("start", "Нажали /start", started), ("phone", "Дали телефон", registered)]
    for s in steps:
        if s.before_webinar is not None:
            when = f"за {s.before_webinar // 60} мин до эфира" if s.before_webinar else "в начале эфира"
        else:
            when = f"+{s.delay} с" + (" после кнопки" if s.after else "")
        rows.append((s.key, f"{s.title} ({when})", sent.get(s.key, 0)))
        if s.button:
            rows.append((f"click:{s.key}", f"Нажали «{s.button}»", clicks.get(s.key, 0)))
    out, prev = [], None
    for key, label, n in rows:
        out.append({"key": key, "label": label, "count": n,
                    "from_prev": round(100 * n / prev) if prev else (100 if prev is None and n else 0),
                    "from_start": round(100 * n / started) if started else 0})
        prev = n
    return out


async def stats(pool: asyncpg.Pool, steps: tuple[Step, ...]) -> dict:
    started = await pool.fetchval("SELECT count(*) FROM users")
    registered = await pool.fetchval("SELECT count(*) FROM users WHERE registered_at IS NOT NULL")
    sent = {r["step_key"]: r["n"] for r in await pool.fetch(
        "SELECT step_key, count(*) AS n FROM scheduled_messages WHERE sent_at IS NOT NULL AND error IS NULL GROUP BY step_key")}
    clicks = {r["k"]: r["n"] for r in await pool.fetch(
        "SELECT substr(name, 7) AS k, count(DISTINCT tg_id) AS n FROM events WHERE name LIKE 'click:%' GROUP BY 1")}
    pending = await pool.fetchval("SELECT count(*) FROM scheduled_messages WHERE sent_at IS NULL")
    failed = await pool.fetchval("SELECT count(*) FROM scheduled_messages WHERE error IS NOT NULL")
    today = await pool.fetchval("SELECT count(*) FROM users WHERE started_at >= date_trunc('day', now())")
    recent = await pool.fetch(
        """SELECT e.created_at, e.name, coalesce(u.first_name, u.username, e.tg_id::text) AS who
           FROM events e LEFT JOIN users u USING (tg_id) ORDER BY e.id DESC LIMIT 10""")
    return {
        "titles": {s.key: s.title for s in steps} | {f"btn:{s.key}": s.button for s in steps if s.button},
        "funnel": funnel_rows(started, registered, sent, steps, clicks),
        "pending": pending, "failed": failed, "started_today": today,
        "recent": [{"at": r["created_at"].isoformat(), "event": r["name"], "who": r["who"]} for r in recent],
    }
