"""Проверка подписи Telegram WebApp initData — дашборд отдаёт цифры только тому, кто открыл его из Telegram.
Алгоритм — https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE = 24 * 3600


def verify(init_data: str, bot_token: str, now: float | None = None, max_age: int = MAX_AGE) -> dict | None:
    """→ данные пользователя ({'id': …}) или None, если подпись неверна или устарела."""
    if not init_data or not bot_token:
        return None
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    got = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return None
    if (now or time.time()) - int(fields.get("auth_date", 0)) > max_age:
        return None
    try:
        return json.loads(fields.get("user", "{}"))
    except json.JSONDecodeError:
        return None


def sign(fields: dict, bot_token: str) -> str:
    """Подписать как Telegram — для тестов."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})
