import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from . import bot, crm
from .config import settings
from .db import Attachment, Card, Note, SessionLocal, init_db
from .tg import Telegram
from .web import LoginRequired, router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

tg = Telegram(settings.bot_token) if settings.bot_token else None


@asynccontextmanager
async def lifespan(_app):
    startup()
    yield


app = FastAPI(title="Partners CRM", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 24 * 30,
                   https_only=settings.public_url.startswith("https://"), same_site="lax")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(router)


@app.exception_handler(LoginRequired)
def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/login", 303)


def startup() -> None:
    init_db(settings.database_url)
    if tg and settings.public_url.startswith("https://"):
        try:
            tg.set_webhook(f"{settings.public_url}/tg/webhook", settings.webhook_secret)
            log.info("webhook set to %s/tg/webhook", settings.public_url)
        except Exception:  # noqa: BLE001
            log.exception("cannot set webhook")
    # Визитки, распознавание которых прервал перезапуск, — запускаем заново.
    with SessionLocal() as s:
        stuck = list(s.scalars(select(Card).where(Card.ocr_status == "processing")))
        for card in stuck:
            if s.scalar(select(Attachment.id).where(Attachment.card_id == card.id, Attachment.kind == "card")):
                crm.start_ocr(card.id, None, tg, card.tg_chat_id if card.source == "bot" else None)
            else:
                card.ocr_status = "none"
        s.commit()
        # Голосовые/заметки, обработку которых прервал перезапуск.
        for note_id in s.scalars(select(Note.id).where(Note.ai_status == "processing")):
            crm.start_note_ai(note_id, tg)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/tg/webhook")
async def webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != settings.webhook_secret or tg is None:
        return JSONResponse({"ok": False}, 403)
    upd = await request.json()
    # Сохраняем всё до ответа Telegram: если сервер перезапустится посреди обработки,
    # Telegram пришлёт update повторно, а повтор уже обработанного отбрасывается по update_id.
    await run_in_threadpool(process, upd)
    return {"ok": True}


def process(upd: dict) -> None:
    with crm.LOCK, SessionLocal() as s:
        try:
            jobs = bot.handle_update(s, tg, upd)
            s.commit()
        except Exception:  # noqa: BLE001
            s.rollback()
            log.exception("update %s failed", upd.get("update_id"))
            chat = (upd.get("message") or {}).get("chat", {}).get("id")
            if chat:
                try:
                    tg.send(chat, "⚠️ Не удалось сохранить сообщение. Отправьте его ещё раз.")
                except Exception:  # noqa: BLE001
                    pass
            return
    for job in jobs:
        kind, *args = job
        if kind == "ocr":
            card_id, att_ids, chat_id, reply_to, delay = args
            crm.start_ocr(card_id, att_ids, tg, chat_id, reply_to, delay)
        elif kind == "note":
            crm.start_note_ai(args[0], tg)
        elif kind == "card":
            crm.schedule_card_summary(args[0])
