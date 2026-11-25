"""Программы (акселераторы, инкубаторы, гранты): дедлайны с часовыми поясами, напоминания,
разбор страницы набора по ссылке, повтор на следующий год, экспорт в календарь (ICS)."""
import hashlib
import html as htmllib
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from . import ai
from .config import settings
from .db import (PROGRAM_OPEN, PROGRAM_STATUSES, STAGES, Card, Program, ProgramContact, ProgramFile, now)
from .tg import esc
from .work import TZ, WEEKDAYS, fmt_due, today

log = logging.getLogger("programs")

REMIND_DAYS = (10, 3, 1)  # за сколько дней до дедлайна напоминать, пока не подано

# Часовые пояса, в которых обычно пишут дедлайны
TIMEZONES = {
    "Asia/Tashkent": "Ташкент",
    "Europe/London": "Лондон (GMT/BST)",
    "Europe/Lisbon": "Лиссабон (WET)",
    "Europe/Berlin": "Центральная Европа (CET)",
    "Europe/Helsinki": "Восточная Европа (EET)",
    "Europe/Istanbul": "Стамбул",
    "Asia/Dubai": "Дубай (GST)",
    "Asia/Singapore": "Сингапур (SGT)",
    "America/New_York": "Нью-Йорк (ET)",
    "America/Los_Angeles": "Калифорния (PT)",
    "UTC": "UTC",
    "Etc/GMT+12": "AoE — «где угодно на Земле»",
}
TZ_ALIASES = {
    "cet": "Europe/Berlin", "cest": "Europe/Berlin", "europe/paris": "Europe/Berlin", "europe/amsterdam": "Europe/Berlin",
    "gmt": "Europe/London", "bst": "Europe/London", "wet": "Europe/Lisbon", "west": "Europe/Lisbon",
    "eet": "Europe/Helsinki", "eest": "Europe/Helsinki", "msk": "Europe/Istanbul", "trt": "Europe/Istanbul",
    "gst": "Asia/Dubai", "sgt": "Asia/Singapore", "et": "America/New_York", "est": "America/New_York",
    "edt": "America/New_York", "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "utc": "UTC", "z": "UTC", "aoe": "Etc/GMT+12", "anywhere on earth": "Etc/GMT+12", "uzt": "Asia/Tashkent",
}


def norm_tz(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if v in TIMEZONES:
        return v
    if v.lower() in TZ_ALIASES:
        return TZ_ALIASES[v.lower()]
    try:
        ZoneInfo(v)
        return v
    except Exception:  # noqa: BLE001
        return None


def norm_time(value: str | None) -> str | None:
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})", value or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def deadline_at(p: Program) -> datetime | None:
    """Момент дедлайна (с учётом времени и пояса организатора). Без времени — конец дня."""
    if not p.deadline_date:
        return None
    hh, mm = (p.deadline_time or "23:59").split(":")
    zone = ZoneInfo(p.deadline_tz or settings.timezone)
    return datetime.combine(p.deadline_date, time(int(hh), int(mm)), zone)


def local_deadline(p: Program) -> datetime | None:
    d = deadline_at(p)
    return d.astimezone(TZ) if d else None


def days_left(p: Program, today_: date | None = None) -> int | None:
    d = local_deadline(p)
    return (d.date() - (today_ or today())).days if d else None


def deadline_text(p: Program, today_: date | None = None) -> str:
    """«чт 05.12, 23:59 (Лиссабон) · у нас пт 06.12 04:59 · через 10 дн.»"""
    d = deadline_at(p)
    if not d:
        return "дедлайн не указан"
    t = today_ or today()
    own = f"{WEEKDAYS[d.weekday()]} {d:%d.%m}" + (f", {p.deadline_time}" if p.deadline_time else "")
    tz = p.deadline_tz or settings.timezone
    text = own
    if tz != settings.timezone:
        text += f" ({TIMEZONES.get(tz, tz).split(' (')[0]})"
        loc = d.astimezone(TZ)
        text += f" · у нас {WEEKDAYS[loc.weekday()]} {loc:%d.%m %H:%M}"
    left = days_left(p, t)
    if left is not None:
        text += " · " + ("сегодня" if left == 0 else "завтра" if left == 1 else
                         f"через {left} дн." if left > 0 else f"прошёл {-left} дн. назад")
    return text


def is_past(p: Program, at: datetime | None = None) -> bool:
    d = deadline_at(p)
    return bool(d and d < (at or now()))


# ---------- напоминания ----------

def due_reminders(s, at: datetime | None = None) -> list[tuple[Program, int]]:
    """Программы, по которым пора напомнить: не подано, до дедлайна ≤ 10/3/1 дней, это напоминание ещё не уходило.
    Отмечает напоминания отправленными (если пропустили 10, а осталось 2 — уйдёт одно, «за 3»)."""
    at = at or now()
    t = at.astimezone(TZ).date()
    out = []
    for p in s.scalars(select(Program).where(Program.status.in_(PROGRAM_OPEN), Program.deadline_date.is_not(None))):
        left = days_left(p, t)
        if left is None or left < 0 or is_past(p, at):
            continue
        sent = set(p.reminders_sent or [])
        due = [n for n in REMIND_DAYS if left <= n and n not in sent]
        if not due:
            continue
        p.reminders_sent = sorted(sent | set(due))
        out.append((p, left))  # сколько дней осталось на самом деле
    return out


def upcoming(s, days: int = 14, today_: date | None = None) -> list[Program]:
    t = today_ or today()
    items = [p for p in s.scalars(select(Program).where(Program.status.in_(PROGRAM_OPEN), Program.deadline_date.is_not(None)))
             if (dl := days_left(p, t)) is not None and 0 <= dl <= days]
    return sorted(items, key=lambda p: deadline_at(p))


# ---------- тексты и кнопки для бота ----------

def program_url(p: Program) -> str | None:
    return f"{settings.public_url}/programs/{p.id}" if settings.public_url.startswith("https://") else None


def program_text(s, p: Program, header: str | None = None) -> str:
    lines = [header] if header else []
    lines.append(f"🚀 <b>{esc(p.title)}</b> {esc(p.country_label)}".rstrip())
    lines.append(f"⏰ Заявки до: {deadline_text(p)}")
    lines.append(f"Статус: {PROGRAM_STATUSES.get(p.status, p.status)}")
    cond = [x for x in (p.funding and f"💵 {esc(p.funding)}", p.equity and f"доля: {esc(p.equity)}",
                        p.fee and f"участие: {esc(p.fee)}") if x]
    if cond:
        lines.append(" · ".join(cond))
    done, total = p.checklist_progress
    if total:
        lines.append(f"☑️ Чек-лист: {done}/{total}")
    for link in (p.links or [])[:3]:
        lines.append(f"🔗 <a href=\"{esc(link['url'])}\">{esc(link.get('label') or link['url'])}</a>")
    contacts = s.scalars(select(Card).join(ProgramContact, ProgramContact.card_id == Card.id)
                         .where(ProgramContact.program_id == p.id)).all()
    if contacts:
        lines.append("👥 " + ", ".join(f"{c.code} {esc(c.title)}" for c in contacts[:5]))
    if p.needs_review:
        lines.append("⚠️ Заполнено ИИ по ссылке — проверьте даты и условия.")
    lines.append("\n<i>Ответьте на это сообщение, чтобы добавить заметку к программе.</i>")
    return "\n".join(lines)


def program_buttons(p: Program) -> list:
    rows = [[(("✅ " if p.status == k else "") + PROGRAM_STATUSES[k], f"pg:{p.id}:{k}") for k in ("preparing", "submitted")],
            [(("✅ " if p.status == "skipped" else "") + PROGRAM_STATUSES["skipped"], f"pg:{p.id}:skipped")]]
    if p.needs_review:
        rows[1].append(("👌 Проверено", f"pgok:{p.id}"))
    url = program_url(p)
    if url:
        rows.append([("Открыть в CRM", url)])
    return rows


def deadlines_block(s, today_: date | None = None, days: int = 14) -> str:
    items = upcoming(s, days, today_)
    if not items:
        return ""
    lines = [f"\n🚀 <b>Дедлайны программ ({days} дн.):</b> {len(items)}"]
    for p in items[:8]:
        lines.append(f"• {esc(p.title)} {p.country_label} — {deadline_text(p, today_)} · {PROGRAM_STATUSES[p.status]}")
    return "\n".join(lines)


# ---------- добавление по ссылке ----------

class FetchError(Exception):
    pass


def fetch_page(url: str) -> str:
    """Текст страницы набора (без скриптов и разметки), не больше ~25 тыс. символов."""
    if not re.match(r"^https?://", url or ""):
        raise FetchError("нужна ссылка вида https://…")
    try:
        r = httpx.get(url, timeout=25, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; PartnersCRM/1.0)"})
    except httpx.HTTPError as e:
        raise FetchError(f"страница не открылась: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"страница ответила {r.status_code}")
    raw = r.text[:2_000_000]
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    links = " ".join(f"[{m.group(2).strip()[:60]}]({m.group(1)})" for m in
                     re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', raw)
                     if re.search(r"apply|application|form|заявк|typeform|tally|airtable|f6s", m.group(0), re.I))[:3000]
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", htmllib.unescape(text)).strip()
    return (text[:25000] + ("\nСсылки на заявку: " + links if links else "")).strip()


def apply_parsed(p: Program, d: dict) -> None:
    p.title = (d.get("title") or p.title or "Программа").strip()[:300]
    for f in ("country", "city", "funding", "equity", "fee", "duration", "description"):
        if d.get(f):
            setattr(p, f, str(d[f]).strip())
    if d.get("format") in ("online", "offline", "hybrid"):
        p.format = d["format"]
    p.deadline_date = _date(d.get("deadline_date")) or p.deadline_date
    p.deadline_time = norm_time(d.get("deadline_time")) or p.deadline_time
    p.deadline_tz = norm_tz(d.get("deadline_timezone")) or p.deadline_tz
    for f in ("results_date", "start_date", "demo_day"):
        if _date(d.get(f)):
            setattr(p, f, _date(d.get(f)))
    stages = [k for k in STAGES if k in (d.get("stages") or [])]
    if stages:
        p.stages = stages
    links = list(p.links or [])
    for link in d.get("links") or []:
        if link.get("url", "").startswith("http") and link["url"] not in {x["url"] for x in links}:
            links.append({"label": (link.get("label") or "")[:80], "url": link["url"]})
    p.links = links
    items = list(p.checklist or [])
    for text in d.get("requirements") or []:
        if text and text not in {i["text"] for i in items}:
            items.append({"text": text[:200], "done": False})
    p.checklist = items


def _date(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse_url(url: str) -> tuple[dict | None, str | None]:
    """Прочитать страницу и разобрать её ИИ — вне общего замка (это до минуты). (данные, ошибка)."""
    try:
        return ai.parse_program(fetch_page(url), url), None
    except Exception as e:  # noqa: BLE001
        log.warning("program parse failed for %s: %s", url, e)
        return None, str(e)[:200]


def create_from_url(s, url: str, author: str | None, parsed: tuple | None = None) -> Program:
    """Программа по ссылке на страницу набора. Если страница не читается — программа со ссылкой и пометкой."""
    data, error = parsed if parsed is not None else parse_url(url)
    p = Program(title=url.split("//", 1)[-1].split("/")[0], source_url=url, created_by=author, status="idea",
                needs_review=True, links=[{"label": "Страница набора", "url": url}])
    if data:
        apply_parsed(p, data)
    else:
        p.description = (f"Не удалось прочитать страницу автоматически ({error}). "
                         "Часто так бывает с формами Typeform/Notion — заполните вручную.")
    s.add(p)
    s.flush()
    return p


def run_from_url(url: str, chat_id: int, reply_to: int | None, author: str | None, tg, lock) -> None:
    """Фоновая задача бота: /program ссылка."""
    from .db import SessionLocal
    from .crm import link
    parsed = parse_url(url)
    with lock, SessionLocal() as s:
        p = create_from_url(s, url, author, parsed)
        s.commit()
        if tg:
            header = "✅ Программа добавлена — проверьте:" if parsed[0] else "⚠️ Страницу прочитать не удалось — программа создана со ссылкой, заполните на сайте:"
            sent = tg.send(chat_id, program_text(s, p, header), reply_to=reply_to, buttons=program_buttons(p))
            link(s, chat_id, sent["message_id"], "program", program_id=p.id)
            s.commit()


# ---------- повтор на следующий год ----------

def _plus_year(d: date | None) -> date | None:
    if not d:
        return None
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29 февраля
        return d.replace(year=d.year + 1, day=28)


def repeat_next_year(s, p: Program, author: str | None) -> Program:
    new = Program(title=p.title, country=p.country, city=p.city, deadline_time=p.deadline_time, deadline_tz=p.deadline_tz,
                  funding=p.funding, equity=p.equity, fee=p.fee, duration=p.duration, format=p.format, stages=p.stages,
                  links=p.links, description=p.description, source_url=p.source_url, created_by=author, status="idea",
                  checklist=[{"text": i["text"], "done": False} for i in (p.checklist or [])],
                  deadline_date=_plus_year(p.deadline_date), results_date=_plus_year(p.results_date),
                  start_date=_plus_year(p.start_date), demo_day=_plus_year(p.demo_day),
                  repeated_from=p.id, needs_review=True)
    s.add(new)
    s.flush()
    for pc in s.scalars(select(ProgramContact).where(ProgramContact.program_id == p.id)):
        s.add(ProgramContact(program_id=new.id, card_id=pc.card_id, role=pc.role))
    for pf in s.scalars(select(ProgramFile).where(ProgramFile.program_id == p.id)):
        s.add(ProgramFile(program_id=new.id, attachment_id=pf.attachment_id))
    return new


# ---------- календарь (ICS) ----------

def ics_token() -> str:
    """Секрет в ссылке подписки: календарные приложения не умеют входить по паролю."""
    return hashlib.sha256(("ics:" + settings.secret_key).encode()).hexdigest()[:32]


def _ics_text(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(programs: list[Program]) -> str:
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Partners CRM//Programs//RU", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:Программы — дедлайны", "X-WR-TIMEZONE:" + settings.timezone]
    for p in programs:
        url = program_url(p) or ""
        d = deadline_at(p)
        if d and p.status != "skipped":
            start = d.astimezone(timezone.utc)
            out += ["BEGIN:VEVENT", f"UID:program-{p.id}-deadline@partners-crm", f"DTSTAMP:{stamp}",
                    f"DTSTART:{(start - timedelta(minutes=30)):%Y%m%dT%H%M%SZ}", f"DTEND:{start:%Y%m%dT%H%M%SZ}",
                    "SUMMARY:" + _ics_text(f"⏰ Дедлайн: {p.title} {p.country_label}".strip()),
                    "DESCRIPTION:" + _ics_text(f"Статус: {PROGRAM_STATUSES.get(p.status)}\n{url}"), f"URL:{url}",
                    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-P3D", "DESCRIPTION:Дедлайн через 3 дня", "END:VALARM",
                    "END:VEVENT"]
        for field, icon, name in (("results_date", "📣", "Результаты"), ("start_date", "🏁", "Старт программы"),
                                  ("demo_day", "🎤", "Demo Day")):
            day = getattr(p, field)
            if day:
                out += ["BEGIN:VEVENT", f"UID:program-{p.id}-{field}@partners-crm", f"DTSTAMP:{stamp}",
                        f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
                        "SUMMARY:" + _ics_text(f"{icon} {name}: {p.title}"), f"URL:{url}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    # строки ICS не длиннее 75 байт — переносим
    folded = []
    for line in out:
        b = line.encode()
        while len(b) > 74:
            cut = 74
            while (b[cut] & 0xC0) == 0x80:  # не резать посреди символа UTF-8
                cut -= 1
            folded.append(b[:cut].decode())
            b = b" " + b[cut:]
        folded.append(b.decode())
    return "\r\n".join(folded) + "\r\n"


def month_grid(year: int, month: int) -> list[list[date]]:
    """Недели месяца (пн–вс), включая дни соседних месяцев по краям."""
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    weeks = []
    d = start
    while True:
        weeks.append([d + timedelta(days=i) for i in range(7)])
        d += timedelta(days=7)
        if d.month != month and d > first:
            break
    return weeks


def calendar_events(programs: list[Program]) -> dict[date, list[dict]]:
    events: dict[date, list[dict]] = {}
    for p in programs:
        loc = local_deadline(p)
        if loc:
            events.setdefault(loc.date(), []).append({"p": p, "kind": "deadline", "icon": "⏰"})
        for field, icon in (("results_date", "📣"), ("start_date", "🏁"), ("demo_day", "🎤")):
            if getattr(p, field):
                events.setdefault(getattr(p, field), []).append({"p": p, "kind": field, "icon": icon})
    return events


__all__ = ["REMIND_DAYS", "TIMEZONES", "deadline_text", "due_reminders", "program_text", "program_buttons",
           "create_from_url", "repeat_next_year", "ics", "ics_token", "fmt_due"]
