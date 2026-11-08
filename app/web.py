"""Веб-интерфейс CRM: список, карточка, правка, заметки, экспорт CSV."""
import csv
import hmac
import io
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from . import crm
from .config import settings
from .db import (FIELD_LABELS, LIST_FIELDS, STATUSES, TAG_GROUPS, TEXT_FIELDS, Attachment, Card, Note, SessionLocal,
                 now)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(STATUSES=STATUSES, FIELD_LABELS=FIELD_LABELS, TAG_GROUPS=TAG_GROUPS)
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


@router.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", status: str = "", kind: str = "", temp: str = "", channel: str = ""):
    user_of(request)
    tags = {"kind": kind, "temp": temp, "channel": channel}
    with SessionLocal() as s:
        cards = crm.search(s, q, limit=10_000) if q else list(s.scalars(select(Card).order_by(Card.created_at.desc())))
        if status:
            cards = [c for c in cards if c.status == status]
        for group, value in tags.items():
            if value == "none":
                cards = [c for c in cards if not getattr(c, group)]
            elif value:
                cards = [c for c in cards if getattr(c, group) == value]
        counts = dict(s.execute(select(Note.card_id, func.count()).group_by(Note.card_id)).all())
        thumbs = dict(s.execute(select(Attachment.card_id, func.min(Attachment.id))
                                .where(Attachment.kind == "card").group_by(Attachment.card_id)).all())
    return render(request, "index.html", cards=cards, q=q, status=status, tags=tags, counts=counts, thumbs=thumbs)


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
            v = (form.get(f) or "").strip() or None
            if v != getattr(card, f):
                setattr(card, f, v)
                edited.add(f)
        for f in LIST_FIELDS:
            v = [x.strip() for x in (form.get(f) or "").splitlines() if x.strip()]
            if v != list(getattr(card, f) or []):
                setattr(card, f, v)
                edited.add(f)
        if form.get("status") in STATUSES:
            card.status = form["status"]
        card.edited_fields = sorted(edited)
        # теги: меняем и закрепляем только то, что человек действительно поменял в форме
        card.set_tags({g: form.get(g) or None for g in TAG_GROUPS
                       if g in form and (form.get(g) or None) != getattr(card, g)}, by_user=True)
        card.unreadable = [f for f in card.unreadable or [] if f not in edited]
        s.commit()
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
                "Соцсети", "О контакте", "Тип", "Температура", "Откуда", "Статус", "Добавил", "Дата", "Сводка", "Заметки"])
    for c in cards:
        w.writerow([c.code, c.full_name, c.position, c.company, "\n".join(c.phones or []), "\n".join(c.emails or []),
                    c.website, c.address, c.country, "\n".join(c.socials or []), c.about,
                    *[TAG_GROUPS[g][1].get(getattr(c, g), "") for g in TAG_GROUPS], STATUSES.get(c.status, c.status),
                    c.created_by, _dt(c.created_at, "%d.%m.%Y"), c.summary, "\n".join(notes.get(c.id, []))])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="partners-{now():%Y%m%d}.csv"'})
