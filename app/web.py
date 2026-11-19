"""Веб-интерфейс CRM: список, карточка, правка, заметки, экспорт CSV."""
import csv
import hmac
import io
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import countries, crm, translate, work
from .config import settings
from .db import (BALLS, FIELD_LABELS, GENDER_UNKNOWN, GENDERS, LIST_FIELDS, STATUSES, TAG_GROUPS, TEXT_FIELDS, UNTRIAGED, Attachment, Card, Note, SessionLocal,
                 now)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(STATUSES=STATUSES, FIELD_LABELS=FIELD_LABELS, TAG_GROUPS=TAG_GROUPS, BALLS=BALLS,
                             GENDERS=GENDERS, GENDER_UNKNOWN=GENDER_UNKNOWN,
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


def by_country(cards: list, country: str) -> list:
    if country == "none":
        return [c for c in cards if not c.country_info[0]]
    return [c for c in cards if c.country_info[0] == country] if country else cards


def country_options(cards: list) -> list[tuple[str, str, int]]:
    """Страны, которые есть в базе: (код, «🇬🇧 Великобритания», сколько контактов) — по убыванию."""
    counts: dict[str, int] = {}
    for c in cards:
        code = c.country_info[0]
        if code:
            counts[code] = counts.get(code, 0) + 1
    return sorted(((k, countries.label(k), n) for k, n in counts.items()), key=lambda x: (-x[2], x[1]))


def by_author(cards: list, author: str) -> list:
    if author == "none":
        return [c for c in cards if not c.created_by]
    return [c for c in cards if c.created_by == author] if author else cards


@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", status: str = "", kind: str = "", temp: str = "", channel: str = "",
          batch: str = "", author: str = "", gender: str = "", period: str = "", date_from: str = "",
          date_to: str = "", country: str = ""):
    user_of(request)
    tags = {"kind": kind, "temp": temp, "channel": channel}
    with SessionLocal() as s:
        cards = crm.search(s, q, limit=10_000) if q else list(s.scalars(select(Card).order_by(Card.created_at.desc())))
        if status:
            cards = [c for c in cards if c.status == status]
        if batch:
            cards = [c for c in cards if c.batch == batch]
        all_countries = country_options(list(s.scalars(select(Card))))
        cards = by_country(by_author(cards, author), country)
        start, end = date_range(period, date_from, date_to)
        cards = by_added(cards, start, end)
        if gender:
            cards = [c for c in cards if (c.gender or "none") == gender]
        all_authors = authors(s)
        for group, value in tags.items():
            if value == "none":
                cards = [c for c in cards if not getattr(c, group)]
            elif value:
                cards = [c for c in cards if getattr(c, group) == value]
        counts = dict(s.execute(select(Note.card_id, func.count()).group_by(Note.card_id)).all())
        thumbs = dict(s.execute(select(Attachment.card_id, func.min(Attachment.id))
                                .where(Attachment.kind == "card").group_by(Attachment.card_id)).all())
    return render(request, "index.html", cards=cards, q=q, status=status, tags=tags, counts=counts, thumbs=thumbs,
                  batch=batch, author=author, authors=all_authors, gender=gender, period=period,
                  country=country, countries=all_countries,
                  date_from=start.isoformat() if start else "", date_to=end.isoformat() if end else "")


@router.get("/work", response_class=HTMLResponse)
def work_page(request: Request, kind: str = "", temp: str = "", channel: str = "", author: str = "", period: str = "",
              date_from: str = "", date_to: str = "", country: str = ""):
    user_of(request)
    tags = {"kind": kind, "temp": temp, "channel": channel}
    with crm.LOCK, SessionLocal() as s:
        if work.expire_waiting(s):
            s.commit()
        data = work.todo(s)
        all_authors = authors(s)
        all_countries = country_options(list(s.scalars(select(Card))))
    start, end = date_range(period, date_from, date_to)
    for key in ("overdue", "due_today", "later", "waiting", "none", "untriaged"):
        data[key] = by_country(by_added(by_author(data[key], author), start, end), country)
        for group, value in tags.items():
            if value:
                data[key] = [c for c in data[key] if (getattr(c, group) or "none") == value]
    return render(request, "work.html", d=data, tags=tags, today=data["today"], author=author, authors=all_authors,
                  period=period, date_from=start.isoformat() if start else "", date_to=end.isoformat() if end else "",
                  country=country, countries=all_countries)


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
    photos = [a for a in atts if a.kind == "card"]
    return render(request, "card.html", card=card, photos=photos, notes=notes, att_by_id=att_by_id,
                  similar=similar, TEXT_FIELDS=TEXT_FIELDS, LIST_FIELDS=LIST_FIELDS)


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
                "Соцсети", "О контакте", "О контакте (рус)", "Должность (рус)", "Страна (определена)", "Пол", "Тип", "Температура", "Откуда", "Статус", "Добавил", "Дата", "Сводка", "Заметки"])
    for c in cards:
        w.writerow([c.code, c.full_name, c.position, c.company, "\n".join(c.phones or []), "\n".join(c.emails or []),
                    c.website, c.address, c.country, "\n".join(c.socials or []), c.about, c.ru("about") or "", c.ru("position") or "",
                    countries.NAMES.get(c.country_info[0], "") + (" (по номеру)" if c.country_info[1] == "phone" else ""),
                    GENDERS.get(c.gender, "").split(" ", 1)[-1],
                    *[TAG_GROUPS[g][1].get(getattr(c, g), "") for g in TAG_GROUPS], STATUSES.get(c.status, c.status),
                    c.created_by, _dt(c.created_at, "%d.%m.%Y"), c.summary, "\n".join(notes.get(c.id, []))])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="partners-{now():%Y%m%d}.csv"'})
