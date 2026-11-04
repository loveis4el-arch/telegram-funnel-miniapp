"""Тесты без базы: конфиг, подпись миниаппа, расчёт воронки."""
import json
import time

import pytest

from app.config import load_funnel
from app.db import funnel_rows
from app.webapp_auth import sign, verify

TOKEN = "123456:TEST"


def test_config_has_two_steps_10s_and_60s():
    f = load_funnel()
    assert [s.delay for s in f.schedule if not s.after and s.before_webinar is None] == [10, 60]  # требование задания
    assert all(s.text for s in f.schedule)
    assert "{name}" in f.greeting
    assert all(s.title and s.title != s.key for s in f.schedule)  # на дашборде — человеческие названия


def test_config_rejects_duplicate_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""greeting: a\nask_phone: b\nphone_button: c\nphone_thanks: d\nphone_not_own: e\nalready_registered: f
schedule:\n  - {key: x, delay: 1, text: t}\n  - {key: x, delay: 2, text: t}\n""", encoding="utf-8")
    with pytest.raises(ValueError, match="повторяется"):
        load_funnel(p)


def test_config_sorts_steps_by_delay(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("""greeting: a\nask_phone: b\nphone_button: c\nphone_thanks: d\nphone_not_own: e\nalready_registered: f
schedule:\n  - {key: late, delay: 60, text: t}\n  - {key: early, delay: 10, text: t}\n""", encoding="utf-8")
    assert [s.key for s in load_funnel(p).schedule] == ["early", "late"]


def _init(user_id=42, age=0):
    return sign({"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id, "first_name": "A"})}, TOKEN)


def test_webapp_signature_ok():
    assert verify(_init(), TOKEN)["id"] == 42


def test_webapp_signature_tampered():
    assert verify(_init().replace("42", "43"), TOKEN) is None


def test_webapp_signature_wrong_token():
    assert verify(_init(), "999:OTHER") is None


def test_webapp_signature_expired():
    assert verify(_init(age=2 * 24 * 3600), TOKEN) is None


def test_funnel_percentages():
    f = load_funnel()
    rows = funnel_rows(started=10, registered=5, sent={"reminder_1": 4, "reminder_2": 2}, steps=f.schedule, clicks={"reminder_2": 1})
    assert [r["count"] for r in rows][:5] == [10, 5, 4, 2, 1]
    assert [r["from_prev"] for r in rows[1:5]] == [50, 80, 50, 50]
    assert rows[3]["from_start"] == 20


def test_funnel_empty_has_no_division_by_zero():
    rows = funnel_rows(0, 0, {}, load_funnel().schedule)
    assert all(r["from_prev"] == 0 and r["from_start"] == 0 for r in rows)


def test_phone_button_always_visible():
    """03.11 прогон: в Telegram Web кнопка пряталась за значком клавиатуры — человек её не видел."""
    from app.bot import phone_keyboard
    kb = phone_keyboard(load_funnel())
    assert kb.is_persistent is True
    assert kb.one_time_keyboard is not True
    assert kb.keyboard[0][0].request_contact is True
    assert not kb.input_field_placeholder  # подсказка в поле ввода выглядела как кнопка и путала (прогон 04.11)


def test_messages_do_not_promise_replies_bot_cannot_handle():
    """Сообщение «ответьте „хочу“» обещало то, чего бот не умеет: ответ уходил в пустоту."""
    import re
    for s in load_funnel().schedule:
        assert not re.search(r"ответьте|напишите|reply", s.text, re.I), s.key


def test_checklist_comes_by_button_not_typing():
    """Anto 04.11: чек-лист — кнопкой, «чтобы не писать пальчиками»."""
    steps = [s for s in load_funnel().schedule if s.button]
    assert steps and all(s.on_click for s in steps)


def test_demo_config_is_same_funnel_but_faster():
    """Для видео: та же воронка, второе сообщение через 20 с; основной config.yaml — по заданию (10 с и 60 с)."""
    from app.config import ROOT
    main, demo = load_funnel(), load_funnel(ROOT / "config.demo.yaml")
    assert [s.key for s in demo.schedule] == [s.key for s in main.schedule]
    assert [s.delay for s in demo.schedule if not s.after and s.before_webinar is None] == [10, 20]


def test_main_config_ties_live_messages_to_webinar_time():
    f = load_funnel()
    assert f.webinar_at is not None and f.webinar_at.utcoffset() is not None
    assert {s.key: s.before_webinar for s in f.schedule if s.before_webinar is not None} == {"live_soon": 600, "live_now": 0}
