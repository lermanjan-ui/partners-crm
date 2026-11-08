"""Общая логика: поиск, похожие контакты, текст карточки, запуск распознавания."""
import hashlib
import logging
import re
import threading
import time

from sqlalchemy import select

from . import ai, ocr
from .config import settings
from .db import FIELD_LABELS, Attachment, Card, Note, SessionLocal, TgLink, now
from .tg import Telegram, esc

log = logging.getLogger("crm")

# Все изменения БД из бота и OCR идут под одним замком: пользователей двое,
# а так исключены гонки (альбом из двух фото, ответ во время распознавания и т.п.).
LOCK = threading.RLock()

ID_RE = re.compile(r"(?:^|\s)#?[PpРр](\d+)\b")


def parse_card_id(text: str | None) -> int | None:
    m = ID_RE.search(text or "")
    return int(m.group(1)) if m else None


def haystack(card: Card, notes_text: str = "") -> str:
    parts = [card.code, card.full_name, card.position, card.company, card.website, card.address, card.country,
             card.raw_text, card.summary, card.about, notes_text, *(card.emails or []), *(card.socials or [])]
    parts += [ocr.phone_key(p) for p in card.phones or []]
    return " ".join(p for p in parts if p).lower()


def notes_text_by_card(s) -> dict[int, str]:
    out: dict[int, list[str]] = {}
    for cid, text, transcript in s.execute(select(Note.card_id, Note.text, Note.transcript).where(Note.card_id.is_not(None))):
        out.setdefault(cid, []).extend(x for x in (text, transcript) if x)
    return {k: " ".join(v) for k, v in out.items()}


def search(s, query: str, limit: int = 10) -> list[Card]:
    query = (query or "").strip()
    cid = parse_card_id(query)
    if cid and re.fullmatch(r"#?[PpРр]?\d+", query):
        card = s.get(Card, cid)
        return [card] if card else []
    words = [w.lower() for w in query.split() if w]
    digits = re.sub(r"\D", "", query)
    cards = s.scalars(select(Card).order_by(Card.updated_at.desc())).all()
    notes = notes_text_by_card(s)
    out = []
    for c in cards:
        h = haystack(c, notes.get(c.id, ""))
        if all(w in h for w in words) or (len(digits) >= 5 and digits in h):
            out.append(c)
    return out[:limit]


def recent(s, limit: int = 6) -> list[Card]:
    return list(s.scalars(select(Card).order_by(Card.created_at.desc()).limit(limit)))


def similar(s, card: Card) -> list[tuple[Card, str]]:
    """Похожие контакты — только предупреждение, объединение не делаем."""
    phones = {ocr.phone_key(p) for p in card.phones or [] if len(ocr.phone_key(p)) >= 7}
    emails = {e.lower() for e in card.emails or []}
    name = (card.full_name or "").strip().lower()
    out = []
    for other in s.scalars(select(Card).where(Card.id != card.id)):
        if phones & {ocr.phone_key(p) for p in other.phones or []}:
            out.append((other, "совпадает телефон"))
        elif emails & {e.lower() for e in other.emails or []}:
            out.append((other, "совпадает email"))
        elif name and name == (other.full_name or "").strip().lower():
            out.append((other, "совпадает имя"))
    return out[:3]


def short_label(card: Card) -> str:
    label = f"{card.code} {card.title}"
    if card.company and card.company != card.title:
        label += f" · {card.company}"
    return label[:60]


def card_url(card: Card) -> str | None:
    return f"{settings.public_url}/c/{card.id}" if settings.public_url.startswith("https://") else None


def summary(s, card: Card) -> str:
    lines = [f"🪪 <b>{card.code}</b> {esc(card.title)}"]
    sub = " · ".join(esc(x) for x in (card.position, card.company if card.company != card.title else None) if x)
    if sub:
        lines.append(sub)
    for icon, values in (("📞", card.phones), ("✉️", card.emails), ("🔗", card.socials)):
        lines += [f"{'💬' if re.match(r'whats', v, re.I) else icon} {esc(v)}" for v in values or []]
    for icon, v in (("🌐", card.website), ("📍", card.address)):
        if v:
            lines.append(f"{icon} {esc(v)}")
    if card.ocr_status == "processing":
        lines.append("⏳ Распознаётся…")
    elif card.ocr_status == "from_note":
        lines.append("⏳ Заполняю из заметки…")
    elif card.ocr_status == "error":
        lines.append("⚠️ Не удалось заполнить автоматически — данные можно внести в CRM.")
    elif card.ocr_status == "done" and not (card.full_name or card.company or card.phones or card.emails):
        lines.append("⚠️ Контакты не найдены — фото сохранено.")
    if card.about and not card.summary:
        about = card.about if len(card.about) <= 400 else card.about[:400] + "…"
        lines.append(f"ℹ️ {esc(about)}")
    if card.summary:
        lines.append(f"\n🧠 <b>Сводка</b>\n{esc(card.summary)}")
    if card.unreadable:
        lines.append("⚠️ Проверить: " + ", ".join(FIELD_LABELS.get(f, f) for f in card.unreadable))
    for other, why in similar(s, card):
        lines.append(f"🔁 Похоже на {other.code} {esc(other.title)} — {why}")
    lines.append("\n<i>Ответьте на это сообщение, чтобы добавить заметку.</i>")
    return "\n".join(lines)


def card_buttons(card: Card):
    rows = [[(f"💬 WhatsApp {'+' + n if len(card.whatsapp_numbers) > 1 else ''}".strip(), f"https://wa.me/{n}")]
            for n in card.whatsapp_numbers[:3]]
    url = card_url(card)
    if url:
        rows.append([("Открыть в CRM", url)])
    return rows or None


def link(s, chat_id: int, message_id: int, kind: str, card_id=None, note_id=None) -> None:
    row = s.get(TgLink, (chat_id, message_id))
    if row is None:
        s.add(TgLink(chat_id=chat_id, message_id=message_id, kind=kind, card_id=card_id, note_id=note_id))
    else:
        row.kind, row.card_id, row.note_id = kind, card_id, note_id


def add_attachment(s, card_id, kind, data: bytes, filename=None, mime=None) -> Attachment:
    att = Attachment(card_id=card_id, kind=kind, data=data, filename=filename, mime=mime, size=len(data),
                     sha256=hashlib.sha256(data).hexdigest())
    s.add(att)
    s.flush()
    return att


def run_ocr(card_id: int, attachment_ids: list[int] | None, tg: Telegram | None = None,
            chat_id: int | None = None, reply_to: int | None = None, delay: float = 0) -> None:
    """Выполняется в фоне. Вызов OpenAI — вне замка, запись результата — под замком.
    attachment_ids=None — все фото визитки (лицо + оборот)."""
    if delay:
        time.sleep(delay)
    with SessionLocal() as s:
        q = select(Attachment).where(Attachment.card_id == card_id, Attachment.kind == "card") \
            if attachment_ids is None else select(Attachment).where(Attachment.id.in_(attachment_ids))
        files = [(a.data, a.mime) for a in s.scalars(q.order_by(Attachment.id))]
    if not files:
        return
    result, error = None, None
    try:
        result = ocr.extract(files)
    except Exception as e:  # noqa: BLE001 — любая ошибка распознавания не должна терять визитку
        log.exception("OCR failed for card %s", card_id)
        error = str(e)[:500]
    with LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            return
        new_notes = []
        if result is not None:
            ocr.apply_result(card, result)
            card.ocr_status, card.ocr_error = "done", None
            new_notes = add_contact_messages(s, card, result.get("messages") or [])
        else:
            card.ocr_status, card.ocr_error = "error", error
        s.commit()
        long_notes = [n.id for n in new_notes if n.ai_status == "processing"]
        has_notes = card.about is not None or s.scalar(select(Note.id).where(Note.card_id == card_id).limit(1)) is not None
        if tg and chat_id:
            try:
                sent = tg.send(chat_id, summary(s, card), reply_to=reply_to, buttons=card_buttons(card))
                link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
                s.commit()
            except Exception:  # noqa: BLE001
                log.exception("cannot send OCR result for card %s", card_id)
    for note_id in long_notes:
        start_note_ai(note_id)  # длинное сообщение — выжимка, затем сводка
    if result is not None and has_notes and not long_notes:
        schedule_card_summary(card_id)


def add_contact_messages(s, card: Card, messages: list[str]) -> list[Note]:
    """Сообщения, которые контакт сам написал (видны на скриншоте), — в историю дословно, без повторов."""
    existing = {t for (t,) in s.execute(select(Note.text).where(Note.card_id == card.id, Note.source == "ocr"))}
    out = []
    for text in messages:
        text = (text or "").strip()
        if not text or text in existing:
            continue
        note = Note(card_id=card.id, text=text, author="Сообщение контакта", source="ocr")
        if len(text) >= settings.summary_min_chars:
            note.ai_status = "processing"
        s.add(note)
        existing.add(text)
        out.append(note)
    s.flush()
    return out


def spawn(fn, *args) -> None:
    """Фоновая задача. В тестах подменяется синхронным вызовом."""
    threading.Thread(target=fn, args=args, daemon=True).start()


def start_ocr(card_id, attachment_ids, tg=None, chat_id=None, reply_to=None, delay=0) -> None:
    spawn(run_ocr, card_id, attachment_ids, tg, chat_id, reply_to, delay)


# ---------- голосовые, выжимки и сводка ----------

def is_audio(att: Attachment | None) -> bool:
    if att is None:
        return False
    mime = att.mime or ""
    return att.kind == "voice" or mime.startswith("audio/") or mime.startswith("video/")


def needs_ai(note: Note, att: Attachment | None) -> bool:
    return is_audio(att) or len(note.text or "") >= settings.summary_min_chars


def card_info(card: Card) -> str:
    parts = [card.full_name, card.position, card.company, card.website, card.address or card.country,
             ", ".join(card.phones or []), ", ".join(card.emails or [])]
    info = " · ".join(p for p in parts if p) or card.title
    if card.about:
        info += "\nО контакте: " + card.about[:2000]
    return info


def process_note(note_id: int, tg: Telegram | None = None) -> None:
    """Расшифровка голосового и/или короткая выжимка заметки. Затем — обновление сводки по контакту."""
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None:
            return
        att = s.get(Attachment, note.attachment_id) if note.attachment_id else None
        audio = is_audio(att)
        data, fname, mime = (att.data, att.filename, att.mime) if audio else (None, None, None)
        text = note.text or ""
        card = s.get(Card, note.card_id) if note.card_id else None
        ctx = card_info(card) if card else ""
    transcript, summary, error = None, None, None
    try:
        if audio:
            transcript = ai.transcribe(data, fname, mime)
        base = "\n".join(x for x in (text, transcript) if x).strip()
        if base and (audio or len(base) >= settings.summary_min_chars):
            summary = ai.summarize_note(base, ctx)
    except Exception as e:  # noqa: BLE001 — оригинал заметки сохранён, ошибка только помечается
        log.exception("AI failed for note %s", note_id)
        error = str(e)[:500]
    with LOCK, SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None:
            return
        if transcript is not None:
            note.transcript = transcript
        if summary is not None:
            note.summary = summary
        note.ai_status, note.ai_error = ("error", error) if error else ("done", None)
        s.commit()
        card = s.get(Card, note.card_id) if note.card_id else None
        if tg and note.tg_chat_id and audio:
            try:
                sent = tg.send(note.tg_chat_id, voice_message(card, note), reply_to=note.tg_message_id,
                               buttons=card_buttons(card) if card else None)
                link(s, note.tg_chat_id, sent["message_id"], "note", card_id=note.card_id, note_id=note.id)
                s.commit()
            except Exception:  # noqa: BLE001
                log.exception("cannot send transcript for note %s", note_id)
        card_id = note.card_id
        fill = card is not None and card.ocr_status == "from_note"
    if fill:
        fill_card_from_note(card_id, note_id, tg)  # сам обновит сводку
    elif card_id:
        schedule_card_summary(card_id)


def fill_card_from_note(card_id: int, note_id: int, tg: Telegram | None = None) -> None:
    """Новый контакт без визитки: имя, компания, телефон и т.д. — из текста или расшифровки заметки."""
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        text = "\n".join(x for x in (note.text, note.transcript) if x).strip() if note else ""
    result, error = None, None
    if text:
        try:
            result = ai.extract_contact(text)
        except Exception as e:  # noqa: BLE001
            log.exception("contact extraction failed for card %s", card_id)
            error = str(e)[:500]
    else:
        error = "в заметке нет текста"
    with LOCK, SessionLocal() as s:
        card, note = s.get(Card, card_id), s.get(Note, note_id)
        if card is None:
            return
        if result is not None:
            ocr.apply_result(card, result)
            card.ocr_status, card.ocr_error = "none", None
        else:
            card.ocr_status, card.ocr_error = "error", error
        s.commit()
        if tg and card.tg_chat_id:
            try:
                text = summary(s, card).replace("\n<i>Ответьте", "\n<i>Имя и контакты можно поправить в CRM. Ответьте", 1)
                sent = tg.send(card.tg_chat_id, text, reply_to=note.tg_message_id if note else None,
                               buttons=card_buttons(card))
                link(s, card.tg_chat_id, sent["message_id"], "bot", card_id=card.id)
                s.commit()
            except Exception:  # noqa: BLE001
                log.exception("cannot send new contact %s", card_id)
    schedule_card_summary(card_id)


def start_fill_card(card_id: int, note_id: int, tg: Telegram | None = None) -> None:
    spawn(fill_card_from_note, card_id, note_id, tg)


def voice_message(card: Card | None, note: Note) -> str:
    head = f"🎙 <b>{card.code}</b> {esc(card.title)}" if card else "🎙 Голосовое"
    if note.ai_status == "error":
        return f"{head}\n⚠️ Не удалось расшифровать: {esc(note.ai_error)}\nАудио сохранено."
    lines = [head]
    if note.summary:
        lines.append(f"<b>Кратко:</b> {esc(note.summary)}")
    if note.transcript:
        full = note.transcript if len(note.transcript) <= 2500 else note.transcript[:2500] + " … (полностью — в CRM)"
        lines.append(f"<blockquote expandable>{esc(full)}</blockquote>")
    elif not note.summary:
        lines.append("Речь не распознана — аудио сохранено.")
    return "\n".join(lines)


def start_note_ai(note_id: int, tg: Telegram | None = None) -> None:
    spawn(process_note, note_id, tg)


_sum_lock = threading.Lock()
_sum_running: set[int] = set()
_sum_dirty: set[int] = set()


def schedule_card_summary(card_id: int) -> None:
    """Пересобрать сводку по контакту. Если пересборка уже идёт — повторить её после (берём свежие заметки)."""
    with _sum_lock:
        if card_id in _sum_running:
            _sum_dirty.add(card_id)
            return
        _sum_running.add(card_id)
    spawn(_card_summary_loop, card_id)


def _card_summary_loop(card_id: int) -> None:
    try:
        while True:
            try:
                build_card_summary(card_id)
            except Exception:  # noqa: BLE001
                log.exception("card summary failed for %s", card_id)
            with _sum_lock:
                if card_id in _sum_dirty:
                    _sum_dirty.discard(card_id)
                    continue
                return
    finally:
        with _sum_lock:
            _sum_running.discard(card_id)


def build_card_summary(card_id: int) -> None:
    with SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            return
        info = card_info(card)
        if card.raw_text:
            info += "\nТекст визитки: " + card.raw_text[:1500]
        notes = []
        for n in s.scalars(select(Note).where(Note.card_id == card_id).order_by(Note.created_at)):
            body = "\n".join(x for x in (n.text, n.transcript) if x).strip()
            if body:
                notes.append(f"[{n.created_at:%d.%m %H:%M}, {n.author or '—'}] {body[:4000]}")
    if not notes and not card.about:
        return
    summary = ai.summarize_card(info, notes[-60:])
    with LOCK, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is not None:
            card.summary, card.summary_at = summary, now()
            s.commit()
