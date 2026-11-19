"""Перевод на русский того, что пришло на другом языке: должность, «О контакте», адрес, сообщения контакта,
заметки и расшифровки. Оригинал не меняется — перевод хранится рядом и показывается с пометкой 🇷🇺.

Переводы идут через одну фоновую очередь по одному контакту — так не перегружаем OpenAI,
даже когда после обновления надо перевести всю базу."""
import logging
import queue
import re
import threading

from sqlalchemy import select

from . import ai
from .db import Card, Note, SessionLocal

log = logging.getLogger("translate")

CARD_FIELDS = ("position", "about", "address")  # имена, компании, email, сайты — не переводим
MAX_CHARS = 6000

_CYR = re.compile(r"[а-яё]", re.I)
_LETTER = re.compile(r"[^\W\d_]")


def is_russian(text: str) -> bool:
    letters = _LETTER.findall(text or "")
    if not letters:
        return True
    return len(_CYR.findall(text)) / len(letters) >= 0.5


def needs_translation(text: str | None) -> bool:
    """Есть хотя бы пара слов не по-русски (ссылки, почта и ники — не в счёт)."""
    if not text:
        return False
    clean = re.sub(r"\S+@\S+|https?://\S+|www\.\S+|@\w+|\+?\d[\d\s()\-]{5,}", " ", text)
    words = re.findall(r"[^\W\d_]{2,}", clean)
    return len(words) >= 1 and not is_russian(clean)


def card_ru(card: Card, field: str) -> str | None:
    """Перевод поля, если он актуален (оригинал с тех пор не менялся)."""
    tr = (card.translations or {}).get(field)
    value = getattr(card, field)
    if tr and value and tr.get("src") == value and tr.get("ru") and tr["ru"].strip() != value.strip():
        return tr["ru"]
    return None


def pending_items(card: Card, notes: list[Note]) -> dict[str, str]:
    items = {}
    for f in CARD_FIELDS:
        value = getattr(card, f)
        if needs_translation(value) and (card.translations or {}).get(f, {}).get("src") != value:
            items[f"f:{f}"] = value[:MAX_CHARS]
    for n in notes:
        if needs_translation(n.text) and not n.text_ru:
            items[f"n:{n.id}:text"] = n.text[:MAX_CHARS]
        if needs_translation(n.transcript) and not n.transcript_ru:
            items[f"n:{n.id}:transcript"] = n.transcript[:MAX_CHARS]
    return items


def run_translate(card_id: int, lock=None) -> int:
    """Переводит всё непереведённое у контакта. Возвращает число переведённых фрагментов."""
    from .crm import LOCK
    lock = lock or LOCK
    with SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            return 0
        notes = list(s.scalars(select(Note).where(Note.card_id == card_id)))
        items = pending_items(card, notes)
    if not items:
        return 0
    try:
        result = ai.translate(items)
    except Exception:  # noqa: BLE001 — перевод не критичен: оригинал на месте, попробуем при следующем изменении
        log.exception("translation failed for card %s", card_id)
        return 0
    done = 0
    with lock, SessionLocal() as s:
        card = s.get(Card, card_id)
        if card is None:
            return 0
        translations = dict(card.translations or {})
        for key, ru in result.items():
            if key not in items or not ru:
                continue
            parts = key.split(":")
            if parts[0] == "f":
                current = getattr(card, parts[1]) or ""
                if current[:MAX_CHARS] != items[key]:
                    continue  # поле успели поправить — переведём новую версию в следующий раз
                translations[parts[1]] = {"src": current, "ru": ru.strip()}
                done += 1
            elif parts[0] == "n":
                note = s.get(Note, int(parts[1]))
                if note is None:
                    continue
                src = note.text if parts[2] == "text" else note.transcript
                if (src or "")[:MAX_CHARS] != items[key]:
                    continue  # заметку успели поправить — переведём новую версию в следующий раз
                setattr(note, "text_ru" if parts[2] == "text" else "transcript_ru", ru.strip())
                done += 1
        card.translations = translations
        s.commit()
    return done


# ---------- очередь ----------

_q: "queue.Queue[int]" = queue.Queue()
_queued: set[int] = set()
_qlock = threading.Lock()
_worker_started = False


def enqueue(card_id: int | None) -> None:
    if not card_id:
        return
    with _qlock:
        if card_id in _queued:
            return
        _queued.add(card_id)
    _q.put(card_id)
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker_started
    with _qlock:
        if _worker_started:
            return
        _worker_started = True

    def loop():
        while True:
            card_id = _q.get()
            with _qlock:
                _queued.discard(card_id)
            try:
                run_translate(card_id)
            except Exception:  # noqa: BLE001
                log.exception("translate worker")

    threading.Thread(target=loop, daemon=True, name="translate").start()


def backfill() -> int:
    """После обновления: поставить в очередь все контакты, где есть что перевести."""
    n = 0
    with SessionLocal() as s:
        notes_by_card: dict[int, list[Note]] = {}
        for note in s.scalars(select(Note).where(Note.card_id.is_not(None))):
            notes_by_card.setdefault(note.card_id, []).append(note)
        for card in s.scalars(select(Card).order_by(Card.id.desc())):
            if pending_items(card, notes_by_card.get(card.id, [])):
                enqueue(card.id)
                n += 1
    return n
