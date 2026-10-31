from datetime import datetime, timezone

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

TEXT_FIELDS = ("full_name", "position", "company", "website", "address", "country")
LIST_FIELDS = ("phones", "emails", "socials")
FIELD_LABELS = {
    "full_name": "Имя", "position": "Должность", "company": "Компания", "website": "Сайт",
    "address": "Адрес", "country": "Страна", "phones": "Телефоны", "emails": "Email",
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
    raw_text: Mapped[str | None] = mapped_column(Text)          # весь распознанный текст визитки
    unreadable: Mapped[list] = mapped_column(JSON, default=list)  # поля, которые OCR не уверенно прочитал
    edited_fields: Mapped[list] = mapped_column(JSON, default=list)  # поля, исправленные вручную: OCR их не трогает
    status: Mapped[str] = mapped_column(String(30), default="new")
    source: Mapped[str] = mapped_column(String(20), default="bot")  # bot | web | form (на будущее)
    ocr_status: Mapped[str] = mapped_column(String(20), default="none")  # none | processing | done | error
    ocr_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(100))
    tg_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    @property
    def code(self) -> str:
        return f"#P{self.id}"

    @property
    def title(self) -> str:
        return self.full_name or self.company or f"Визитка {self.code}"


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
