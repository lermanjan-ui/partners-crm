"""Общая логика: поиск, похожие контакты, текст карточки, запуск распознавания."""
import hashlib
import logging
import re
import threading
import time

from sqlalchemy import select

from . import ocr
from .config import settings
from .db import FIELD_LABELS, Attachment, Card, SessionLocal, TgLink
from .tg import Telegram, esc

log = logging.getLogger("crm")

# Все изменения БД из бота и OCR идут под одним замком: пользователей двое,
# а так исключены гонки (альбом из двух фото, ответ во время распознавания и т.п.).
LOCK = threading.RLock()

ID_RE = re.compile(r"(?:^|\s)#?[PpРр](\d+)\b")


def parse_card_id(text: str | None) -> int | None:
    m = ID_RE.search(text or "")
    return int(m.group(1)) if m else None


def haystack(card: Card) -> str:
    parts = [card.code, card.full_name, card.position, card.company, card.website, card.address, card.country,
             card.raw_text, *(card.emails or []), *(card.socials or [])]
    parts += [ocr.phone_key(p) for p in card.phones or []]
    return " ".join(p for p in parts if p).lower()


def search(s, query: str, limit: int = 10) -> list[Card]:
    query = (query or "").strip()
    cid = parse_card_id(query)
    if cid and re.fullmatch(r"#?[PpРр]?\d+", query):
        card = s.get(Card, cid)
        return [card] if card else []
    words = [w.lower() for w in query.split() if w]
    digits = re.sub(r"\D", "", query)
    cards = s.scalars(select(Card).order_by(Card.updated_at.desc())).all()
    out = []
    for c in cards:
        h = haystack(c)
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
        lines += [f"{icon} {esc(v)}" for v in values or []]
    for icon, v in (("🌐", card.website), ("📍", card.address)):
        if v:
            lines.append(f"{icon} {esc(v)}")
    if card.ocr_status == "processing":
        lines.append("⏳ Распознаётся…")
    elif card.ocr_status == "error":
        lines.append("⚠️ Распознать не удалось — фото сохранено, данные можно внести в CRM.")
    elif card.ocr_status == "done" and not (card.full_name or card.company or card.phones or card.emails):
        lines.append("⚠️ Контакты не найдены — фото сохранено.")
    if card.unreadable:
        lines.append("⚠️ Проверить: " + ", ".join(FIELD_LABELS.get(f, f) for f in card.unreadable))
    for other, why in similar(s, card):
        lines.append(f"🔁 Похоже на {other.code} {esc(other.title)} — {why}")
    lines.append("\n<i>Ответьте на это сообщение, чтобы добавить заметку.</i>")
    return "\n".join(lines)


def card_buttons(card: Card):
    url = card_url(card)
    return [[("Открыть в CRM", url)]] if url else None


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
        if result is not None:
            ocr.apply_result(card, result)
            card.ocr_status, card.ocr_error = "done", None
        else:
            card.ocr_status, card.ocr_error = "error", error
        s.commit()
        if tg and chat_id:
            try:
                sent = tg.send(chat_id, summary(s, card), reply_to=reply_to, buttons=card_buttons(card))
                link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
                s.commit()
            except Exception:  # noqa: BLE001
                log.exception("cannot send OCR result for card %s", card_id)


def start_ocr(card_id, attachment_ids, tg=None, chat_id=None, reply_to=None, delay=0) -> None:
    threading.Thread(target=run_ocr, args=(card_id, attachment_ids, tg, chat_id, reply_to, delay), daemon=True).start()
