"""Веб-интерфейс CRM: список, карточка, правка, заметки, экспорт CSV."""
import csv
import hmac
import io
from datetime import date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import countries, crm, translate, work
from .config import settings
from .db import (BALLS, FIELD_LABELS, GENDER_UNKNOWN, GENDERS, STAGES, LIST_FIELDS, STATUSES, TAG_GROUPS, TEXT_FIELDS, UNTRIAGED, Attachment, Card, Note, SessionLocal,
                 now)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(STATUSES=STATUSES, FIELD_LABELS=FIELD_LABELS, TAG_GROUPS=TAG_GROUPS, BALLS=BALLS,
                             GENDERS=GENDERS, GENDER_UNKNOWN=GENDER_UNKNOWN, STAGES=STAGES,
                             UNTRIAGED=UNTRIAGED, fmt_due=work.fmt_due, followup_date=work.followup_date)
TZ = ZoneInfo(settings.timezone)


def _dt(value, fmt="%d.%m.%Y %H:%M"):
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(TZ).strftime(fmt)


templates.env.filters["dt"] = _dt


class LoginRequired(Exception):
    pass


def user_of(request: Request) -> str:
    user = request.session.get("user")
    if not user or user not in settings.web_users:
        raise LoginRequired()
    return user


def render(request, name, **ctx):
    return templates.TemplateResponse(request, name, {"user": request.session.get("user"), **ctx})


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    expected = settings.web_users.get(username.strip())
    if expected and hmac.compare_digest(expected, password):
        request.session["user"] = username.strip()
        return RedirectResponse("/", 303)
    return render(request, "login.html", error="Неверный логин или пароль")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


def authors(s) -> list[str]:
    return sorted({a for (a,) in s.execute(select(Card.created_by).distinct()) if a}, key=str.lower)


PERIODS = {"today": ("Сегодня", 0, 0), "yesterday": ("Вчера", 1, 1), "7d": ("7 дней", 6, 0), "30d": ("30 дней", 29, 0)}


def date_range(period: str, date_from: str, date_to: str) -> tuple:
    """Период добавления: быстрая кнопка или «с — по» (даты по местному времени, включительно)."""
    if period in PERIODS:
        t = work.today()
        _, back_from, back_to = PERIODS[period]
        return t - timedelta(days=back_from), t - timedelta(days=back_to)
    return work.parse_due(date_from), work.parse_due(date_to)


templates.env.globals["PERIODS"] = {k: v[0] for k, v in PERIODS.items()}


def by_added(cards: list, start, end) -> list:
    if start:
        cards = [c for c in cards if work.local_date(c.created_at) >= start]
    if end:
        cards = [c for c in cards if work.local_date(c.created_at) <= end]
    return cards


def country_options(cards: list) -> list[tuple[str, str, int]]:
    """Страны, которые есть в базе: (код, «🇬🇧 Великобритания», сколько контактов) — по убыванию."""
    counts: dict[str, int] = {}
    for c in cards:
        code = c.country_info[0]
        if code:
            counts[code] = counts.get(code, 0) + 1
    return sorted(((k, countries.label(k), n) for k, n in counts.items()), key=lambda x: (-x[2], x[1]))


SORTS = {
    "new": "Новые сверху",
    "old": "Старые сверху",
    "stale": "Давно не трогали — сверху",
    "fresh": "Свежая активность — сверху",
    "due": "Срок шага: просроченные и ближайшие",
    "temp": "Температура: 🔥 → ❄️",
    "status": "Статус воронки",
    "notes": "Больше заметок — сверху",
    "incomplete": "Неполные — сверху",
    "complete": "Полные — сверху",
}
STATUS_ORDER = {"agreed": 0, "in_progress": 1, "new": 2, "postponed": 3, "rejected": 4}
TEMP_ORDER = {"hot": 0, "warm": 1, "cold": 2}
# Что считаем «заполненностью» контакта (8 пунктов)
COMPLETENESS = (("имя", lambda c: c.full_name), ("компания", lambda c: c.company), ("должность", lambda c: c.position),
                ("телефон", lambda c: c.phones), ("email", lambda c: c.emails),
                ("мессенджер/соцсеть", lambda c: c.socials), ("страна", lambda c: c.country_info[0]),
                ("о контакте", lambda c: c.about))


def completeness(card) -> tuple[int, list[str]]:
    """(сколько заполнено из 8, чего не хватает)."""
    missing = [name for name, get in COMPLETENESS if not get(card)]
    return len(COMPLETENESS) - len(missing), missing


def last_activity(s) -> dict:
    """Последняя заметка или касание по контакту (системные отметки CRM не считаются)."""
    return dict(s.execute(select(Note.card_id, func.max(Note.created_at))
                          .where(Note.card_id.is_not(None), Note.kind != "system").group_by(Note.card_id)).all())


def _ts(dt) -> float:
    if dt is None:
        return 0.0
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def sort_cards(cards: list, sort: str, counts: dict, activity: dict) -> list:
    added = lambda c: _ts(c.created_at)  # noqa: E731
    act = lambda c: max(_ts(activity.get(c.id)), added(c))  # noqa: E731 — без заметок: дата добавления
    keys = {
        "new": lambda c: -added(c),
        "old": lambda c: added(c),
        "stale": lambda c: act(c),
        "fresh": lambda c: -act(c),
        "due": lambda c: (c.due is None, c.due or date.max, -added(c)),
        "temp": lambda c: (TEMP_ORDER.get(c.temp, 3), -added(c)),
        "status": lambda c: (STATUS_ORDER.get(c.status, 5), -added(c)),
        "notes": lambda c: (-counts.get(c.id, 0), -added(c)),
        "incomplete": lambda c: (completeness(c)[0], -added(c)),
        "complete": lambda c: (-completeness(c)[0], -added(c)),
    }
    return sorted(cards, key=keys.get(sort, keys["new"]))


def filter_defs(s) -> list[dict]:
    """Все фильтры списка и страницы «Работа». В каждом можно выбрать несколько значений:
    внутри фильтра — «или», между фильтрами — «и». Значение none — «не задано»."""
    all_cards = list(s.scalars(select(Card)))
    defs = [
        {"key": "status", "title": "Статус", "options": list(STATUSES.items()), "get": lambda c: c.status},
        *[{"key": g, "title": title, "options": [*values.items(), ("none", "не задано")], "get": (lambda g: lambda c: getattr(c, g))(g)}
          for g, (title, values) in TAG_GROUPS.items()],
        {"key": "gender", "title": "Пол", "options": [*GENDERS.items(), ("none", GENDER_UNKNOWN)], "get": lambda c: c.gender},
        {"key": "country", "title": "Страна",
         "options": [(code, f"{label} ({n})") for code, label, n in country_options(all_cards)] + [("none", "не указана")],
         "get": lambda c: c.country_info[0]},
        {"key": "stage", "title": "Стадия", "options": [*STAGES.items(), ("none", "инвестор без стадии")],
         "match": lambda c, sel: bool(sel & set(c.stages or [])) or ("none" in sel and c.kind == "investor" and not c.stages)},
        {"key": "author", "title": "Добавил", "options": [(a, a) for a in authors(s)] + [("none", "не указано")],
         "get": lambda c: c.created_by},
    ]
    return defs


def selected_filters(request: Request, defs: list[dict]) -> dict[str, set]:
    sel = {}
    for d in defs:
        allowed = {v for v, _ in d["options"]}
        values = {v for v in request.query_params.getlist(d["key"]) if v in allowed}
        if values:
            sel[d["key"]] = values
    return sel


def apply_filters(cards: list, defs: list[dict], sel: dict[str, set]) -> list:
    for d in defs:
        values = sel.get(d["key"])
        if not values:
            continue
        if "match" in d:
            cards = [c for c in cards if d["match"](c, values)]
        else:
            cards = [c for c in cards if (d["get"](c) or "none") in values]
    return cards


def filter_summary(d: dict, values: set) -> str:
    labels = [label for v, label in d["options"] if v in values]
    if not labels:
        return f"{d['title']}: все"
    shown = ", ".join(labels[:2])
    return f"{d['title']}: {shown}" + (f" +{len(labels) - 2}" if len(labels) > 2 else "")


templates.env.globals["filter_summary"] = filter_summary


@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", batch: str = "", period: str = "", date_from: str = "", date_to: str = "",
          sort: str = ""):
    user_of(request)
    with SessionLocal() as s:
        defs = filter_defs(s)
        sel = selected_filters(request, defs)
        cards = crm.search(s, q, limit=10_000) if q else list(s.scalars(select(Card).order_by(Card.created_at.desc())))
        if batch:
            cards = [c for c in cards if c.batch == batch]
        cards = apply_filters(cards, defs, sel)
        start, end = date_range(period, date_from, date_to)
        cards = by_added(cards, start, end)
        counts = dict(s.execute(select(Note.card_id, func.count()).group_by(Note.card_id)).all())
        # сортировка запоминается для этого входа (сессия), пока не выберут другую
        if sort in SORTS:
            request.session["sort"] = sort
        sort = sort if sort in SORTS else request.session.get("sort", "new")
        activity = last_activity(s)
        cards = sort_cards(cards, sort, counts, activity)
        thumbs = dict(s.execute(select(Attachment.card_id, func.min(Attachment.id))
                                .where(Attachment.kind == "card").group_by(Attachment.card_id)).all())
    return render(request, "index.html", cards=cards, q=q, counts=counts, thumbs=thumbs, batch=batch,
                  defs=defs, sel=sel, period=period, sort=sort, SORTS=SORTS, activity=activity,
                  completeness=completeness, today=work.today(),
                  date_from=start.isoformat() if start else "", date_to=end.isoformat() if end else "")


@router.get("/work", response_class=HTMLResponse)
def work_page(request: Request, period: str = "", date_from: str = "", date_to: str = ""):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        if work.expire_waiting(s):
            s.commit()
        data = work.todo(s)
        defs = filter_defs(s)
    sel = selected_filters(request, defs)
    start, end = date_range(period, date_from, date_to)
    for key in ("overdue", "due_today", "later", "waiting", "none", "untriaged"):
        data[key] = by_added(apply_filters(data[key], defs, sel), start, end)
    return render(request, "work.html", d=data, today=data["today"], defs=defs, sel=sel, period=period,
                  date_from=start.isoformat() if start else "", date_to=end.isoformat() if end else "")


def _back(request: Request, default: str) -> str:
    ref = request.headers.get("referer") or ""
    path = ref.split("://", 1)[-1].split("/", 1)[-1] if "://" in ref else ""
    return "/" + path if path.startswith(("work", "c/")) else default


@router.post("/c/{card_id}/work")
async def save_work(request: Request, card_id: int):
    """Мяч, шаг, срок — из карточки на сайте; «done» — шаг выполнен; быстрые кнопки со страницы «Работа»."""
    user = user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            raise HTTPException(404)
        action = form.get("action")
        if action == "done":
            work.step_done(s, card, user, source="web")
            work.set_ball(card, form.get("then") or "them")
        elif action == "touch":
            text = (form.get("touch") or "").strip()
            if text:
                work.log_touch(s, card, text, user, source="web")
        else:
            prev_ball, prev_due = card.ball, card.due
            if "ball" in form:
                work.set_ball(card, form.get("ball") or None)
            if "next_step" in form:
                card.next_step = (form.get("next_step") or "").strip() or None
            if "due" in form:
                due = work.parse_due(form.get("due"))
                # переключили на «ждём их», а срок в форме остался прежним (нашим) — не переносим его
                if not (card.ball == "them" and prev_ball != "them" and due == prev_due):
                    card.due = due
            if card.next_step and card.ball in (None, "none") and "ball" not in form:
                work.set_ball(card, "us")
        card.updated_at = now()
        s.commit()
    crm.schedule_card_summary(card_id)
    return RedirectResponse(_back(request, f"/c/{card_id}"), 303)


@router.get("/c/new", response_class=HTMLResponse)
def new_form(request: Request):
    user_of(request)
    return render(request, "new.html")


@router.post("/c/new")
async def create(request: Request, name: str = Form(""), company: str = Form(""), photo: UploadFile | None = File(None)):
    user = user_of(request)
    data = await photo.read() if photo and photo.filename else b""
    if not (name.strip() or company.strip() or data):
        return RedirectResponse("/c/new", 303)
    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {settings.max_file_mb} МБ")
    with crm.LOCK, SessionLocal() as s:
        edited = [f for f, v in (("full_name", name), ("company", company)) if v.strip()]
        card = Card(full_name=name.strip() or None, company=company.strip() or None, source="web",
                    created_by=user, edited_fields=edited, ocr_status="processing" if data else "none")
        s.add(card)
        s.flush()
        att_id = crm.add_attachment(s, card.id, "card", data, photo.filename, photo.content_type).id if data else None
        s.commit()
        card_id = card.id
    if att_id:
        crm.start_ocr(card_id, [att_id])
    return RedirectResponse(f"/c/{card_id}", 303)


@router.get("/c/{card_id}", response_class=HTMLResponse)
def card_page(request: Request, card_id: int):
    user_of(request)
    with SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            raise HTTPException(404)
        atts = list(s.scalars(select(Attachment).where(Attachment.card_id == card_id).order_by(Attachment.id)))
        notes = list(s.scalars(select(Note).where(Note.card_id == card_id).order_by(Note.created_at.desc())))
        att_by_id = {a.id: a for a in atts}
        similar = crm.similar(s, card)
        from .web_programs import programs_for_card
        linked_programs = programs_for_card(s, card_id)
    photos = [a for a in atts if a.kind == "card"]
    return render(request, "card.html", card=card, photos=photos, notes=notes, att_by_id=att_by_id,
                  similar=similar, TEXT_FIELDS=TEXT_FIELDS, LIST_FIELDS=LIST_FIELDS, linked_programs=linked_programs)


@router.post("/c/{card_id}")
async def save_card(request: Request, card_id: int):
    user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            raise HTTPException(404)
        edited = set(card.edited_fields or [])
        for f in TEXT_FIELDS:
            if f not in form:  # поле не пришло в форме — не трогаем (а не стираем)
                continue
            v = (form.get(f) or "").strip() or None
            if v != getattr(card, f):
                setattr(card, f, v)
                edited.add(f)
        for f in LIST_FIELDS:
            if f not in form:
                continue
            v = [x.strip() for x in (form.get(f) or "").splitlines() if x.strip()]
            if v != list(getattr(card, f) or []):
                setattr(card, f, v)
                edited.add(f)
        if form.get("status") in STATUSES:
            card.status = form["status"]
        card.edited_fields = sorted(edited)
        if "stages_form" in form:  # отметки стадий (чекбоксы): пришедший набор — и есть выбор
            chosen = set(form.getlist("stages"))
            card.stages = [k for k in STAGES if k in chosen] or None
        if "gender" in form and (form.get("gender") or None) != card.gender:
            card.set_gender(form.get("gender") or None, by_user=True)
        # теги: меняем и закрепляем только то, что человек действительно поменял в форме
        card.set_tags({g: form.get(g) or None for g in TAG_GROUPS
                       if g in form and (form.get(g) or None) != getattr(card, g)}, by_user=True)
        card.unreadable = [f for f in card.unreadable or [] if f not in edited]
        s.commit()
    translate.enqueue(card_id)  # изменённые поля — перевести, если они не на русском
    return RedirectResponse(f"/c/{card_id}", 303)


@router.post("/c/{card_id}/note")
async def add_note(request: Request, card_id: int, text: str = Form(""), file: UploadFile | None = File(None)):
    user = user_of(request)
    data = await file.read() if file and file.filename else b""
    if not text.strip() and not data:
        return RedirectResponse(f"/c/{card_id}", 303)
    with crm.LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            raise HTTPException(404)
        note = Note(card_id=card_id, text=text.strip() or None, author=user, source="web")
        att = None
        if data:
            kind = "voice" if (file.content_type or "").startswith("audio/") else "file"
            att = crm.add_attachment(s, card_id, kind, data, file.filename, file.content_type)
            note.attachment_id = att.id
        ai_needed = crm.needs_ai(note, att)
        if ai_needed:
            note.ai_status = "processing"
        s.add(note)
        card.updated_at = now()
        s.commit()
        note_id = note.id
    if ai_needed:
        crm.start_note_ai(note_id)
    else:
        crm.schedule_card_summary(card_id)
    return RedirectResponse(f"/c/{card_id}", 303)


@router.post("/n/{note_id}/ai")
def rerun_note_ai(request: Request, note_id: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None or note.card_id is None:
            raise HTTPException(404)
        note.ai_status = "processing"
        s.commit()
        card_id = note.card_id
    crm.start_note_ai(note_id)
    return RedirectResponse(f"/c/{card_id}", 303)


@router.post("/c/{card_id}/summary")
def rebuild_summary(request: Request, card_id: int):
    user_of(request)
    crm.schedule_card_summary(card_id)
    return RedirectResponse(f"/c/{card_id}", 303)


@router.post("/c/{card_id}/ocr")
def rerun_ocr(request: Request, card_id: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            raise HTTPException(404)
        card.ocr_status = "processing"
        s.commit()
    crm.start_ocr(card_id, None)
    return RedirectResponse(f"/c/{card_id}", 303)


@router.post("/batch/{batch}/source")
def batch_source(request: Request, batch: str, to: str = Form(...)):
    user_of(request)
    if to not in crm.SOURCE_NAMES:
        raise HTTPException(400)
    with crm.LOCK, SessionLocal() as s:
        crm.convert_batch(s, batch, to)
        s.commit()
    return RedirectResponse(f"/?batch={batch}", 303)


@router.post("/c/{card_id}/delete")
def delete_card(request: Request, card_id: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card:
            s.delete(card)
            s.commit()
    return RedirectResponse("/", 303)


@router.get("/f/{att_id}")
def file(request: Request, att_id: int):
    user_of(request)
    with SessionLocal() as s:
        att = s.get(Attachment, att_id)
        if att is None:
            raise HTTPException(404)
        data = att.data
    name = (att.filename or f"file-{att_id}").encode("ascii", "ignore").decode() or f"file-{att_id}"
    return Response(data, media_type=att.mime or "application/octet-stream",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "private, max-age=86400"})


@router.get("/export.csv")
def export(request: Request):
    user_of(request)
    with SessionLocal() as s:
        cards = list(s.scalars(select(Card).order_by(Card.id)))
        notes: dict[int, list[str]] = {}
        for n in s.scalars(select(Note).where(Note.card_id.is_not(None)).order_by(Note.created_at)):
            body = n.summary or n.text or n.transcript
            if body:
                notes.setdefault(n.card_id, []).append(f"[{_dt(n.created_at, '%d.%m %H:%M')} {n.author}] {body}")
    buf = io.StringIO()
    buf.write("﻿")  # чтобы Excel открыл UTF-8 с кириллицей
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Имя", "Должность", "Компания", "Телефоны", "Email", "Сайт", "Адрес", "Страна",
                "Соцсети", "Стадии инвестирования", "О контакте", "О контакте (рус)", "Должность (рус)", "Страна (определена)", "Пол", "Тип", "Температура", "Откуда", "Статус", "Добавил", "Дата", "Сводка", "Заметки"])
    for c in cards:
        w.writerow([c.code, c.full_name, c.position, c.company, "\n".join(c.phones or []), "\n".join(c.emails or []),
                    c.website, c.address, c.country, "\n".join(c.socials or []), ", ".join(c.stage_labels), c.about, c.ru("about") or "", c.ru("position") or "",
                    countries.NAMES.get(c.country_info[0], "") + (" (по номеру)" if c.country_info[1] == "phone" else ""),
                    GENDERS.get(c.gender, "").split(" ", 1)[-1],
                    *[TAG_GROUPS[g][1].get(getattr(c, g), "") for g in TAG_GROUPS], STATUSES.get(c.status, c.status),
                    c.created_by, _dt(c.created_at, "%d.%m.%Y"), c.summary, "\n".join(notes.get(c.id, []))])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="partners-{now():%Y%m%d}.csv"'})
