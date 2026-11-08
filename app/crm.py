"""Общая логика: поиск, похожие контакты, текст карточки, запуск распознавания."""
import hashlib
import logging
import re
import threading
import time

from sqlalchemy import select

from . import ai, ocr
from .config import settings
from .db import FIELD_LABELS, TAG_BUTTONS, TAG_GROUPS, Attachment, Card, Note, SessionLocal, TgLink, now
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
             card.raw_text, card.summary, card.about, notes_text, *card.tag_labels, *(card.emails or []), *(card.socials or [])]
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


def tag_buttons(card: Card) -> list:
    rows = []
    for group, (_, values) in TAG_GROUPS.items():
        row = [(("✅ " if getattr(card, group) == v else "") + TAG_BUTTONS.get(v, label), f"t:{card.id}:{group}:{v}")
               for v, label in values.items()]
        rows += [row[:2], row[2:]] if len(row) > 3 else [row]
    return rows


def card_buttons(card: Card):
    rows = tag_buttons(card) + [[(f"💬 WhatsApp {'+' + n if len(card.whatsapp_numbers) > 1 else ''}".strip(), f"https://wa.me/{n}")]
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
    """Голосовые — всегда; наши тексты — всегда (теги); сообщения контакта — только длинные (выжимка)."""
    text = note.text or ""
    return is_audio(att) or (bool(text) and note.source != "ocr") or len(text) >= settings.summary_min_chars


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
        want_tags = note.source != "ocr"  # теги ставим только по нашим словам, не по сообщениям контакта
    transcript, summary, tags, error = None, None, None, None
    try:
        if audio:
            transcript = ai.transcribe(data, fname, mime)
        base = "\n".join(x for x in (text, transcript) if x).strip()
        want_summary = audio or len(base) >= settings.summary_min_chars
        if base and (want_summary or want_tags):
            res = ai.analyze_note(base, ctx, want_summary=want_summary)
            summary = res.get("summary") if want_summary else None
            if want_tags:
                tags = {g: res.get(g) for g in TAG_GROUPS if res.get(g)}
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
        elif not audio and len(text) < settings.summary_min_chars:
            note.summary = None  # короткая заметка (например, после правки) — выжимка не нужна
        note.tags = tags or None
        note.ai_status, note.ai_error = ("error", error) if error else ("done", None)
        card = s.get(Card, note.card_id) if note.card_id else None
        changed = card.set_tags(tags, by_user=True) if card and tags else []
        s.commit()
        if tg and note.tg_chat_id and (audio or changed):
            try:
                if audio:
                    body = voice_message(card, note)
                else:
                    body = f"🏷 <b>{card.code}</b> {esc(card.title)}"
                if changed:
                    body += "\n🏷 Теги: " + ", ".join(esc(c) for c in changed)
                sent = tg.send(note.tg_chat_id, body, reply_to=note.tg_message_id,
                               buttons=card_buttons(card) if card else None)
                link(s, note.tg_chat_id, sent["message_id"], "note" if audio else "bot",
                     card_id=note.card_id, note_id=note.id if audio else None)
                s.commit()
            except Exception:  # noqa: BLE001
                log.exception("cannot send result for note %s", note_id)
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


# ---------- /list: список участников группы → много контактов ----------

LIST_SOURCES = {"whatsapp": ("WhatsApp", "wa_base"), "telegram": ("Telegram", None),
                "ws_app": ("приложения Web Summit", "ws_app"), "other": ("", None)}


def _clean_name(name: str | None) -> str:
    return re.sub(r"^[~\s]+", "", name or "").strip()


def _find_existing(cards: list[Card], phone_digits: str, name: str, username: str | None) -> Card | None:
    if len(phone_digits) >= 7:
        for c in cards:
            if phone_digits in {ocr.phone_key(p) for p in c.phones or []}:
                return c
    if username:
        for c in cards:
            if (c.full_name or "").strip().lower() == name.lower() and \
                    any(username.lower() in (v or "").lower() for v in c.socials or []):
                return c
    return None


def run_list_import(att_id: int, chat_id: int, reply_to: int | None, author: str, tg: Telegram | None) -> None:
    with SessionLocal() as s:
        att = s.get(Attachment, att_id)
        files = [(att.data, att.mime)] if att else []
    if not files:
        return
    try:
        result = ocr.extract_people(files)
    except Exception as e:  # noqa: BLE001
        log.exception("list import failed")
        if tg:
            tg.send(chat_id, f"⚠️ Не удалось разобрать список: {esc(str(e)[:300])}\nСкриншот сохранён — пришлите его ещё раз.",
                    reply_to=reply_to)
        return
    where, channel = LIST_SOURCES.get(result.get("source"), LIST_SOURCES["other"])
    group = (result.get("group_name") or "").strip()
    origin = "Из " + (f"группы {where} «{group}»" if group else (f"списка {where}" if where else "списка участников"))
    batch = f"L{att_id}"
    created, updated, cut, seen = [], [], 0, set()
    with LOCK, SessionLocal() as s:
        cards = list(s.scalars(select(Card)))
        for p in result.get("people") or []:
            name = _clean_name(p.get("name"))
            phone = ocr.norm_phone(p.get("phone") or "") if p.get("phone") else ""
            username = (p.get("username") or "").strip() or None
            if username and not username.startswith("@"):
                username = "@" + username
            if p.get("cut_off"):
                cut += 1
                continue
            if not name or not (phone or username or p.get("subtitle")):
                continue
            key = ocr.phone_key(phone) or f"{name.lower()}|{username or ''}"
            if key in seen:  # одна строка дважды (обзор + фрагмент скриншота)
                continue
            seen.add(key)
            socials = []
            if result.get("source") == "whatsapp":
                if phone.startswith("+"):
                    socials.append(f"WhatsApp: {phone}")
                if username:
                    socials.append(f"WhatsApp: {username}")
            elif username:
                socials.append(f"{'Telegram' if result.get('source') == 'telegram' else 'Ник'}: {username}")
            about = origin + (f"\nПодпись: {p['subtitle'].strip()}" if p.get("subtitle") else "")
            data = {"full_name": name, "position": p.get("position"), "company": p.get("company"),
                    "website": p.get("website"), "about": about, "phones": [phone] if phone else [],
                    "emails": [], "socials": socials, "uncertain_fields": []}
            tags = {"temp": "cold", "channel": channel}
            card = _find_existing(cards, ocr.phone_key(phone), name, username)
            if card is not None:  # уже есть — дополняем пустое, выбранные теги не трогаем
                ocr.apply_result(card, data)
                card.set_tags(tags, by_user=False)
                updated.append(card)
                continue
            card = Card(source="list", created_by=author, tg_chat_id=chat_id, batch=batch, ocr_status="none")
            ocr.apply_result(card, data)
            card.set_tags(tags, by_user=False)
            s.add(card)
            s.flush()
            cards.append(card)
            created.append(card)
        s.commit()
        text = list_report(created, updated, cut, origin)
        if tg:
            buttons = [[("↩️ Отменить импорт", f"lu:{batch}")]] if created else []
            if created and settings.public_url.startswith("https://"):
                buttons.append([("Открыть в CRM", f"{settings.public_url}/?batch={batch}")])
            sent = tg.send(chat_id, text, reply_to=reply_to, buttons=buttons or None)
            if len(created) == 1:
                link(s, chat_id, sent["message_id"], "bot", card_id=created[0].id)
            s.commit()


def list_report(created, updated, cut, origin) -> str:
    def label(c):
        extra = c.company or c.position
        return f"{c.code} {esc(c.title)}" + (f" — {esc(extra)}" if extra else "")

    lines = [f"📋 <b>Добавлено: {len(created)}</b> (❄️ холодные) · {esc(origin)}"]
    lines += [label(c) for c in created[:40]]
    if len(created) > 40:
        lines.append(f"… и ещё {len(created) - 40}")
    if updated:
        lines.append(f"\n🔁 Уже были в базе, дополнены: {len(updated)} — " + ", ".join(label(c) for c in updated[:15]))
    if cut:
        lines.append(f"✂️ Пропущено обрезанных строк: {cut} — они будут целиком на следующем скриншоте.")
    if not created and not updated:
        lines.append("Людей на скриншоте не нашлось.")
    return "\n".join(lines)


def undo_batch(s, batch: str) -> tuple[int, int]:
    """Удаляет контакты пачки, к которым ещё ничего не добавляли. Возвращает (удалено, оставлено)."""
    removed = kept = 0
    for card in s.scalars(select(Card).where(Card.batch == batch, Card.source == "list")):
        if s.scalar(select(Note.id).where(Note.card_id == card.id).limit(1)) is not None:
            kept += 1
            continue
        s.delete(card)
        removed += 1
    return removed, kept


def start_list_import(att_id, chat_id, reply_to, author, tg=None) -> None:
    spawn(run_list_import, att_id, chat_id, reply_to, author, tg)
