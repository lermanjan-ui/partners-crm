"""Сайт: программы (акселераторы, инкубаторы, гранты), календарь дедлайнов, банк ответов."""
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select

from . import crm, programs
from .config import settings
from .db import (FORMATS, PROGRAM_OPEN, PROGRAM_STATUSES, STAGES, Answer, Attachment, Card, Note, Program,
                 ProgramContact, ProgramFile, SessionLocal, now)
from .web import apply_filters, render, selected_filters, templates, user_of

router = APIRouter()
templates.env.globals.update(PROGRAM_STATUSES=PROGRAM_STATUSES, FORMATS=FORMATS,
                             FORMATS_SHORT={k: v.split(" ", 1)[1].lower() for k, v in FORMATS.items()})

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def program_filter_defs(items: list[Program]) -> list[dict]:
    countries: dict[str, tuple[str, int]] = {}
    for p in items:
        if p.country_code:
            label, n = countries.get(p.country_code, (p.country_label, 0))
            countries[p.country_code] = (label, n + 1)
    return [
        {"key": "status", "title": "Статус", "options": list(PROGRAM_STATUSES.items()), "get": lambda p: p.status},
        {"key": "country", "title": "Страна",
         "options": [(c, f"{lbl} ({n})") for c, (lbl, n) in sorted(countries.items(), key=lambda x: -x[1][1])]
         + [("none", "не указана / онлайн")], "get": lambda p: p.country_code},
        {"key": "format", "title": "Формат", "options": [*FORMATS.items(), ("none", "не указан")], "get": lambda p: p.format},
        {"key": "stage", "title": "Стадия", "options": list(STAGES.items()),
         "match": lambda p, sel: bool(sel & set(p.stages or []))},
    ]


def _filtered(request: Request, s) -> tuple[list[Program], list[dict], dict]:
    items = list(s.scalars(select(Program)))
    defs = program_filter_defs(items)
    sel = selected_filters(request, defs)
    return apply_filters(items, defs, sel), defs, sel


def _sort_key(p: Program):
    d = programs.deadline_at(p)
    return (d is None, d.timestamp() if d else 0, p.title.lower())


# ---------- список ----------

@router.get("/programs", response_class=HTMLResponse)
def programs_list(request: Request, past: str = ""):
    user_of(request)
    with SessionLocal() as s:
        items, defs, sel = _filtered(request, s)
        at = now()
        current = sorted([p for p in items if not programs.is_past(p, at)], key=_sort_key)
        past_items = sorted([p for p in items if programs.is_past(p, at)], key=_sort_key, reverse=True)
        files = dict(s.execute(select(ProgramFile.program_id, func.count()).group_by(ProgramFile.program_id)).all())
    return render(request, "programs.html", items=current, past_items=past_items, show_past=bool(past), defs=defs,
                  sel=sel, files=files, hide_dates=True, deadline_text=programs.deadline_text,
                  days_left=programs.days_left, tab="list")


@router.get("/programs/new", response_class=HTMLResponse)
def program_new_form(request: Request):
    user_of(request)
    return render(request, "program_new.html", tab="new")


@router.post("/programs/new")
def program_create(request: Request, url: str = Form(""), title: str = Form("")):
    user = user_of(request)
    url, title = url.strip(), title.strip()
    if not url and not title:
        return RedirectResponse("/programs/new", 303)
    parsed = programs.parse_url(url) if url else None  # чтение страницы и ИИ — вне общего замка
    with crm.LOCK, SessionLocal() as s:
        if url:
            p = programs.create_from_url(s, url, user, parsed)
            if title:
                p.title = title[:300]
        else:
            p = Program(title=title[:300], created_by=user, status="idea")
            s.add(p)
            s.flush()
        s.commit()
        pid = p.id
    return RedirectResponse(f"/programs/{pid}", 303)


# ---------- карточка программы ----------

def _get(s, pid: int) -> Program:
    p = s.get(Program, pid)
    if p is None:
        raise HTTPException(404)
    return p


@router.get("/programs/{pid}", response_class=HTMLResponse)
def program_page(request: Request, pid: int):
    user_of(request)
    with SessionLocal() as s:
        p = _get(s, pid)
        contacts = s.execute(select(Card, ProgramContact.role).join(ProgramContact, ProgramContact.card_id == Card.id)
                             .where(ProgramContact.program_id == pid)).all()
        files = list(s.scalars(select(Attachment).join(ProgramFile, ProgramFile.attachment_id == Attachment.id)
                               .where(ProgramFile.program_id == pid).order_by(Attachment.id)))
        library = list(s.scalars(select(Attachment).where(Attachment.kind == "material", Attachment.id.not_in([f.id for f in files]))
                                 .order_by(Attachment.id.desc()).limit(50)))
        notes = list(s.scalars(select(Note).where(Note.program_id == pid).order_by(Note.created_at.desc())))
        att_by_id = {a.id: a for a in s.scalars(select(Attachment).where(Attachment.id.in_([n.attachment_id for n in notes if n.attachment_id])))}
        origin = s.get(Program, p.repeated_from) if p.repeated_from else None
    return render(request, "program.html", p=p, contacts=contacts, files=files, library=library, notes=notes,
                  att_by_id=att_by_id, origin=origin, TIMEZONES=programs.TIMEZONES, FORMATS=FORMATS,
                  PROGRAM_STATUSES=PROGRAM_STATUSES, deadline_text=programs.deadline_text(p),
                  default_tz=settings.timezone, max_mb=settings.max_file_mb, tab=None)


TEXT = ("title", "country", "city", "funding", "equity", "fee", "duration", "description")


@router.post("/programs/{pid}")
async def program_save(request: Request, pid: int):
    user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        p = _get(s, pid)
        for f in TEXT:
            if f in form:
                value = (form.get(f) or "").strip() or None
                if f == "title" and not value:
                    continue
                setattr(p, f, value)
        if "deadline_date" in form:
            old = (p.deadline_date, p.deadline_time, p.deadline_tz)
            p.deadline_date = programs._date(form.get("deadline_date"))
            p.deadline_time = programs.norm_time(form.get("deadline_time"))
            p.deadline_tz = programs.norm_tz(form.get("deadline_tz")) or settings.timezone
            if (p.deadline_date, p.deadline_time, p.deadline_tz) != old:
                p.reminders_sent = None  # новый дедлайн — напоминания заново
        for f in ("results_date", "start_date", "demo_day"):
            if f in form:
                setattr(p, f, programs._date(form.get(f)))
        if "format" in form:
            p.format = form.get("format") if form.get("format") in FORMATS else None
        if "stages_form" in form:
            chosen = set(form.getlist("stages"))
            p.stages = [k for k in STAGES if k in chosen] or None
        if form.get("status") in PROGRAM_STATUSES:
            _set_status(s, p, form["status"], user_of(request))
        s.commit()
    return RedirectResponse(f"/programs/{pid}", 303)


def _set_status(s, p: Program, status: str, who: str) -> None:
    if status == p.status:
        return
    p.status = status
    if status == "submitted" and not p.submitted_at:
        p.submitted_at, p.submitted_by = now(), who
    s.add(Note(program_id=p.id, kind="system", source="system", author=who, ai_status="done",
               text=f"Статус: {PROGRAM_STATUSES[status]}"))


@router.post("/programs/{pid}/status")
def program_status(request: Request, pid: int, to: str = Form(...)):
    who = user_of(request)
    if to not in PROGRAM_STATUSES:
        raise HTTPException(400)
    with crm.LOCK, SessionLocal() as s:
        _set_status(s, _get(s, pid), to, who)
        s.commit()
    back = request.headers.get("referer") or f"/programs/{pid}"
    return RedirectResponse(back if "/programs" in back or "/calendar" in back else f"/programs/{pid}", 303)


@router.post("/programs/{pid}/reviewed")
def program_reviewed(request: Request, pid: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        _get(s, pid).needs_review = False
        s.commit()
    return RedirectResponse(f"/programs/{pid}", 303)


@router.post("/programs/{pid}/checklist")
async def program_checklist(request: Request, pid: int):
    user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        p = _get(s, pid)
        items = [dict(i) for i in (p.checklist or [])]
        if "toggle" in form and form["toggle"].isdigit() and int(form["toggle"]) < len(items):
            i = int(form["toggle"])
            items[i]["done"] = not items[i].get("done")
        elif "remove" in form and form["remove"].isdigit() and int(form["remove"]) < len(items):
            items.pop(int(form["remove"]))
        for line in (form.get("add") or "").splitlines():
            if line.strip():
                items.append({"text": line.strip()[:200], "done": False})
        p.checklist = items
        s.commit()
    return RedirectResponse(f"/programs/{pid}#checklist", 303)


@router.post("/programs/{pid}/links")
async def program_links(request: Request, pid: int):
    user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        p = _get(s, pid)
        links = [dict(x) for x in (p.links or [])]
        if "remove" in form and form["remove"].isdigit() and int(form["remove"]) < len(links):
            links.pop(int(form["remove"]))
        url = (form.get("url") or "").strip()
        if url:
            if not url.startswith("http"):
                url = "https://" + url
            links.append({"label": (form.get("label") or "").strip()[:80], "url": url})
        p.links = links
        s.commit()
    return RedirectResponse(f"/programs/{pid}#links", 303)


@router.post("/programs/{pid}/files")
async def program_files(request: Request, pid: int, file: UploadFile | None = File(None)):
    user_of(request)
    form = await request.form()
    data = await file.read() if file and file.filename else b""
    if len(data) > settings.max_file_mb * 1024 * 1024:
        raise HTTPException(413, f"Файл больше {settings.max_file_mb} МБ — добавьте его ссылкой (Google Drive, YouTube)")
    with crm.LOCK, SessionLocal() as s:
        _get(s, pid)
        if data:
            att = crm.add_attachment(s, None, "material", data, file.filename, file.content_type)
            s.add(ProgramFile(program_id=pid, attachment_id=att.id))
        attach = form.get("attach")
        if attach and attach.isdigit() and s.get(Attachment, int(attach)) and not s.get(ProgramFile, (pid, int(attach))):
            s.add(ProgramFile(program_id=pid, attachment_id=int(attach)))
        detach = form.get("detach")
        if detach and detach.isdigit():
            row = s.get(ProgramFile, (pid, int(detach)))
            if row:
                s.delete(row)
        s.commit()
    return RedirectResponse(f"/programs/{pid}#files", 303)


@router.post("/programs/{pid}/contacts")
async def program_contacts(request: Request, pid: int):
    user_of(request)
    form = await request.form()
    with crm.LOCK, SessionLocal() as s:
        _get(s, pid)
        query = (form.get("query") or "").strip()
        if query:
            found = crm.search(s, query, limit=1)
            if found and not s.get(ProgramContact, (pid, found[0].id)):
                s.add(ProgramContact(program_id=pid, card_id=found[0].id, role=(form.get("role") or "").strip()[:100] or None))
        remove = form.get("remove")
        if remove and remove.isdigit():
            row = s.get(ProgramContact, (pid, int(remove)))
            if row:
                s.delete(row)
        s.commit()
    return RedirectResponse(f"/programs/{pid}#contacts", 303)


@router.post("/programs/{pid}/note")
def program_note(request: Request, pid: int, text: str = Form("")):
    who = user_of(request)
    if text.strip():
        with crm.LOCK, SessionLocal() as s:
            p = _get(s, pid)
            s.add(Note(program_id=pid, text=text.strip(), author=who, source="web", ai_status="done"))
            p.updated_at = now()
            s.commit()
    return RedirectResponse(f"/programs/{pid}#history", 303)


@router.post("/programs/{pid}/repeat")
def program_repeat(request: Request, pid: int):
    who = user_of(request)
    with crm.LOCK, SessionLocal() as s:
        new = programs.repeat_next_year(s, _get(s, pid), who)
        s.commit()
        new_id = new.id
    return RedirectResponse(f"/programs/{new_id}", 303)


@router.post("/programs/{pid}/delete")
def program_delete(request: Request, pid: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        p = s.get(Program, pid)
        if p:
            s.delete(p)
            s.commit()
    return RedirectResponse("/programs", 303)


# ---------- календарь ----------

@router.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, month: str = ""):
    user_of(request)
    t = programs.today()
    try:
        y, m = (int(x) for x in month.split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = date(t.year, t.month, 1)
    with SessionLocal() as s:
        items, defs, sel = _filtered(request, s)
        events = programs.calendar_events(items)
        upcoming = sorted([p for p in items if (dl := programs.days_left(p, t)) is not None and 0 <= dl <= 60], key=_sort_key)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (first + timedelta(days=32)).replace(day=1)
    qs = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "month"])
    ics_url = f"{settings.public_url}/calendar.ics?token={programs.ics_token()}" if settings.public_url else \
        f"/calendar.ics?token={programs.ics_token()}"
    return render(request, "calendar.html", weeks=programs.month_grid(first.year, first.month), first=first,
                  month_name=f"{MONTHS[first.month - 1].capitalize()} {first.year}", prev_m=prev_m, next_m=next_m,
                  events=events, today=t, defs=defs, sel=sel, hide_dates=True, upcoming=upcoming,
                  deadline_text=programs.deadline_text, ics_url=ics_url, qs=qs, tab="calendar")


@router.get("/calendar.ics")
def calendar_ics(token: str = ""):
    if token != programs.ics_token():
        raise HTTPException(403)
    with SessionLocal() as s:
        items = list(s.scalars(select(Program)))
        body = programs.ics(items)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="programs.ics"'})


# ---------- банк ответов ----------

@router.get("/answers", response_class=HTMLResponse)
def answers(request: Request, q: str = ""):
    user_of(request)
    with SessionLocal() as s:
        items = list(s.scalars(select(Answer).order_by(Answer.question)))
    if q:
        ql = q.lower()
        items = [a for a in items if ql in " ".join(x or "" for x in (a.question, a.answer_ru, a.answer_en)).lower()]
    return render(request, "answers.html", items=items, q=q, tab="answers")


@router.post("/answers")
def answer_save(request: Request, aid: str = Form(""), question: str = Form(""), answer_ru: str = Form(""),
                answer_en: str = Form("")):
    who = user_of(request)
    if question.strip():
        with crm.LOCK, SessionLocal() as s:
            a = s.get(Answer, int(aid)) if aid.isdigit() else None
            if a is None:
                a = Answer(question=question.strip())
                s.add(a)
            a.question, a.answer_ru, a.answer_en = question.strip(), answer_ru.strip() or None, answer_en.strip() or None
            a.updated_by = who
            s.commit()
    return RedirectResponse("/answers", 303)


@router.post("/answers/{aid}/delete")
def answer_delete(request: Request, aid: int):
    user_of(request)
    with crm.LOCK, SessionLocal() as s:
        a = s.get(Answer, aid)
        if a:
            s.delete(a)
            s.commit()
    return RedirectResponse("/answers", 303)


def programs_for_card(s, card_id: int) -> list[tuple[Program, str | None]]:
    return list(s.execute(select(Program, ProgramContact.role).join(ProgramContact, ProgramContact.program_id == Program.id)
                          .where(ProgramContact.card_id == card_id)).all())


__all__ = ["router", "programs_for_card", "PROGRAM_OPEN"]
