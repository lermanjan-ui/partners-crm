import re
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import (JSON, BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, sessionmaker


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


STATUSES = {
    "new": "Новый",
    "in_progress": "В работе",
    "agreed": "Договорились",
    "postponed": "Отложено",
    "rejected": "Отказ",
}

# Теги: в каждой группе выбирается одно значение (или ничего).
TAG_GROUPS = {
    "kind": ("Тип", {"partner": "🤝 Партнёр", "investor": "💰 Инвестор"}),
    "temp": ("Температура", {"cold": "❄️ Холодный", "warm": "🌤 Тёплый", "hot": "🔥 Горячий"}),
    "channel": ("Откуда", {"booth": "🎪 Со стенда", "offline": "🚶 Офлайн",
                           "ws_app": "📱 Приложение Web Summit", "wa_base": "💬 База WhatsApp"}),
}
# Короткие подписи для кнопок в Telegram (длинные обрезаются на телефоне)
TAG_BUTTONS = {"ws_app": "📱 Приложение WS", "wa_base": "💬 База WhatsApp"}

TEXT_FIELDS = ("full_name", "position", "company", "website", "address", "country", "about")
LIST_FIELDS = ("phones", "emails", "socials")
FIELD_LABELS = {
    "full_name": "Имя", "position": "Должность", "company": "Компания", "website": "Сайт",
    "address": "Адрес", "country": "Страна", "about": "О контакте", "phones": "Телефоны", "emails": "Email",
    "socials": "Соцсети / мессенджеры",
}


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(300))
    position: Mapped[str | None] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(300))
    website: Mapped[str | None] = mapped_column(String(300))
    address: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(100))
    phones: Mapped[list] = mapped_column(JSON, default=list)
    emails: Mapped[list] = mapped_column(JSON, default=list)
    socials: Mapped[list] = mapped_column(JSON, default=list)
    about: Mapped[str | None] = mapped_column(Text)  # био/описание из профиля (Web Summit, LinkedIn), отрасль
    raw_text: Mapped[str | None] = mapped_column(Text)          # весь распознанный текст визитки
    unreadable: Mapped[list] = mapped_column(JSON, default=list)  # поля, которые OCR не уверенно прочитал
    edited_fields: Mapped[list] = mapped_column(JSON, default=list)  # поля, исправленные вручную: OCR их не трогает
    status: Mapped[str] = mapped_column(String(30), default="new")
    kind: Mapped[str | None] = mapped_column(String(20))     # partner | investor
    temp: Mapped[str | None] = mapped_column(String(20))     # cold | warm | hot
    channel: Mapped[str | None] = mapped_column(String(20))  # booth | offline | ws_app | wa_base
    source: Mapped[str] = mapped_column(String(20), default="bot")  # bot | web | list | form (на будущее)
    batch: Mapped[str | None] = mapped_column(String(40), index=True)  # пачка импорта из /list (для отмены)
    ocr_status: Mapped[str] = mapped_column(String(20), default="none")  # none | processing | done | error
    ocr_error: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)  # ИИ-сводка по контакту (по визитке и всем заметкам)
    summary_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(100))
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    @property
    def code(self) -> str:
        return f"#P{self.id}"

    def set_tags(self, tags: dict, by_user: bool) -> list[str]:
        """Ставит теги. by_user=True — выбор человека (кнопка, сайт, прямые слова в заметке): перезаписывает
        и закрепляет. Автоматика (скриншот) только заполняет пустое и не трогает то, что выбрал человек.
        Возвращает подписи изменённых тегов."""
        locked = set(self.edited_fields or [])
        changed = []
        for group, (_, values) in TAG_GROUPS.items():
            if group not in tags:
                continue
            value = tags[group]
            if value is not None and value not in values:
                continue
            if not by_user and (group in locked or getattr(self, group) or value is None):
                continue
            if getattr(self, group) != value:
                setattr(self, group, value)
                changed.append(values[value] if value else f"{TAG_GROUPS[group][0]}: —")
            if by_user:
                locked.add(group)
        self.edited_fields = sorted(locked)
        return changed

    @property
    def tag_labels(self) -> list[str]:
        return [values[getattr(self, g)] for g, (_, values) in TAG_GROUPS.items() if getattr(self, g) in values]

    @property
    def whatsapp_numbers(self) -> list[str]:
        """Номера, помеченные как WhatsApp (строки «WhatsApp: +…» в соцсетях), — только с кодом страны."""
        out = []
        for v in self.socials or []:
            if re.search(r"whats\s*app|wa\.me", v, re.I) and "@" not in v:  # «WhatsApp: @ник» — без номера
                digits = re.sub(r"\D", "", v)
                if len(digits) >= 8 and ("+" in v or "wa.me" in v.lower() or len(digits) >= 11) and digits not in out:
                    out.append(digits)
        return out

    @property
    def linkedin_url(self) -> str | None:
        """Ссылка на профиль, если она есть; иначе — поиск в LinkedIn по имени и компании."""
        for v in self.socials or []:
            m = re.search(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/[^\s,;]+", v, re.I)
            if m:
                url = m.group(0)
                return url if url.startswith("http") else "https://" + url
        if not self.full_name:
            return None
        query = " ".join(x for x in (self.full_name, self.company) if x)
        return "https://www.linkedin.com/search/results/people/?keywords=" + quote(query)

    @property
    def title(self) -> str:
        return self.full_name or self.company or f"Контакт {self.code}"


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # card | file | voice
    filename: Mapped[str | None] = mapped_column(String(300))
    mime: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    prev_text: Mapped[str | None] = mapped_column(Text)  # прошлая версия при редактировании в Telegram
    transcript: Mapped[str | None] = mapped_column(Text)  # полная расшифровка голосового
    summary: Mapped[str | None] = mapped_column(Text)     # короткая выжимка (голосовые и длинные тексты)
    ai_status: Mapped[str] = mapped_column(String(20), default="none")  # none | processing | done | error
    ai_error: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict | None] = mapped_column(JSON)  # теги, названные в заметке прямо («горячий инвестор»)
    author: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(20), default="bot")
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id", ondelete="SET NULL"))
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TgLink(Base):
    """Каждое сообщение чата (наше и бота) → к какой визитке/заметке оно относится.
    Так ответ на любое сообщение цепочки находит нужную карточку без ограничения глубины."""
    __tablename__ = "tg_links"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), index=True)
    note_id: Mapped[int | None] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # card | note | bot | ask | search


class ProcessedUpdate(Base):
    __tablename__ = "tg_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChatMode(Base):
    """Режим чата: list — фото разбираются как списки участников, а не как визитки."""
    __tablename__ = "chat_modes"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MediaGroup(Base):
    """Альбом из нескольких фото = одна визитка (лицо + оборот)."""
    __tablename__ = "media_groups"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"))


engine = None
SessionLocal = sessionmaker(expire_on_commit=False)


def init_db(url: str) -> None:
    global engine
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    SessionLocal.configure(bind=engine)
    Base.metadata.create_all(engine)
    migrate()


# Новые колонки для уже существующей базы (create_all добавляет только новые таблицы).
NEW_COLUMNS = {
    "cards": {"summary": "TEXT", "summary_at": "TIMESTAMP", "about": "TEXT",
              "kind": "VARCHAR(20)", "temp": "VARCHAR(20)", "channel": "VARCHAR(20)", "batch": "VARCHAR(40)"},
    "notes": {"transcript": "TEXT", "summary": "TEXT", "ai_status": "VARCHAR(20) DEFAULT 'none'", "ai_error": "TEXT",
              "tags": "JSON"},
}


def migrate(eng=None) -> None:
    from sqlalchemy import inspect, text

    eng = eng or engine
    insp = inspect(eng)
    pg = eng.dialect.name == "postgresql"
    with eng.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    if pg and ddl == "TIMESTAMP":
                        ddl = "TIMESTAMP WITH TIME ZONE"
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
