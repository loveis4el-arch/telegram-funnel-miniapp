"""FastAPI: миниапп-дашборд и /api/stats.

Запуск: uvicorn app.api:app --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from . import db, webapp_auth
from .config import load_funnel, load_settings

STATIC = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings, app.state.funnel = load_settings(), load_funnel()
    app.state.pool = await db.connect(app.state.settings.database_url)
    yield
    await app.state.pool.close()


app = FastAPI(title="Funnel dashboard", lifespan=lifespan)


def check_access(init_data: str | None) -> None:
    s = app.state.settings
    user = webapp_auth.verify(init_data or "", s.bot_token)
    if user is None:
        if s.allow_browser and not init_data:  # локальная отладка в браузере
            return
        raise HTTPException(401, "откройте дашборд из Telegram")
    if s.admin_ids and int(user.get("id", 0)) not in s.admin_ids:
        raise HTTPException(403, "нет доступа")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "dashboard.html")


@app.get("/api/stats")
async def stats(x_telegram_init_data: str | None = Header(default=None)) -> dict:
    check_access(x_telegram_init_data)
    return {"title": app.state.funnel.dashboard_title, **await db.stats(app.state.pool, app.state.funnel.schedule)}


@app.get("/health")
async def health() -> dict:
    await app.state.pool.fetchval("SELECT 1")
    return {"ok": True}
