"""Обработка контактов: «мяч» (кто действует), следующий шаг, срок, касания, дожим и утренняя сводка."""
import logging
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .config import settings
from .db import BALLS, UNTRIAGED, AppState, Card, Note, SessionLocal, now
from .tg import esc

log = logging.getLogger("work")

TZ = ZoneInfo(settings.timezone)
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
TEMP_RANK = {"hot": 0, "warm": 1, "cold": 2}
REMIND = "Напомнить о себе"


def today() -> date:
    return datetime.now(TZ).date()


def local_date(dt: datetime) -> date:
    if dt.tzinfo is None:
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ).date()


def fmt_due(d: date | None, today_: date | None = None) -> str:
    if d is None:
        return ""
    t = today_ or today()
    if d == t:
        return "сегодня"
    if d == t + timedelta(days=1):
        return "завтра"
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def ball_label(card: Card) -> str:
    return BALLS.get(card.ball, UNTRIAGED)


def set_ball(card: Card, ball: str | None) -> None:
    if ball not in (None, *BALLS):
        return
    if ball == "them" and card.ball != "them":
        card.waiting_since = now()
        card.due = None  # прежний срок был нашим; их срок (если назван) ставится отдельно, иначе — правило N дней
    elif ball != "them":
        card.waiting_since = None
    if ball == "none":
        card.due = None
    card.ball = ball


def followup_date(card: Card) -> date | None:
    """Когда мяч сам вернётся к нам, если мы ждём ответа."""
    if card.ball != "them":
        return None
    if card.due:
        return card.due + timedelta(days=1)
    if card.waiting_since:
        return local_date(card.waiting_since) + timedelta(days=settings.followup_days)
    return None


def work_line(card: Card, today_: date | None = None) -> str:
    """Строка для текста карточки в боте: шаг и срок (мяч виден по ✅ на кнопках)."""
    parts = []
    if card.next_step:
        parts.append(f"🎯 {esc(card.next_step)}")
    if card.due:
        t = today_ or today()
        parts.append(("🔴 просрочено: " if card.due < t and card.ball == "us" else "📅 ") + fmt_due(card.due, t))
    if card.ball == "them":
        back = followup_date(card)
        if back:
            parts.append(f"⏳ ждём, вернётся к нам {fmt_due(back, today_)}")
    return " · ".join(parts)


def parse_due(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def apply_work(card: Card, w: dict | None) -> list[str]:
    """Шаг/срок/мяч, названные в заметке прямо (их разбирает ИИ). Возвращает строки для ответа бота."""
    if not w:
        return []
    changes = []
    step, due, ball = (w.get("next_step") or "").strip() or None, parse_due(w.get("due")), w.get("ball")
    if ball in BALLS and ball != card.ball:
        set_ball(card, ball)
        changes.append(f"⚽ Мяч: {BALLS[ball]}")
    elif ball is None and step and card.ball in (None, "none"):
        set_ball(card, "us")  # назван следующий шаг — значит, есть что делать
        changes.append(f"⚽ Мяч: {BALLS['us']}")
    if step and step != card.next_step:
        card.next_step = step
        changes.append(f"🎯 Шаг: {esc(step)}")
    if due and due != card.due:
        card.due = due
        changes.append(f"📅 Срок: {fmt_due(due)}")
    return changes


def log_touch(s, card: Card, text: str, author: str | None, source: str = "bot") -> Note:
    note = Note(card_id=card.id, text=text, touch=text, kind="touch", author=author, source=source, ai_status="done")
    s.add(note)
    card.updated_at = now()
    return note


def step_done(s, card: Card, author: str | None, source: str = "bot") -> Note:
    """Отметить текущий шаг выполненным: касание в историю, шаг очищается. Что дальше — решает человек."""
    note = log_touch(s, card, f"✅ {card.next_step or 'Шаг выполнен'}", author, source)
    card.next_step, card.due = None, None
    return note


# ---------- дожим: мяч возвращается к нам ----------

def expire_waiting(s, today_: date | None = None) -> list[Card]:
    t = today_ or today()
    back = []
    for card in s.scalars(select(Card).where(Card.ball == "them")):
        fd = followup_date(card)
        if fd is None or fd > t:
            continue
        waited = card.next_step
        set_ball(card, "us")
        card.next_step = REMIND + (f" (ждали: {waited})" if waited and not waited.startswith(REMIND) else "")
        card.due = t
        s.add(Note(card_id=card.id, kind="system", source="system", author="CRM", ai_status="done",
                   text=f"⏰ Ответа нет — мяч вернулся к нам: {REMIND.lower()}"))
        back.append(card)
    return back


# ---------- очередь дел ----------

def _us_key(c: Card, t: date):
    return (c.due or date.max, TEMP_RANK.get(c.temp, 3), -(c.updated_at.timestamp() if c.updated_at else 0))


def todo(s, today_: date | None = None) -> dict:
    t = today_ or today()
    cards = list(s.scalars(select(Card)))
    us = sorted([c for c in cards if c.ball == "us"], key=lambda c: _us_key(c, t))
    return {
        "today": t,
        "overdue": [c for c in us if c.due and c.due < t],
        "due_today": [c for c in us if c.due == t],
        "later": [c for c in us if not c.due or c.due > t],
        "waiting": sorted([c for c in cards if c.ball == "them"], key=lambda c: followup_date(c) or date.max),
        "none": [c for c in cards if c.ball == "none"],
        "untriaged": sorted([c for c in cards if c.ball is None], key=lambda c: (TEMP_RANK.get(c.temp, 3), -c.id)),
    }


def _item(c: Card, t: date) -> str:
    temp = {"hot": "🔥 ", "warm": "🌤 ", "cold": ""}.get(c.temp, "")
    step = f" — {esc(c.next_step)}" if c.next_step else ""
    due = ""
    if c.due and c.ball == "us":
        due = f" <b>(просрочено {(t - c.due).days} дн.)</b>" if c.due < t else ("" if c.due == t else f" · {fmt_due(c.due, t)}")
    return f"{temp}<b>{c.code}</b> {esc(c.title)}{step}{due}"


def todo_message(t: dict, title: str = "📋 Дела по контактам") -> tuple[str, list]:
    d = t["today"]
    us_now = t["overdue"] + t["due_today"]
    lines = [f"<b>{title}</b> · {WEEKDAYS_FULL[d.weekday()]}, {d:%d.%m}"]
    lines.append(f"🟢 Наш ход: {len(us_now) + len(t['later'])}" + (f" (🔴 просрочено: {len(t['overdue'])})" if t["overdue"] else ""))
    show = (us_now + t["later"])[:12]
    lines += [_item(c, d) for c in show]
    if len(us_now) + len(t["later"]) > len(show):
        lines.append(f"… и ещё {len(us_now) + len(t['later']) - len(show)} — все на сайте, страница «Работа»")
    if t["waiting"]:
        soon = [c for c in t["waiting"] if (followup_date(c) or date.max) <= d + timedelta(days=1)]
        lines.append(f"\n🟡 Ждём ответа: {len(t['waiting'])}" + (f", вернутся к нам завтра: {len(soon)}" if soon else ""))
    if t["untriaged"]:
        lines.append(f"📥 Не разобраны: {len(t['untriaged'])} — решите, кто действует (кнопки под карточкой или «Работа» на сайте)")
    if not (us_now or t["later"] or t["waiting"] or t["untriaged"]):
        lines.append("Всё разобрано, дел нет 🎉")
    buttons = [[(f"{c.code} {c.title}"[:60], f"c:{c.id}")] for c in show]
    if settings.public_url.startswith("https://"):
        buttons.append([("Открыть «Работа» в CRM", f"{settings.public_url}/work")])
    return "\n".join(lines), buttons


# ---------- фоновый цикл: дожим и сводка в 9:00 по будням ----------

def _state(s, key: str) -> str | None:
    row = s.get(AppState, key)
    return row.value if row else None


def _set_state(s, key: str, value: str) -> None:
    row = s.get(AppState, key)
    if row is None:
        s.add(AppState(key=key, value=value))
    else:
        row.value = value


def tick(tg, lock, at: datetime | None = None) -> bool:
    """Один проход: вернуть просроченные «ждём их»; в 9:00+ по будням — разослать сводку (раз в день).
    Возвращает True, если сводка отправлена."""
    at = at or datetime.now(TZ)
    t = at.date()
    with lock, SessionLocal() as s:
        expire_waiting(s, t)
        s.commit()
        if at.weekday() >= 5 or at.hour < settings.digest_hour or _state(s, "digest_date") == t.isoformat():
            return False
        _set_state(s, "digest_date", t.isoformat())
        s.commit()
        data = todo(s, t)
        if not (data["overdue"] or data["due_today"] or data["later"] or data["waiting"]):
            return False  # нечего делать — не беспокоим
        text, buttons = todo_message(data, "☀️ Доброе утро! Дела на сегодня")
    for chat_id in settings.allowed_ids:
        try:
            tg.send(chat_id, text, buttons=buttons)
        except Exception:  # noqa: BLE001
            log.exception("cannot send digest to %s", chat_id)
    return True


def start_loop(tg, lock) -> None:
    def loop():
        while True:
            try:
                tick(tg, lock)
            except Exception:  # noqa: BLE001
                log.exception("work tick failed")
            time.sleep(300)

    threading.Thread(target=loop, daemon=True, name="work-loop").start()
