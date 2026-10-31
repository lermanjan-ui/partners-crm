"""Обработка обновлений Telegram.

Правило привязки: заметка попадает к визитке, только если
  1) это ответ (Reply) на визитку / на сообщение бота о ней / на уже привязанную заметку, или
  2) в тексте явно указан ID вида #P12.
Иначе бот спрашивает «К кому это?» и ничего не привязывает сам.
"""
import logging

from sqlalchemy import delete, select, update

from . import crm
from .config import settings
from .db import Attachment, Card, MediaGroup, Note, ProcessedUpdate, TgLink, now
from .tg import Telegram, esc

log = logging.getLogger("bot")

HELP = """<b>Как пользоваться</b>

📸 <b>Визитка</b> — отправьте фото (лучше «как файл», без сжатия) или PDF. Бот сохранит оригинал, распознает контакты и пришлёт карточку с ID.
↩️ <b>Заметка</b> — нажмите «Ответить» на визитку или на сообщение бота о ней и напишите текст, отправьте голосовое или файл. Можно отвечать на любое сообщение цепочки.
🔄 <b>Оборот визитки</b> — отправьте фото ответом на визитку. Или отправьте обе стороны одним альбомом.
#️⃣ Можно указать ID в тексте: «#P12 договорились созвониться».
❓ Если непонятно, к кому относится сообщение, бот спросит.

/find текст — поиск по имени, компании, телефону, email
/last — последние визитки
/new Имя или компания — контакт без визитки"""

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}


def handle_update(s, tg: Telegram, upd: dict) -> list[tuple]:
    """Возвращает задания на распознавание (запускаются после commit)."""
    if s.get(ProcessedUpdate, upd["update_id"]):
        return []  # повторная доставка — игнорируем
    s.add(ProcessedUpdate(update_id=upd["update_id"]))
    jobs: list[tuple] = []
    if "message" in upd:
        jobs = on_message(s, tg, upd["message"])
    elif "edited_message" in upd:
        on_edit(s, tg, upd["edited_message"])
    elif "callback_query" in upd:
        on_callback(s, tg, upd["callback_query"])
    return jobs


def allowed(user: dict) -> bool:
    return user.get("id") in settings.allowed_ids


def author_name(user: dict) -> str:
    return user.get("first_name") or user.get("username") or str(user.get("id"))


# ---------- сообщения ----------

def resolve_reply(s, chat_id: int, reply: dict | None) -> tuple[Card | None, TgLink | None]:
    if not reply:
        return None, None
    lnk = s.get(TgLink, (chat_id, reply["message_id"]))
    if lnk is None:
        return None, None
    if lnk.card_id:
        return s.get(Card, lnk.card_id), lnk
    if lnk.note_id:
        note = s.get(Note, lnk.note_id)
        if note and note.card_id:
            return s.get(Card, note.card_id), lnk
    return None, lnk


def file_of(msg: dict) -> tuple[dict, str, str | None, str] | None:
    """(объект файла, kind, mime, имя) для фото/документа/голосового."""
    if msg.get("photo"):
        return msg["photo"][-1], "image", "image/jpeg", "photo.jpg"
    if msg.get("document"):
        d = msg["document"]
        mime = d.get("mime_type")
        kind = "image" if mime in IMAGE_MIMES else "file"
        return d, kind, mime, d.get("file_name") or "file"
    for key, mime, name in (("voice", "audio/ogg", "voice.ogg"), ("audio", None, "audio"),
                            ("video_note", "video/mp4", "video.mp4"), ("video", None, "video.mp4")):
        if msg.get(key):
            obj = msg[key]
            return obj, "voice" if key in ("voice", "audio") else "file", obj.get("mime_type") or mime, obj.get("file_name") or name
    return None


def on_message(s, tg: Telegram, msg: dict) -> list[tuple]:
    user, chat_id, mid = msg.get("from") or {}, msg["chat"]["id"], msg["message_id"]
    if msg["chat"].get("type") != "private":
        return []
    if not allowed(user):
        tg.send(chat_id, f"Доступ закрыт.\nВаш Telegram ID: <code>{user.get('id')}</code> — передайте его администратору.")
        return []
    who = author_name(user)
    text = (msg.get("text") or msg.get("caption") or "").strip()
    card, reply_link = resolve_reply(s, chat_id, msg.get("reply_to_message"))

    # Ответ на «К кому это?» / на просьбу ввести поиск — это поисковый запрос.
    if reply_link and reply_link.kind in ("ask", "search") and reply_link.note_id and msg.get("text"):
        return show_choices(s, tg, chat_id, reply_link.note_id, query=text) or []

    if msg.get("text", "").startswith("/"):
        return on_command(s, tg, chat_id, mid, text, who)

    f = file_of(msg)
    if f:
        obj, kind, mime, name = f
        if (obj.get("file_size") or 0) > settings.max_file_mb * 1024 * 1024:
            tg.send(chat_id, f"Файл больше {settings.max_file_mb} МБ — Telegram не даёт боту его скачать. Сожмите или отправьте фото.", reply_to=mid)
            return []
        data = tg.download(obj["file_id"])
        if kind == "image":
            return on_card_image(s, tg, msg, who, card, data, mime, name, text)
        return [] if add_note(s, tg, msg, who, card, text, (data, kind, mime, name)) else []

    if not text:
        tg.send(chat_id, "Этот тип сообщения не поддерживается. Отправьте фото визитки, текст, голосовое или файл.", reply_to=mid)
        return []
    add_note(s, tg, msg, who, card, text, None)
    return []


def on_card_image(s, tg, msg, who, card, data, mime, name, caption) -> list[tuple]:
    chat_id, mid = msg["chat"]["id"], msg["message_id"]
    group = msg.get("media_group_id")
    if card is None and group:
        mg = s.get(MediaGroup, group)
        if mg:
            card = s.get(Card, mg.card_id)
    if card is None and caption:
        cid = crm.parse_card_id(caption)
        card = s.get(Card, cid) if cid else None
    if card is not None:
        att = crm.add_attachment(s, card.id, "card", data, name, mime)
        crm.link(s, chat_id, mid, "card", card_id=card.id)
        if caption and crm.parse_card_id(caption) != card.id:
            s.add(Note(card_id=card.id, text=caption, author=who, tg_chat_id=chat_id, tg_message_id=mid))
        if group and s.get(MediaGroup, group):
            # следующее фото альбома: распознается вместе с первым (задание уже запланировано)
            return []
        # оборот / дополнительное фото ответом на визитку
        card.ocr_status = "processing"
        if group:
            s.add(MediaGroup(id=group, card_id=card.id))
        sent = tg.send(chat_id, f"🔄 Добавлено к <b>{card.code}</b> {esc(card.title)}, распознаю…", reply_to=mid)
        crm.link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
        return [(card.id, None if group else [att.id], chat_id, mid, 4 if group else 0)]

    card = Card(source="bot", created_by=who, tg_chat_id=chat_id, ocr_status="processing")
    s.add(card)
    s.flush()
    att = crm.add_attachment(s, card.id, "card", data, name, mime)
    crm.link(s, chat_id, mid, "card", card_id=card.id)
    if group:
        s.add(MediaGroup(id=group, card_id=card.id))
    if caption:
        s.add(Note(card_id=card.id, text=caption, author=who, tg_chat_id=chat_id, tg_message_id=mid))
    hint = mime == "image/jpeg" and msg.get("photo") and "\n<i>Совет: отправляйте визитки «как файл» — так точнее распознаётся.</i>" or ""
    sent = tg.send(chat_id, f"📥 Визитка принята — <b>{card.code}</b>. Распознаю…{hint}", reply_to=mid)
    crm.link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
    # Для альбома ждём остальные фото (оборот) и распознаём все стороны разом.
    return [(card.id, None if group else [att.id], chat_id, mid, 4 if group else 0)]


def add_note(s, tg, msg, who, card, text, file) -> Note:
    chat_id, mid = msg["chat"]["id"], msg["message_id"]
    if card is None:
        cid = crm.parse_card_id(text)
        card = s.get(Card, cid) if cid else None
    note = Note(card_id=card.id if card else None, text=text or None, author=who,
                tg_chat_id=chat_id, tg_message_id=mid)
    if file:
        data, kind, mime, name = file
        att = crm.add_attachment(s, note.card_id, kind, data, name, mime)
        note.attachment_id = att.id
    s.add(note)
    s.flush()
    crm.link(s, chat_id, mid, "note", card_id=note.card_id, note_id=note.id)
    if card:
        card.updated_at = now()
        sent = tg.send(chat_id, f"📝 Добавлено к <b>{card.code}</b> {esc(card.title)}", reply_to=mid)
        crm.link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
    else:
        show_choices(s, tg, chat_id, note.id, reply_to=mid)
    return note


def show_choices(s, tg, chat_id, note_id, reply_to=None, query=None, edit_message_id=None):
    note = s.get(Note, note_id)
    if note is None or note.card_id:
        tg.send(chat_id, "Это сообщение уже привязано.")
        return None
    if query:
        cards = crm.search(s, query, limit=8)
        head = f"Найдено по «{esc(query)}»: {len(cards)}" if cards else f"По «{esc(query)}» ничего не найдено."
    else:
        cards = crm.recent(s)
        head = "❓ <b>К кому это?</b> Выберите контакт:" if cards else "❓ <b>К кому это?</b> Пока нет ни одной визитки."
    buttons = [[(crm.short_label(c), f"a:{note_id}:{c.id}")] for c in cards]
    buttons.append([("🔍 Найти", f"f:{note_id}"), ("✖ Отмена", f"x:{note_id}")])
    text = head + "\n<i>Или ответьте на это сообщение именем, компанией или телефоном.</i>"
    if edit_message_id:
        tg.edit(chat_id, edit_message_id, text, buttons)
        return None
    sent = tg.send(chat_id, text, reply_to=reply_to, buttons=buttons)
    crm.link(s, chat_id, sent["message_id"], "ask", note_id=note_id)
    return None


def on_command(s, tg, chat_id, mid, text, who) -> list[tuple]:
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd in ("/start", "/help"):
        tg.send(chat_id, HELP)
    elif cmd == "/find":
        if not arg:
            tg.send(chat_id, "Напишите запрос: <code>/find Иван</code> или <code>/find +998901234567</code>")
            return []
        cards = crm.search(s, arg)
        if not cards:
            tg.send(chat_id, f"По «{esc(arg)}» ничего не найдено.", reply_to=mid)
        else:
            tg.send(chat_id, f"Найдено: {len(cards)}", reply_to=mid,
                    buttons=[[(crm.short_label(c), f"c:{c.id}")] for c in cards])
    elif cmd == "/last":
        cards = crm.recent(s, limit=10)
        if not cards:
            tg.send(chat_id, "Визиток пока нет.")
        else:
            tg.send(chat_id, "Последние визитки:", buttons=[[(crm.short_label(c), f"c:{c.id}")] for c in cards])
    elif cmd == "/new":
        if not arg:
            tg.send(chat_id, "Напишите имя или компанию: <code>/new Иван Петров, Acme</code>")
            return []
        card = Card(full_name=arg[:300], source="bot", created_by=who, tg_chat_id=chat_id, edited_fields=["full_name"])
        s.add(card)
        s.flush()
        sent = tg.send(chat_id, crm.summary(s, card), reply_to=mid, buttons=crm.card_buttons(card))
        crm.link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
    else:
        tg.send(chat_id, "Неизвестная команда. /help — справка.")
    return []


def on_edit(s, tg, msg: dict) -> None:
    if not allowed(msg.get("from") or {}):
        return
    chat_id, mid = msg["chat"]["id"], msg["message_id"]
    text = (msg.get("text") or msg.get("caption") or "").strip()
    note = s.scalars(select(Note).where(Note.tg_chat_id == chat_id, Note.tg_message_id == mid)).first()
    if note is None:
        if msg.get("caption") and s.get(TgLink, (chat_id, mid)):  # добавили подпись к уже отправленной визитке
            lnk = s.get(TgLink, (chat_id, mid))
            if lnk.card_id:
                s.add(Note(card_id=lnk.card_id, text=text, author=author_name(msg["from"]),
                           tg_chat_id=chat_id, tg_message_id=mid))
        return
    if note.text != text:
        note.prev_text, note.text, note.edited_at = note.text, text, now()


# ---------- кнопки ----------

def on_callback(s, tg, cq: dict) -> None:
    user = cq.get("from") or {}
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id, mid = msg.get("chat", {}).get("id"), msg.get("message_id")
    if not allowed(user) or chat_id is None:
        tg.answer_callback(cq["id"], "Нет доступа")
        return
    kind, *args = data.split(":")
    if kind == "a":  # привязать заметку к карточке
        note, card = s.get(Note, int(args[0])), s.get(Card, int(args[1]))
        if note is None or card is None:
            tg.answer_callback(cq["id"], "Не найдено")
            return
        if note.card_id and note.card_id != card.id:
            tg.answer_callback(cq["id"], "Уже привязано к другому контакту")
            return
        note.card_id = card.id
        if note.attachment_id:
            s.execute(update(Attachment).where(Attachment.id == note.attachment_id).values(card_id=card.id))
        s.execute(update(TgLink).where(TgLink.note_id == note.id).values(card_id=card.id))
        card.updated_at = now()
        tg.edit(chat_id, mid, f"📝 Добавлено к <b>{card.code}</b> {esc(card.title)}")
        crm.link(s, chat_id, mid, "bot", card_id=card.id)
        tg.answer_callback(cq["id"], "Сохранено")
    elif kind == "f":  # попросить поисковый запрос
        tg.answer_callback(cq["id"])
        sent = tg.send(chat_id, "🔍 Напишите имя, компанию, телефон или #ID:", force_reply=True,
                       placeholder="Имя, компания, телефон")
        crm.link(s, chat_id, sent["message_id"], "search", note_id=int(args[0]))
    elif kind == "x":  # отмена — заметку не сохраняем
        note = s.get(Note, int(args[0]))
        if note is not None and note.card_id is None:
            if note.attachment_id:
                s.execute(delete(Attachment).where(Attachment.id == note.attachment_id))
            s.execute(delete(TgLink).where(TgLink.note_id == note.id))
            s.delete(note)
        tg.edit(chat_id, mid, "✖ Отменено, сообщение не сохранено.")
        tg.answer_callback(cq["id"])
    elif kind == "c":  # показать карточку
        card = s.get(Card, int(args[0]))
        tg.answer_callback(cq["id"])
        if card:
            sent = tg.send(chat_id, crm.summary(s, card), buttons=crm.card_buttons(card))
            crm.link(s, chat_id, sent["message_id"], "bot", card_id=card.id)
    else:
        tg.answer_callback(cq["id"])
