# CLAUDE.md — правила проекта для Claude Code

Мини-воронка: aiogram-бот + FastAPI-дашборд (Telegram Mini App) + PostgreSQL, всё в Docker Compose. Устройство — README.md.

## Перед правкой
- Тексты и тайминги — только в `config.yaml`, в коде их нет.
- Новый шаг воронки = строка в `schedule` конфига; код менять не нужно.
- Очередь отправки живёт в базе (`scheduled_messages`). Не заменять на `asyncio.sleep`: перезапуск потеряет сообщения.

## Порядок правки
1. Тест, который падает (`tests/`).
2. Правка.
3. `docker compose --profile test run --rm tests` — зелёный.
4. `docker compose up -d --build bot api` и `docker compose logs -f bot` — проверить шаг вживую.

## Известные грабли
- Тесты чистят таблицы → только база `funnel_test` (`TEST_DATABASE_URL`). Рабочую `funnel` тестам не давать.
- Ошибка Telegram «chat not found» / «bot was blocked» не лечится повтором → `failed`, иначе планировщик повторяет вечно.
- Миниапп открывается в Telegram только по https: локально — тоннель (README). С тоннелем `DASHBOARD_ALLOW_BROWSER=0`, иначе цифры видны по ссылке без Telegram.
- `.env` (токен бота) — не в git.
