"""Сквозные проверки бота и сайта с фейковыми Telegram и OpenAI (без сети)."""
import io
import os
import tempfile
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["TELEGRAM_ALLOWED_IDS"] = "1,2"
os.environ["WEB_USERS"] = "anna:pw"
os.environ["OPENAI_API_KEY"] = "k"
os.environ["WORK_LOOP"] = "0"  # фоновый цикл сводки в тестах не запускаем — проверяем work.tick напрямую

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import ai, crm, main, ocr, translate  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Card, Note, SessionLocal  # noqa: E402


class FakeTg:
    def __init__(self):
        self.sent, self.edits, self.mid = [], [], 1000

    def send(self, chat_id, text, reply_to=None, buttons=None, force_reply=False, placeholder=None):
        self.mid += 1
        self.sent.append({"chat": chat_id, "id": self.mid, "text": text, "reply_to": reply_to, "buttons": buttons})
        return {"message_id": self.mid}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append({"id": message_id, "text": text, "buttons": buttons})

    def edit_buttons(self, chat_id, message_id, buttons):
        self.edits.append({"id": message_id, "text": None, "buttons": buttons})

    def answer_callback(self, *a, **k):
        pass

    def download(self, file_id):
        buf = io.BytesIO()
        Image.new("RGB", (60, 40), "white").save(buf, "JPEG")
        return buf.getvalue()

    def set_webhook(self, *a):
        pass


OCR_RESULTS = {}


def fake_extract(files):
    return OCR_RESULTS.get("next") or {
        "full_name": "Иван Петров", "position": "CEO", "company": "Acme", "phones": ["+998 90 123-45-67"],
        "emails": ["Ivan@Acme.uz "], "website": "acme.uz", "address": None, "country": None,
        "socials": ["Telegram: @ivan"], "all_text": "Иван Петров\nCEO Acme", "uncertain_fields": [],
    }


AI = {"fail": False}


def fake_transcribe(data, filename, mime):
    if AI["fail"]:
        raise ai.AiError("OpenAI 500: boom")
    return "Обсудили поставку оборудования, нужна презентация до пятницы, бюджет около 50 тысяч долларов"


def fake_analyze(text, ctx="", want_summary=True):
    low = text.lower()
    tags = {"kind": "investor" if "инвестор" in low else ("partner" if "партн" in low else None),
            "temp": "hot" if "горяч" in low else ("cold" if "холодн" in low else ("warm" if "тёпл" in low else None)),
            "channel": "booth" if "со стенда" in low else ("wa_base" if "база whatsapp" in low else None)}
    from app import work as w
    t = w.today()
    work_ = {"touch": "Отправили презентацию" if "отправил" in low else None,
             "ball": "them" if "жду ответа" in low else ("us" if "перезвонить" in low else None),
             "next_step": "Перезвонить" if "перезвонить" in low else None,
             "due": (t + timedelta(days=(4 - t.weekday()) % 7 or 7)).isoformat() if "пятниц" in low else None}
    gender = "female" if "женщина" in low else ("male" if "мужчина" in low else None)
    return {"summary": "КРАТКО: " + text[:40] if want_summary else None, **tags, **work_, "gender": gender}


TRANSLATE_CALLS = []


def fake_translate(items):
    TRANSLATE_CALLS.append(dict(items))
    if any("FAIL" in v for v in items.values()):
        raise ai.AiError("OpenAI 500")
    return {k: "RU: " + v for k, v in items.items()}


def fake_contact(text):
    if "Мария" not in text and "поставку" not in text:
        return {k: None for k in ("full_name", "position", "company", "website", "address", "country")} | {
            "phones": [], "emails": [], "socials": [], "uncertain_fields": []}
    return {"full_name": "Мария Ким" if "Мария" in text else "Пётр", "position": None, "company": "Globex",
            "phones": ["+998 91 555 44 33"], "emails": ["maria@globex.uz"], "website": None, "address": None,
            "country": None, "socials": [], "uncertain_fields": ["position"]}


@pytest.fixture(scope="module")
def env():
    fake = FakeTg()
    main.tg = fake
    ocr.extract = fake_extract
    crm.spawn = lambda fn, *a: fn(*a)  # фоновые задачи — синхронно
    ai.transcribe = fake_transcribe
    ai.analyze_note = fake_analyze
    ai.summarize_card = lambda info, notes: f"Кто: {info[:30]}\nЗаметок: {len(notes)}"
    ai.extract_contact = fake_contact
    ai.translate = fake_translate
    translate.enqueue = lambda cid: translate.run_translate(cid) if cid else 0  # очередь — синхронно
    ai.parse_step = lambda text: {"next_step": text.split(" в ")[0].capitalize(),
                                  "due": (__import__("app.work", fromlist=["x"]).today() + timedelta(days=1)).isoformat()
                                  if "завтра" in text else None}
    with TestClient(main.app) as client:
        yield client, fake


UPD = [0]


def post(client, kind="message", **payload):
    UPD[0] += 1
    body = {"update_id": UPD[0], kind: payload}
    r = client.post("/tg/webhook", json=body, headers={"X-Telegram-Bot-Api-Secret-Token": settings.webhook_secret})
    assert r.status_code == 200
    return body


def msg(mid, user=1, reply_to=None, **kw):
    m = {"message_id": mid, "chat": {"id": user, "type": "private"}, "from": {"id": user, "first_name": f"U{user}"}, **kw}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return m


def last_card(s):
    return s.query(Card).order_by(Card.id.desc()).first()


def test_full_flow(env):
    client, tg = env
    # 1. Визитка А
    post(client, **msg(1, photo=[{"file_id": "a", "file_size": 100}], caption="Встретили у стенда"))
    with SessionLocal() as s:
        a = last_card(s)
        assert a.full_name == "Иван Петров" and a.phones == ["+998901234567"] and a.emails == ["ivan@acme.uz"]
        assert a.ocr_status == "done"
        assert s.query(Note).filter_by(card_id=a.id).one().text == "Встретили у стенда"
    summary_msg = tg.sent[-1]
    assert "#P" in summary_msg["text"] and summary_msg["reply_to"] == 1

    # 2. Визитка Б, другой человек
    OCR_RESULTS["next"] = dict(fake_extract([]), full_name="Анна", company="Beta", phones=["+7 999 000 11 22"], emails=[])
    OCR_RESULTS["next"] = {**OCR_RESULTS["next"]}
    post(client, **msg(2, document={"file_id": "b", "mime_type": "image/png", "file_name": "b.png", "file_size": 10}))
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        b = last_card(s)
        assert b.full_name == "Анна" and b.id != a.id

    # 3. Чередующиеся цепочки: ответ на визитку А, ответ на визитку Б, ответ на ответ, ответ на сообщение бота
    post(client, **msg(3, reply_to=1, text="Обсудили интеграцию"))
    post(client, **msg(4, reply_to=2, text="Про Б"))
    post(client, **msg(5, reply_to=3, text="Отправить презентацию"))
    post(client, **msg(6, reply_to=summary_msg["id"], text="Через месяц"))
    chain = 6
    for i in range(7, 107):  # цепочка из 100 ответов
        post(client, **msg(i, reply_to=chain, text=f"ответ {i}"))
        chain = i
    with SessionLocal() as s:
        texts_a = {n.text for n in s.query(Note).filter_by(card_id=a.id)}
        texts_b = {n.text for n in s.query(Note).filter_by(card_id=b.id)}
    assert {"Обсудили интеграцию", "Отправить презентацию", "Через месяц", "ответ 106"} <= texts_a
    assert texts_b == {"Про Б"}
    assert len(texts_a) == 1 + 3 + 100

    # 4. Сообщение без ответа → «К кому это?», выбор кнопкой
    post(client, **msg(200, text="Не забыть позвонить"))
    ask = tg.sent[-1]
    assert "К кому это" in ask["text"]
    cb = [btn for row in ask["buttons"] for _, btn in [row[0]] if btn.endswith(f":{b.id}")][0]
    post(client, "callback_query", id="q", data=cb, **{"from": {"id": 1}, "message": {"message_id": ask["id"], "chat": {"id": 1}}})
    with SessionLocal() as s:
        assert s.query(Note).filter_by(text="Не забыть позвонить").one().card_id == b.id
    # ответ на это сообщение теперь тоже идёт к Б
    post(client, **msg(201, reply_to=200, text="Позвонил"))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(text="Позвонил").one().card_id == b.id

    # 5. Ответ на «К кому это?» текстом = поиск
    post(client, **msg(202, text="Голосовое ниже"))
    ask2 = tg.sent[-1]
    post(client, **msg(203, reply_to=ask2["id"], text="Acme"))
    found = tg.sent[-1]
    assert "Найдено" in found["text"] and any(f":{a.id}" in r[0][1] for r in found["buttons"][:-1])

    # 6. Явный ID
    post(client, **msg(204, text=f"#P{a.id} договорились"))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(text=f"#P{a.id} договорились").one().card_id == a.id

    # 7. Повтор того же update не создаёт дубль
    UPD[0] -= 1
    post(client, **msg(204, text=f"#P{a.id} договорились"))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(text=f"#P{a.id} договорились").count() == 1

    # 8. Редактирование сохраняет прошлую версию
    post(client, "edited_message", **msg(3, reply_to=1, text="Обсудили интеграцию и цены"))
    with SessionLocal() as s:
        n = s.query(Note).filter_by(tg_message_id=3).one()
        assert n.text == "Обсудили интеграцию и цены" and n.prev_text == "Обсудили интеграцию"

    # 9. Голосовое ответом на визитку
    post(client, **msg(205, reply_to=1, voice={"file_id": "v", "file_size": 10}))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(tg_message_id=205).one().card_id == a.id

    # 10. Чужой пользователь
    post(client, **msg(300, user=999, text="привет"))
    assert "Доступ закрыт" in tg.sent[-1]["text"] and "999" in tg.sent[-1]["text"]

    # 11. Ручная правка не затирается повторным OCR; похожий контакт помечается
    client.post("/login", data={"username": "anna", "password": "pw"})
    r = client.post(f"/c/{a.id}", data={"full_name": "Ivan Petrov", "phones": "+998901234567", "status": "agreed"},
                    follow_redirects=False)
    assert r.status_code == 303
    crm.run_ocr(a.id, None)
    with SessionLocal() as s:
        a2 = s.get(Card, a.id)
        assert a2.full_name == "Ivan Petrov" and a2.status == "agreed"
    post(client, **msg(301, photo=[{"file_id": "c", "file_size": 10}]))  # та же визитка ещё раз
    assert "Похоже на" in tg.sent[-1]["text"]

    # 12. Сайт
    assert "Ivan Petrov" in client.get("/").text
    assert "Анна" in client.get("/?q=999000").text
    page = client.get(f"/c/{a.id}").text
    assert "Через месяц" in page and "Прошлая версия" in page
    csv_text = client.get("/export.csv").text
    assert "Ivan Petrov" in csv_text and "Через месяц" in csv_text


def test_login_required(env):
    client, _ = env
    c2 = TestClient(main.app)
    assert c2.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c2.post("/tg/webhook", json={"update_id": 1}).status_code == 403


def test_ocr_merge_rules():
    card = Card(phones=["+998 90 1234567"], emails=[], socials=[], unreadable=[], edited_fields=["company"], company="Ручная")
    ocr.apply_result(card, {"full_name": "A", "company": "OCR", "phones": ["+998901234567", "90 111 22 33"],
                            "emails": ["bad email"], "socials": [], "all_text": "x", "uncertain_fields": ["address"]})
    assert card.company == "Ручная"
    assert card.phones == ["+998 90 1234567", "90 111 22 33"]
    assert "emails" in card.unreadable and "address" in card.unreadable


def test_pdf_to_image():
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Test")
    imgs = ocr.to_images(doc.tobytes(), "application/pdf")
    assert imgs and imgs[0][:4] == b"\x89PNG"


def test_voice_and_summaries(env):
    client, tg = env
    # новая визитка
    post(client, **msg(400, photo=[{"file_id": "v1", "file_size": 10}]))
    with SessionLocal() as s:
        card = last_card(s)
    # голосовое ответом на визитку → расшифровка + выжимка + ответ бота
    post(client, **msg(401, reply_to=400, voice={"file_id": "voice", "file_size": 10, "mime_type": "audio/ogg"}))
    with SessionLocal() as s:
        n = s.query(Note).filter_by(tg_message_id=401).one()
        assert n.card_id == card.id and n.ai_status == "done"
        assert n.transcript.startswith("Обсудили поставку") and n.summary.startswith("КРАТКО: Обсудили")
        assert s.get(Card, card.id).summary.startswith("Кто:")
    voice_reply = tg.sent[-1]
    assert voice_reply["reply_to"] == 401 and "Кратко" in voice_reply["text"] and "blockquote expandable" in voice_reply["text"]
    # ответ на сообщение с расшифровкой — к той же визитке
    post(client, **msg(402, reply_to=voice_reply["id"], text="короткая"))
    with SessionLocal() as s:
        short = s.query(Note).filter_by(tg_message_id=402).one()
        assert short.card_id == card.id and short.summary is None
        assert "Заметок: 2" in s.get(Card, card.id).summary
    # длинный текст → выжимка
    long_text = "Очень длинная заметка " * 20
    post(client, **msg(403, reply_to=400, text=long_text))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(tg_message_id=403).one().summary.startswith("КРАТКО")
    # голосовое без ответа: спрашивает «К кому это?», расшифровку присылает сразу; после выбора — сводка обновляется
    post(client, **msg(404, voice={"file_id": "v", "file_size": 10}))
    ask = next(m for m in reversed(tg.sent) if "К кому это" in m["text"])
    assert "Кратко" in tg.sent[-1]["text"]
    cb = next(row[0][1] for row in ask["buttons"] if row[0][1].endswith(f":{card.id}"))
    post(client, "callback_query", id="q2", data=cb, **{"from": {"id": 1}, "message": {"message_id": ask["id"], "chat": {"id": 1}}})
    with SessionLocal() as s:
        assert s.query(Note).filter_by(tg_message_id=404).one().card_id == card.id
        assert "Заметок: 4" in s.get(Card, card.id).summary
    # ошибка расшифровки: аудио сохранено, бот сообщает, заметка помечена
    AI["fail"] = True
    post(client, **msg(405, reply_to=400, voice={"file_id": "v", "file_size": 10}))
    AI["fail"] = False
    assert "Не удалось расшифровать" in tg.sent[-1]["text"]
    with SessionLocal() as s:
        n = s.query(Note).filter_by(tg_message_id=405).one()
        assert n.ai_status == "error" and n.attachment_id
    # карточка в боте показывает сводку
    post(client, "callback_query", id="q3", data=f"c:{card.id}", **{"from": {"id": 1}, "message": {"message_id": 1, "chat": {"id": 1}}})
    assert "Сводка" in tg.sent[-1]["text"]
    # сайт: сводка, расшифровка, повтор обработки, поиск по расшифровке
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get(f"/c/{card.id}").text
    assert "Сводка" in page and "Расшифровка" in page and "Повторить" in page
    with SessionLocal() as s:
        failed_id = s.query(Note).filter_by(tg_message_id=405).one().id
    client.post(f"/n/{failed_id}/ai")
    with SessionLocal() as s:
        assert s.get(Note, failed_id).ai_status == "done"
    assert f"#P{card.id}" in client.get("/?q=презентация").text
    # аудиофайл через сайт расшифровывается
    client.post(f"/c/{card.id}/note", data={"text": ""}, files={"file": ("rec.m4a", b"123", "audio/mp4")})
    with SessionLocal() as s:
        web_note = s.query(Note).filter_by(card_id=card.id, source="web").one()
        assert web_note.transcript and web_note.summary
    assert "Сводка" in client.get("/export.csv").text


def test_migration_adds_columns(tmp_path):
    """База, созданная первой версией (без новых колонок), дополняется при старте."""
    from sqlalchemy import create_engine, inspect, text

    from app.db import migrate

    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE cards (id INTEGER PRIMARY KEY, full_name VARCHAR(300))"))
        c.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)"))
        c.execute(text("INSERT INTO notes (id, text) VALUES (1, 'старая')"))
    migrate(eng)
    migrate(eng)  # повторный запуск ничего не ломает
    cols = {c["name"] for c in inspect(eng).get_columns("notes")}
    assert {"transcript", "summary", "ai_status", "ai_error"} <= cols
    assert {"summary", "summary_at"} <= {c["name"] for c in inspect(eng).get_columns("cards")}
    with eng.begin() as c:
        assert c.execute(text("SELECT ai_status FROM notes WHERE id=1")).scalar() == "none"


def ask_and_press_new(client, tg, mid, **content):
    post(client, **msg(mid, **content))
    ask = next(m for m in reversed(tg.sent) if "К кому это" in m["text"])
    cb = next(row[0][1] for row in ask["buttons"] if row[0][0].startswith("➕"))
    post(client, "callback_query", id=f"n{mid}", data=cb,
         **{"from": {"id": 1}, "message": {"message_id": ask["id"], "chat": {"id": 1}}})
    return ask


def test_new_contact_from_text(env):
    client, tg = env
    ask_and_press_new(client, tg, 500, text="Мария Ким из Globex, +998915554433, maria собака globex.uz, хотят пилот")
    assert any("Новый контакт" in e["text"] for e in tg.edits)
    with SessionLocal() as s:
        card = last_card(s)
        assert card.full_name == "Мария Ким" and card.company == "Globex" and card.phones == ["+998915554433"]
        assert card.ocr_status == "none" and "position" in card.unreadable and card.summary
        note = s.query(Note).filter_by(tg_message_id=500).one()
        assert note.card_id == card.id
    assert "Мария Ким" in tg.sent[-1]["text"] and tg.sent[-1]["reply_to"] == 500
    # ответ на присланную карточку — к этому же контакту
    post(client, **msg(501, reply_to=tg.sent[-1]["id"], text="созвон в пятницу"))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(tg_message_id=501).one().card_id == card.id


def test_new_contact_from_voice_while_transcribing(env):
    client, tg = env
    queued = []
    crm.spawn = lambda fn, *a: queued.append((fn, a))  # расшифровка «ещё идёт»
    try:
        ask_and_press_new(client, tg, 510, voice={"file_id": "v", "file_size": 10})
        with SessionLocal() as s:
            card = last_card(s)
            assert card.ocr_status == "from_note" and card.full_name is None
            assert card.title == f"Контакт #P{card.id}"
        assert len(queued) == 1 and queued[0][0] is crm.process_note  # заполнение ждёт расшифровку
    finally:
        crm.spawn = lambda fn, *a: fn(*a)
    fn, args = queued[0]
    fn(*args)
    with SessionLocal() as s:
        card = s.get(Card, card.id)
        assert card.full_name == "Пётр" and card.company == "Globex" and card.ocr_status == "none"
        assert card.summary
    assert "Пётр" in tg.sent[-1]["text"]


def test_new_contact_nothing_found(env):
    client, tg = env
    ask_and_press_new(client, tg, 520, text="просто мысль без имени")
    with SessionLocal() as s:
        card = last_card(s)
        assert card.ocr_status == "none" and card.title == f"Контакт #P{card.id}"
    assert "поправить в CRM" in tg.sent[-1]["text"]


def test_tall_screenshot_is_split_into_fragments():
    from PIL import Image as Img

    shot = Img.new("RGB", (1170, 2532), "white")
    buf = io.BytesIO()
    shot.save(buf, "PNG")
    parts = ocr.to_images(buf.getvalue(), "image/png")
    assert 3 <= len(parts) <= 5  # обзор + фрагменты
    tiles = ocr.split_tall(shot)[1:]
    assert tiles[0].size[1] < 2532 and tiles[-1].getbbox() is not None
    # фрагменты с перекрытием покрывают весь скриншот: сумма высот больше исходной
    assert sum(t.size[1] for t in tiles) > 2532
    card = Img.new("RGB", (1000, 600), "white")  # обычная визитка не режется
    assert len(ocr.split_tall(card)) == 1


def test_prompt_prefers_recipient_email():
    assert "Кому" in ocr.PROMPT and "IGNORE" in ocr.PROMPT


def test_websummit_whatsapp_linkedin_screenshots(env):
    client, tg = env
    ws = {"full_name": "Massimiliano Sulpizi", "position": "Serial Entrepreneur | Investor", "company": "EquityMatch.co",
          "phones": [], "emails": [], "website": None, "address": "London, United Kingdom", "country": "United Kingdom",
          "socials": [], "about": "Role at event: INVESTOR\nI'm a serial entrepreneur, six companies established.\nIndustry: Venture capital & private equity",
          "messages": ["Hi Nikita, let's meet at the summit. Join me on EquityMatch.co.\nBest.\nMax"],
          "all_text": "Massimiliano Sulpizi", "uncertain_fields": []}
    OCR_RESULTS["next"] = ws
    post(client, **msg(600, document={"file_id": "ws", "mime_type": "image/png", "file_name": "ws.png", "file_size": 10}))
    with SessionLocal() as s:
        card = last_card(s)
        assert card.about.startswith("Role at event") and card.summary
        msgs = s.query(Note).filter_by(card_id=card.id, source="ocr").all()
        assert len(msgs) == 1 and msgs[0].author == "Сообщение контакта" and "let's meet" in msgs[0].text
    # WhatsApp-скрин ответом на визитку: номер → телефоны + WhatsApp, сообщение не дублируется
    OCR_RESULTS["next"] = {**{k: None for k in ("full_name", "position", "company", "website", "address", "country", "about")},
                           "phones": ["+44 7539 049424"], "emails": [], "socials": ["WhatsApp: +44 7539 049424"],
                           "messages": ws["messages"], "all_text": "+44 7539 049424", "uncertain_fields": []}
    post(client, **msg(601, reply_to=600, document={"file_id": "wa", "mime_type": "image/png", "file_name": "wa.png", "file_size": 10}))
    # LinkedIn: описание дополняется, а не затирается
    OCR_RESULTS["next"] = {**OCR_RESULTS["next"], "phones": [], "socials": [], "messages": [],
                           "position": "Entrepreneur | Angel Investor", "about": "Board Member, Speaker"}
    post(client, **msg(602, reply_to=600, document={"file_id": "li", "mime_type": "image/png", "file_name": "li.png", "file_size": 10}))
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        card = s.get(Card, card.id)
        assert card.phones == ["+447539049424"] and card.socials == ["WhatsApp: +447539049424"]
        assert card.whatsapp_numbers == ["447539049424"]
        assert card.position == "Serial Entrepreneur | Investor"  # первое значение не затирается
        assert "Board Member" in card.about and card.about.count("serial entrepreneur") == 1
        assert s.query(Note).filter_by(card_id=card.id, source="ocr").count() == 1
    wa_msg = tg.sent[-1]
    assert any(btn[1] == "https://wa.me/447539049424" for row in wa_msg["buttons"] for btn in row)
    assert "💬 WhatsApp" in wa_msg["text"]
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get(f"/c/{card.id}").text
    assert 'href="https://wa.me/447539049424"' in page
    assert "linkedin.com/search/results/people/?keywords=Massimiliano%20Sulpizi%20EquityMatch.co" in page
    assert "со скриншота" in page and "О контакте" in page
    # ручной ввод: строка WhatsApp в соцсетях даёт кнопку; ссылка LinkedIn — прямая
    client.post(f"/c/{card.id}", data={"full_name": card.full_name, "company": card.company, "position": card.position,
                                       "about": card.about, "address": card.address, "country": card.country,
                                       "phones": "+447539049424", "socials": "WhatsApp: +998 90 111 22 33\nLinkedIn: linkedin.com/in/msulpizi",
                                       "status": "new"})
    with SessionLocal() as s:
        card = s.get(Card, card.id)
        assert card.whatsapp_numbers == ["998901112233"]
        assert card.linkedin_url == "https://linkedin.com/in/msulpizi"


def test_whatsapp_without_country_code_has_no_button():
    c = Card(socials=["WhatsApp: 90 111 22 33", "Telegram: @x"])
    assert c.whatsapp_numbers == []
    assert Card(full_name=None).linkedin_url is None


def test_tags(env):
    client, tg = env
    post(client, **msg(700, photo=[{"file_id": "t1", "file_size": 10}]))
    with SessionLocal() as s:
        card = last_card(s)
    summary_msg = tg.sent[-1]
    labels = [b[0] for row in summary_msg["buttons"] for b in row]
    assert "🤝 Партнёр" in labels and "🔥 Горячий" in labels and "📱 Приложение WS" in labels

    def press(group, value):
        post(client, "callback_query", id="t", data=f"t:{card.id}:{group}:{value}",
             **{"from": {"id": 1}, "message": {"message_id": summary_msg["id"], "chat": {"id": 1}}})
        with SessionLocal() as s:
            return s.get(Card, card.id)

    c = press("kind", "partner")
    assert c.kind == "partner" and "kind" in c.edited_fields
    assert any("✅ 🤝 Партнёр" == b[0] for row in tg.edits[-1]["buttons"] for b in row)
    c = press("kind", "investor")  # в группе только одно значение
    assert c.kind == "investor"
    c = press("kind", "investor")  # повторное нажатие снимает
    assert c.kind is None
    c = press("temp", "warm")
    c = press("channel", "booth")
    assert (c.temp, c.channel) == ("warm", "booth")

    # прямые слова в заметке меняют тег и бот сообщает
    post(client, **msg(701, reply_to=700, text="Горячий инвестор, хочет созвон"))
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert (c.kind, c.temp, c.channel) == ("investor", "hot", "booth")
    assert "🏷 🔥 Горячий" in tg.sent[-1]["text"] and "🏷 💰 Инвестор" in tg.sent[-1]["text"]
    # заметка без тегов ничего не меняет и не шлёт лишнего
    n_sent = len(tg.sent)
    post(client, **msg(702, reply_to=700, text="созвон в 15:00"))
    assert len(tg.sent) == n_sent + 1  # только «Добавлено к»

    # автоматика со скриншота не перебивает выбор человека, но заполняет пустое
    OCR_RESULTS["next"] = {**fake_extract([]), "kind": "partner", "channel": "ws_app"}
    crm.run_ocr(card.id, None)
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert (c.kind, c.channel) == ("investor", "booth")
    post(client, **msg(703, document={"file_id": "ws2", "mime_type": "image/png", "file_name": "p.png", "file_size": 10}))
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        c2 = last_card(s)
        assert (c2.kind, c2.channel) == ("partner", "ws_app") and "kind" not in (c2.edited_fields or [])

    # теги из голосового без привязки переходят на контакт при выборе
    post(client, **msg(704, text="холодный партнёр со стенда"))
    ask = next(m for m in reversed(tg.sent) if "К кому это" in m["text"])
    post(client, "callback_query", id="a", data=f"a:{ask_note_id(704)}:{c2.id}",
         **{"from": {"id": 1}, "message": {"message_id": ask["id"], "chat": {"id": 1}}})
    with SessionLocal() as s:
        c2 = s.get(Card, c2.id)
        assert (c2.kind, c2.temp, c2.channel) == ("partner", "cold", "booth")

    # сайт: фильтры, метки, правка тегов формой
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/?temp=hot&kind=investor").text
    assert f"#P{card.id}" in page and f"#P{c2.id}" not in page and "🔥 Горячий" in page
    assert f"#P{c2.id}" in client.get("/?channel=booth&temp=cold").text
    form = client.get(f"/c/{card.id}").text
    assert 'name="temp" value="hot" checked' in form
    client.post(f"/c/{card.id}", data={"temp": "warm", "kind": "investor", "channel": "booth", "status": "new"})
    with SessionLocal() as s:
        assert s.get(Card, card.id).temp == "warm"
    assert "Температура" in client.get("/export.csv").text and "🌤 Тёплый" in client.get("/export.csv").text


def ask_note_id(mid):
    with SessionLocal() as s:
        return s.query(Note).filter_by(tg_message_id=mid).one().id


LIST_RESULT = {
    "source": "whatsapp", "group_name": "Founders & CEOs @ Web Summit",
    "people": [
        {"name": "~Marysol", "subtitle": None, "position": None, "company": None, "website": None,
         "phone": "+353 86 034 8480", "username": None, "cut_off": False},
        {"name": "~Mert", "subtitle": "Paraltera Founder & CEO", "position": "Founder & CEO", "company": "Paraltera",
         "website": None, "phone": None, "username": "@mozguler", "cut_off": False},
        {"name": "~Med Said Meliji", "subtitle": "www.invisionpixels.net", "position": None, "company": None,
         "website": "www.invisionpixels.net", "phone": None, "username": "s.meliji", "cut_off": False},
        {"name": "~Mert", "subtitle": "Paraltera Founder & CEO", "position": "Founder & CEO", "company": "Paraltera",
         "website": None, "phone": None, "username": "@mozguler", "cut_off": False},  # дубль из фрагмента
        {"name": "~Rob Parker", "subtitle": None, "position": None, "company": None, "website": None,
         "phone": "+1 (250) 650-8787", "username": None, "cut_off": False},
        {"name": "~Amar", "subtitle": "Making Manufacturing Easy", "position": None, "company": None, "website": None,
         "phone": None, "username": None, "cut_off": True},
    ],
}


def test_list_import(env):
    client, tg = env
    ocr.extract_people = lambda files: LIST_RESULT
    # Rob Parker уже есть в базе с горячим тегом
    with SessionLocal() as s:
        rob = Card(full_name="Rob Parker", phones=["+12506508787"], temp="hot", edited_fields=["temp"], source="web")
        s.add(rob)
        s.commit()
        rob_id = rob.id
    post(client, **msg(800, text="/list"))
    assert "Режим списка" in tg.sent[-1]["text"]
    post(client, **msg(801, document={"file_id": "lst", "mime_type": "image/png", "file_name": "l.png", "file_size": 10}))
    report = tg.sent[-1]
    assert "Добавлено: 3" in report["text"] and "Founders &amp; CEOs @ Web Summit" in report["text"]
    assert "Уже были в базе, дополнены: 1" in report["text"] and "обрезанных строк: 1" in report["text"]
    with SessionLocal() as s:
        new = s.query(Card).filter_by(source="list").order_by(Card.id).all()
        assert [c.full_name for c in new] == ["Marysol", "Mert", "Med Said Meliji"]
        assert all(c.temp == "cold" and c.channel == "wa_base" for c in new)
        marysol, mert, med = new
        assert marysol.whatsapp_numbers == ["353860348480"] and marysol.phones == ["+353860348480"]
        assert mert.company == "Paraltera" and mert.socials == ["WhatsApp: @mozguler"] and mert.whatsapp_numbers == []
        assert med.website == "www.invisionpixels.net" and "WhatsApp: @s.meliji" in med.socials
        assert "Founders & CEOs" in mert.about and "Подпись: Paraltera" in mert.about
        rob = s.get(Card, rob_id)
        assert rob.temp == "hot" and rob.channel == "wa_base" and "WhatsApp: +12506508787" in rob.socials
        batch = marysol.batch
    # второй скрин с перекрытием — повторов нет
    post(client, **msg(802, document={"file_id": "lst2", "mime_type": "image/png", "file_name": "l2.png", "file_size": 10}))
    assert "Добавлено: 0" in tg.sent[-1]["text"] and "дополнены: 4" in tg.sent[-1]["text"]
    # сайт: фильтр по пачке
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get(f"/?batch={batch}").text
    assert "Marysol" in page and "Rob Parker" not in page
    # отмена импорта: контакт с заметкой остаётся
    with SessionLocal() as s:
        s.add(Note(card_id=mert.id, text="написал ему"))
        s.commit()
    btn = next(b for row in report["buttons"] for b in row if b[1].startswith("lu:"))
    post(client, "callback_query", id="u", data=btn[1], **{"from": {"id": 1}, "message": {"message_id": report["id"], "chat": {"id": 1}}})
    assert "удалено контактов — 2" in tg.edits[-1]["text"] and "Оставлено 1" in tg.edits[-1]["text"]
    with SessionLocal() as s:
        assert [c.full_name for c in s.query(Card).filter_by(source="list")] == ["Mert"]
        assert s.get(Card, rob_id) is not None
    # выключение режима — фото снова визитка
    post(client, **msg(803, text="/done"))
    n_before = len(tg.sent)
    post(client, **msg(804, photo=[{"file_id": "card", "file_size": 10}]))
    assert "Визитка принята" in tg.sent[n_before]["text"]


def test_list_mode_expires(env):
    client, tg = env
    from datetime import timedelta

    from app.db import ChatMode, now
    post(client, **msg(810, text="/list"))
    with SessionLocal() as s:
        s.get(ChatMode, 1).until = now() - timedelta(minutes=1)
        s.commit()
    n_before = len(tg.sent)
    post(client, **msg(811, photo=[{"file_id": "card2", "file_size": 10}]))
    assert "Визитка принята" in tg.sent[n_before]["text"]


def test_list_whatsapp_detected_by_tilde_even_if_model_says_telegram(env):
    client, tg = env
    ocr.extract_people = lambda files: {"source": "telegram", "group_name": "Founders & CEOs @ Web Summit", "people": [
        {"name": "Saeed", "subtitle": "NetGreener, CEO", "position": "CEO", "company": "NetGreener", "website": None,
         "phone": "+1 (514) 262-3792", "username": None, "cut_off": False, "tilde": True},
        {"name": "Motin", "subtitle": "Founder and CEO Optyv", "position": "Founder and CEO", "company": "Optyv",
         "website": None, "phone": None, "username": "@mrqmotin", "cut_off": False, "tilde": True}]}
    post(client, **msg(920, text="/list"))
    post(client, **msg(921, document={"file_id": "g2", "mime_type": "image/png", "file_name": "g.png", "file_size": 10}))
    post(client, **msg(922, text="/done"))
    assert "Из группы WhatsApp" in next(m for m in reversed(tg.sent) if "Добавлено" in m["text"])["text"]
    with SessionLocal() as s:
        saeed = s.query(Card).filter_by(full_name="Saeed").one()
        motin = s.query(Card).filter_by(full_name="Motin").one()
        assert saeed.channel == "wa_base" and saeed.whatsapp_numbers == ["15142623792"]
        assert motin.socials == ["WhatsApp: @mrqmotin"] and "Telegram" not in (motin.about or "")


def test_fix_batch_source_like_on_screenshot(env):
    """Контакт, ошибочно заведённый как Telegram (как на скрине), исправляется кнопкой на WhatsApp и обратно."""
    client, tg = env
    with SessionLocal() as s:
        med = Card(full_name="Med Said Meliji", source="list", batch="L777", temp="cold",
                   about="Из группы Telegram «Founders & CEOs @ Web Summit»", socials=["Telegram: @s.meliji"])
        sheila = Card(full_name="Sheila", source="list", batch="L777", temp="cold", phones=["+12145583809"],
                      about="Из группы Telegram «Founders & CEOs @ Web Summit»", socials=[])
        other = Card(full_name="Чужой", source="list", batch="L778", socials=["Telegram: @x"])
        s.add_all([med, sheila, other])
        s.commit()
        ids = (med.id, sheila.id, other.id)
    # кнопка в боте
    post(client, "callback_query", id="ls", data="ls:L777:whatsapp",
         **{"from": {"id": 1}, "message": {"message_id": 5, "chat": {"id": 1}}})
    assert "Источник исправлен у 2" in tg.sent[-1]["text"]
    with SessionLocal() as s:
        med, sheila, other = (s.get(Card, i) for i in ids)
        assert med.socials == ["WhatsApp: @s.meliji"] and med.about == "Из группы WhatsApp «Founders & CEOs @ Web Summit»"
        assert sheila.socials == ["WhatsApp: +12145583809"] and sheila.whatsapp_numbers == ["12145583809"]
        assert med.channel == sheila.channel == "wa_base"
        assert other.socials == ["Telegram: @x"]  # другая пачка не тронута
    # на сайте — обратно и снова (идемпотентно)
    client.post("/login", data={"username": "anna", "password": "pw"})
    assert "Источник неверный?" in client.get("/?batch=L777").text
    assert "пачка импорта" in client.get(f"/c/{ids[0]}").text
    client.post("/batch/L777/source", data={"to": "telegram"})
    with SessionLocal() as s:
        med, sheila = s.get(Card, ids[0]), s.get(Card, ids[1])
        assert med.socials == ["Telegram: @s.meliji"] and sheila.socials == [] and sheila.channel is None
    client.post("/batch/L777/source", data={"to": "whatsapp"})
    client.post("/batch/L777/source", data={"to": "whatsapp"})
    with SessionLocal() as s:
        sheila = s.get(Card, ids[1])
        assert sheila.socials == ["WhatsApp: +12145583809"] and sheila.channel == "wa_base"


def test_work_ball_steps_followup_and_digest(env):
    from app import work
    from app.db import AppState

    client, tg = env
    post(client, **msg(1000, photo=[{"file_id": "w1", "file_size": 10}]))
    with SessionLocal() as s:
        card = last_card(s)
        assert card.ball is None  # новый контакт — «не разобран»
    card_msg = tg.sent[-1]
    labels = [b[0] for row in card_msg["buttons"] for b in row]
    assert labels[:3] == ["🟢 Наш ход", "🟡 Ждём их", "⚪ Без шага"] and "☑️ Шаг сделан" in labels

    def cb(data, mid=None):
        post(client, "callback_query", id="w", data=data,
             **{"from": {"id": 1}, "message": {"message_id": mid or card_msg["id"], "chat": {"id": 1}}})
        with SessionLocal() as s:
            return s.get(Card, card.id)

    # голосом/текстом: касание, мяч, срок
    post(client, **msg(1001, reply_to=1000, text="Отправил презентацию в WhatsApp, жду ответа до пятницы"))
    reply = tg.sent[-1]["text"]
    assert "📌 Касание: Отправили презентацию" in reply and "⚽ Мяч: 🟡 Ждём их" in reply and "📅 Срок: " in reply
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert c.ball == "them" and c.due and c.due.weekday() == 4 and c.waiting_since
        n = s.query(Note).filter_by(tg_message_id=1001).one()
        assert n.kind == "touch" and n.touch == "Отправили презентацию"
        assert work.followup_date(c) == c.due + timedelta(days=1)

    # дожим: после срока мяч сам возвращается к нам
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        c.next_step = "ждём ответ по презентации"
        s.commit()
        assert work.expire_waiting(s, c.due) == []  # в сам срок ещё ждём
        back = work.expire_waiting(s, c.due + timedelta(days=1))
        s.commit()
        assert [x.id for x in back] == [card.id]
        c = s.get(Card, card.id)
        assert c.ball == "us" and c.next_step == "Напомнить о себе (ждали: ждём ответ по презентации)"
        assert s.query(Note).filter_by(card_id=card.id, kind="system").count() == 1

    # без срока — через FOLLOWUP_DAYS (2) дня
    c = cb(f"w:{card.id}:them")
    assert c.ball == "them" and c.due is None
    with SessionLocal() as s:
        start = work.local_date(s.get(Card, card.id).waiting_since)
        assert work.expire_waiting(s, start + timedelta(days=1)) == []
        assert len(work.expire_waiting(s, start + timedelta(days=2))) == 1
        s.commit()

    # шаг сделан → касание → «что дальше» → новый шаг текстом
    c = cb(f"wd:{card.id}")
    assert c.next_step is None
    assert "Что дальше?" in tg.sent[-1]["text"]
    with SessionLocal() as s:
        assert s.query(Note).filter_by(card_id=card.id, kind="touch").order_by(Note.id.desc()).first().touch.startswith("✅ Напомнить")
    cb(f"wn:{card.id}")
    prompt = tg.sent[-1]
    assert "Следующий шаг" in prompt["text"]
    post(client, **msg(1002, reply_to=prompt["id"], text="созвон завтра"))
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert c.next_step == "Созвон завтра" and c.ball == "us" and c.due == work.today() + timedelta(days=1)
        assert s.query(Note).filter_by(tg_message_id=1002).count() == 0  # это не заметка
    # срок кнопками
    cb(f"wt:{card.id}")
    due_msg = tg.sent[-1]
    assert "Срок для" in due_msg["text"]
    c = cb(f"wdue:{card.id}:7", mid=due_msg["id"])
    assert c.due == work.today() + timedelta(days=7)
    c = cb(f"wdue:{card.id}:x", mid=due_msg["id"])
    assert c.due is None

    # /todo
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        c.due = work.today() - timedelta(days=2)
        s.commit()
    post(client, **msg(1003, text="/todo"))
    todo_text = tg.sent[-1]["text"]
    assert "🟢 Наш ход:" in todo_text and "просрочено: " in todo_text and f"#P{card.id}" in todo_text
    assert "📥 Не разобраны:" in todo_text
    assert any(b[1] == f"c:{card.id}" for row in tg.sent[-1]["buttons"] for b in row)

    # утренняя сводка: пн 9:10 — да (один раз), 8:59 и суббота — нет
    with SessionLocal() as s:
        st = s.get(AppState, "digest_date")
        if st:
            s.delete(st)
            s.commit()
    mon = datetime(2026, 11, 23, 9, 10, tzinfo=work.TZ)
    assert work.tick(tg, crm.LOCK, at=mon.replace(hour=8, minute=59)) is False
    before = len(tg.sent)
    assert work.tick(tg, crm.LOCK, at=mon) is True
    assert len(tg.sent) == before + len(settings.allowed_ids) and "Доброе утро" in tg.sent[-1]["text"]
    assert work.tick(tg, crm.LOCK, at=mon.replace(hour=11)) is False  # уже отправляли сегодня
    assert work.tick(tg, crm.LOCK, at=datetime(2026, 11, 28, 9, 30, tzinfo=work.TZ)) is False  # суббота

    # сайт: страница «Работа», быстрые действия, блок в карточке
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/work").text
    assert "🟢 Наш ход" in page and "🟡 Ждём их" in page and "📥 Не разобраны" in page and "просрочено" in page
    client.post(f"/c/{card.id}/work", data={"action": "done", "then": "them"}, headers={"referer": "http://x/work"})
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert c.ball == "them" and c.next_step is None
    client.post(f"/c/{card.id}/work", data={"ball": "us", "next_step": "Прислать КП", "due": "2026-12-01"})
    client.post(f"/c/{card.id}/work", data={"action": "touch", "touch": "Созвонились, всё ок"})
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert (c.ball, c.next_step, c.due.isoformat()) == ("us", "Прислать КП", "2026-12-01")
        assert s.query(Note).filter_by(card_id=card.id, touch="Созвонились, всё ок", kind="touch").count() == 1
    card_page = client.get(f"/c/{card.id}").text
    assert 'value="Прислать КП"' in card_page and "📌 Созвонились, всё ок" in card_page
    assert "🟢 Наш ход" in client.get("/").text


def test_work_untriaged_list_import_and_filters(env):
    from app import work
    client, _ = env
    with SessionLocal() as s:
        cold = Card(full_name="Холодный из списка", source="list", temp="cold")
        hot = Card(full_name="Горячий без шага", temp="hot")
        s.add_all([cold, hot])
        s.commit()
        t = work.todo(s)
        ids = [c.id for c in t["untriaged"]]
        assert ids.index(hot.id) < ids.index(cold.id)  # горячие разбирать первыми
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/work?temp=hot").text
    assert "Горячий без шага" in page and "Холодный из списка" not in page


def test_filter_by_author(env):
    client, _ = env
    with SessionLocal() as s:
        s.add_all([Card(full_name="Контакт Жени", created_by="Jane", ball="us", next_step="позвонить"),
                   Card(full_name="Контакт коллеги", created_by="Surnik", ball="us", next_step="написать")])
        s.commit()
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/?author=Jane").text
    assert "Контакт Жени" in page and "Контакт коллеги" not in page
    assert 'name="author" value="Surnik"' in page and "👤 Jane" in page
    work_page = client.get("/work?author=Surnik").text
    assert "Контакт коллеги" in work_page and "Контакт Жени" not in work_page
    assert "Контакт коллеги" in client.get("/?author=Surnik&q=Контакт").text


def test_gender(env):
    client, tg = env
    # со скриншота/визитки — автоматически (явное имя/обращение), в боте — значок у имени
    OCR_RESULTS["next"] = {**fake_extract([]), "full_name": "Maria Lopez", "phones": ["+34 600 000 001"],
                           "emails": ["maria@lopez.es"], "gender": "female"}
    post(client, **msg(1100, document={"file_id": "g1", "mime_type": "image/png", "file_name": "m.png", "file_size": 10}))
    OCR_RESULTS.clear()
    assert "Maria Lopez 👩" in tg.sent[-1]["text"]
    with SessionLocal() as s:
        maria = last_card(s)
        assert maria.gender == "female" and "gender" not in (maria.edited_fields or [])
    # неоднозначное имя — не определено
    OCR_RESULTS["next"] = {**fake_extract([]), "full_name": "Alex Kim", "phones": ["+82 10 0000 0002"],
                           "emails": ["alex@kim.kr"], "gender": None}
    post(client, **msg(1101, document={"file_id": "g2", "mime_type": "image/png", "file_name": "a.png", "file_size": 10}))
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        alex = last_card(s)
        assert alex.gender is None
    # словами в заметке — ставится и закрепляется
    post(client, **msg(1102, reply_to=1101, text="Это женщина, CEO, очень интересуется"))
    assert "👩 Пол: женщина" in tg.sent[-1]["text"]
    with SessionLocal() as s:
        alex = s.get(Card, alex.id)
        assert alex.gender == "female" and "gender" in alex.edited_fields
    # автоматика не перебивает выбор человека
    OCR_RESULTS["next"] = {**fake_extract([]), "gender": "male"}
    crm.run_ocr(alex.id, None)
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        assert s.get(Card, alex.id).gender == "female"
    # сайт: правка, фильтр, CSV
    client.post("/login", data={"username": "anna", "password": "pw"})
    client.post(f"/c/{maria.id}", data={"gender": "", "status": "new"})
    with SessionLocal() as s:
        assert s.get(Card, maria.id).gender is None and "gender" in s.get(Card, maria.id).edited_fields
    client.post(f"/c/{maria.id}", data={"gender": "female", "status": "new"})
    page = client.get("/?gender=female").text
    assert "Maria Lopez" in page and "Alex Kim" in page and "Ivan" not in page
    assert "Maria Lopez" not in client.get("/?gender=none").text
    assert 'name="gender" value="female" checked' in client.get(f"/c/{maria.id}").text
    assert "Пол" in client.get("/export.csv").text and "Женщина" in client.get("/export.csv").text


def test_gender_from_list_import(env):
    client, tg = env
    ocr.extract_people = lambda files: {"source": "whatsapp", "group_name": None, "people": [
        {"name": "~Sakine Gulec", "subtitle": None, "position": None, "company": None, "website": None,
         "phone": "+1 (305) 812-6537", "username": None, "cut_off": False, "tilde": True, "gender": "female"},
        {"name": "~Saeed2", "subtitle": None, "position": None, "company": None, "website": None,
         "phone": "+1 (514) 262-0000", "username": None, "cut_off": False, "tilde": True, "gender": None}]}
    post(client, **msg(1110, text="/list"))
    post(client, **msg(1111, document={"file_id": "gl", "mime_type": "image/png", "file_name": "g.png", "file_size": 10}))
    post(client, **msg(1112, text="/done"))
    with SessionLocal() as s:
        assert s.query(Card).filter_by(full_name="Sakine Gulec").one().gender == "female"
        assert s.query(Card).filter_by(full_name="Saeed2").one().gender is None


def test_filter_by_added_date(env):
    from app import work
    client, _ = env
    t = work.today()
    noon = lambda d: datetime(d.year, d.month, d.day, 12, 0, tzinfo=work.TZ)  # noqa: E731
    with SessionLocal() as s:
        s.add_all([Card(full_name="Добавлен сегодня", created_at=noon(t)),
                   Card(full_name="Добавлен вчера", created_at=noon(t - timedelta(days=1))),
                   Card(full_name="Добавлен месяц назад", created_at=noon(t - timedelta(days=40)), ball="us")])
        s.commit()
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/?period=today").text
    assert "Добавлен сегодня" in page and "Добавлен вчера" not in page
    page = client.get("/?period=yesterday").text
    assert "Добавлен вчера" in page and "Добавлен сегодня" not in page
    page = client.get("/?period=7d").text
    assert "Добавлен вчера" in page and "Добавлен месяц назад" not in page
    d40 = (t - timedelta(days=41)).isoformat()
    d39 = (t - timedelta(days=39)).isoformat()
    page = client.get(f"/?date_from={d40}&date_to={d39}").text
    assert "Добавлен месяц назад" in page and "Добавлен вчера" not in page and f'value="{d40}"' in page
    assert "Добавлен месяц назад" not in client.get("/work?period=30d").text
    assert "Добавлен месяц назад" in client.get(f"/work?date_from={d40}").text
    with SessionLocal() as s:
        cid = s.query(Card).filter_by(full_name="Добавлен вчера").one().id
    assert "📅 Добавлен <b>" in client.get(f"/c/{cid}").text


def test_country_resolution():
    from app import countries as co
    c = Card(country="United Kingdom")
    assert c.country_info == ("GB", "country") and c.country_label == "🇬🇧 Великобритания"
    assert Card(country="UK").country_info[0] == "GB" and Card(country="Великобритания").country_info[0] == "GB"
    assert Card(address="Maia, Portugal").country_info == ("PT", "address")
    assert Card(address="London").country_info == ("GB", "address")
    assert Card(address="Лондон, Англия, Соединенное Королевство").country_info[0] == "GB"
    assert Card(phones=["+447539049424"]).country_info == ("GB", "phone")
    assert Card(phones=["+998901234567"]).country_info == ("UZ", "phone")
    assert Card(phones=["+77011234567"]).country_info[0] == "KZ" and Card(phones=["+79161234567"]).country_info[0] == "RU"
    assert Card(phones=["+12145583809"]).country_label == "🇺🇸🇨🇦 США/Канада"
    assert Card(phones=["90 123 45 67"]).country_info == (None, None)  # без кода страны — не угадываем
    assert Card(socials=["WhatsApp: +353860348480"]).country_info == ("IE", "phone")
    assert Card(country="Portugal", phones=["+44 1"]).country_info == ("PT", "country")  # явная страна важнее номера
    assert Card(address="Rua Augusta 10, 1100 Lisboa").country_info[0] == "PT"
    assert co.from_text("Rua Augusta 10") is None


def test_country_filter(env):
    client, _ = env
    with SessionLocal() as s:
        s.add_all([Card(full_name="Из Португалии", address="Maia, Portugal", ball="us"),
                   Card(full_name="Британец по номеру", phones=["+447700900123"]),
                   Card(full_name="Без страны")])
        s.commit()
        pt_id = s.query(Card).filter_by(full_name="Из Португалии").one().id
        gb_id = s.query(Card).filter_by(full_name="Британец по номеру").one().id
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get("/?country=PT").text
    assert "Из Португалии" in page and "Британец по номеру" not in page and "🇵🇹 Португалия (" in page
    page = client.get("/?country=GB").text
    assert "Британец по номеру" in page and "· по номеру" in page
    assert "Без страны" in client.get("/?country=none").text and "Из Португалии" not in client.get("/?country=none").text
    assert "Из Португалии" in client.get("/work?country=PT").text
    assert "по коду телефона" in client.get(f"/c/{gb_id}").text and "— по адресу" in client.get(f"/c/{pt_id}").text
    assert "Великобритания (по номеру)" in client.get("/export.csv").text


def test_needs_translation():
    from app.translate import needs_translation as nt
    assert nt("Serial Entrepreneur | Investor") and nt("CEO") and nt("Bosh direktor") and nt("London, United Kingdom")
    assert not nt("Генеральный директор") and not nt("Москва, ул. Тверская 1")
    assert not nt("maria@globex.uz") and not nt("+44 7539 049424") and not nt("www.equitymatch.co") and not nt("")
    assert nt("Hi Nikita, let's meet at the summit.")


def test_translations(env):
    client, tg = env
    OCR_RESULTS["next"] = {**fake_extract([]), "full_name": "Massimiliano Sulpizi Tr", "company": "EquityMatch.co",
                           "position": "Serial Entrepreneur | Investor", "phones": ["+44 7539 000111"],
                           "emails": ["max@equitymatch.co"], "address": "London, United Kingdom",
                           "about": "I'm a serial entrepreneur, six companies established.",
                           "messages": ["Hi Nikita, let's meet at the summit."]}
    post(client, **msg(1200, document={"file_id": "tr", "mime_type": "image/png", "file_name": "t.png", "file_size": 10}))
    OCR_RESULTS.clear()
    with SessionLocal() as s:
        card = last_card(s)
        assert card.ru("position") == "RU: Serial Entrepreneur | Investor"
        assert card.ru("about").startswith("RU: I'm a serial") and card.ru("address") == "RU: London, United Kingdom"
        assert "full_name" not in (card.translations or {}) and "company" not in (card.translations or {})
        msg_note = s.query(Note).filter_by(card_id=card.id, source="ocr").one()
        assert msg_note.text_ru == "RU: Hi Nikita, let's meet at the summit."
    assert "🇷🇺 RU: Serial Entrepreneur" in tg.sent[-1]["text"]  # в боте — перевод должности
    # русскую заметку не переводим
    post(client, **msg(1201, reply_to=1200, text="Созвонились, всё хорошо"))
    with SessionLocal() as s:
        assert s.query(Note).filter_by(tg_message_id=1201).one().text_ru is None
    # сайт: перевод рядом с оригиналом
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get(f"/c/{card.id}").text
    assert "🇷🇺 RU: Serial Entrepreneur" in page and "🇷🇺 RU: Hi Nikita" in page
    assert "🇷🇺 RU: Serial Entrepreneur" in client.get("/?q=Sulpizi").text
    # правка поля на сайте — перевод обновляется
    client.post(f"/c/{card.id}", data={"position": "Angel Investor"})
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        assert c.ru("position") == "RU: Angel Investor" and c.ru("about").startswith("RU: I'm")
        # если поле поменяли, а перевод ещё не готов — старый перевод не показываем
        c.position = "Board Member"
        assert c.ru("position") is None
    assert "Должность (рус)" in client.get("/export.csv").text
    # ошибка перевода не ломает ничего
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        c.about = "FAIL text in English"
        s.commit()
    assert translate.run_translate(card.id) == 0
    with SessionLocal() as s:
        assert s.get(Card, card.id).ru("about") is None and s.get(Card, card.id).about == "FAIL text in English"


def test_translation_backfill(env):
    with SessionLocal() as s:
        old = Card(full_name="Старый контакт EN", position="Head of Partnerships")
        ru = Card(full_name="Русский", position="Директор")
        s.add_all([old, ru])
        s.commit()
        ids = (old.id, ru.id)
    queued = []
    real = translate.enqueue
    translate.enqueue = queued.append
    try:
        translate.backfill()
    finally:
        translate.enqueue = real
    assert ids[0] in queued and ids[1] not in queued
    for cid in queued:
        translate.run_translate(cid)
    TRANSLATE_CALLS.clear()
    translate.backfill()  # всё переведено — повторно ничего не отправляем
    with SessionLocal() as s:
        assert s.get(Card, ids[0]).ru("position") == "RU: Head of Partnerships"


def test_investor_stages(env):
    client, tg = env
    post(client, **msg(1300, photo=[{"file_id": "inv", "file_size": 10}]))
    with SessionLocal() as s:
        card = last_card(s)
    card_msg = tg.sent[-1]
    labels = lambda m: [b[0] for row in (m or []) for b in row]  # noqa: E731
    assert "Pre-Seed" not in labels(card_msg["buttons"])  # не инвестор — кнопок стадий нет

    def cb(data):
        post(client, "callback_query", id="st", data=data,
             **{"from": {"id": 1}, "message": {"message_id": card_msg["id"], "chat": {"id": 1}}})
        with SessionLocal() as s:
            return s.get(Card, card.id)

    cb(f"t:{card.id}:kind:investor")
    assert {"Pre-Seed", "Seed", "Product-Market Fit", "Unicorn on Steroids"} <= set(labels(tg.edits[-1]["buttons"]))
    cb(f"st:{card.id}:seed")
    c = cb(f"st:{card.id}:preseed")
    assert c.stages == ["preseed", "seed"]  # несколько, всегда по порядку
    assert {"✅ Pre-Seed", "✅ Seed", "Product-Market Fit"} <= set(labels(tg.edits[-1]["buttons"]))
    c = cb(f"st:{card.id}:seed")
    assert c.stages == ["preseed"]
    cb(f"st:{card.id}:unicorn")
    # карточка в боте
    post(client, "callback_query", id="c", data=f"c:{card.id}", **{"from": {"id": 1}, "message": {"message_id": 1, "chat": {"id": 1}}})
    assert "💰 Стадии: Pre-Seed → Unicorn on Steroids" in tg.sent[-1]["text"]
    # сайт: чекбоксы, фильтр, CSV
    client.post("/login", data={"username": "anna", "password": "pw"})
    page = client.get(f"/c/{card.id}").text
    assert 'name="stages" value="preseed" checked' in page and 'name="stages" value="seed" checked' not in page
    client.post(f"/c/{card.id}", data={"stages_form": "1", "stages": ["seed", "pmf"], "status": "new"})
    with SessionLocal() as s:
        assert s.get(Card, card.id).stages == ["seed", "pmf"]
        partner = Card(full_name="Партнёр без стадий", kind="partner")
        inv_empty = Card(full_name="Инвестор без стадий", kind="investor")
        s.add_all([partner, inv_empty])
        s.commit()
    page = client.get("/?stage=seed").text
    assert f"#P{card.id}" in page and "Seed → Product-Market Fit" in page and "Партнёр без стадий" not in page
    assert f"#P{card.id}" not in client.get("/?stage=preseed").text
    none_page = client.get("/?stage=none").text
    assert "Инвестор без стадий" in none_page and "Партнёр без стадий" not in none_page
    with SessionLocal() as s:
        c = s.get(Card, card.id)
        c.ball = "us"
        s.commit()
    assert f"#P{card.id}" in client.get("/work?stage=pmf").text
    # сняли все отметки
    client.post(f"/c/{card.id}", data={"stages_form": "1", "status": "new"})
    with SessionLocal() as s:
        assert s.get(Card, card.id).stages is None
    assert "Стадии инвестирования" in client.get("/export.csv").text


def test_sorting(env):
    from app import work
    from app.web import SORTS
    client, _ = env
    t = datetime(2026, 11, 1, 12, 0, tzinfo=work.TZ)
    with SessionLocal() as s:
        a = Card(full_name="SortA старый полный", created_at=t, temp="cold", status="rejected", company="A", position="CEO",
                 phones=["+447700900001"], emails=["a@a.com"], socials=["Telegram: @a"], about="about")
        b = Card(full_name="SortB средний", created_at=t + timedelta(days=5), temp="hot", status="agreed",
                 due=work.today() - timedelta(days=1), ball="us")
        c = Card(full_name="SortC новый", created_at=t + timedelta(days=10), status="new", due=work.today() + timedelta(days=3))
        s.add_all([a, b, c])
        s.flush()
        # у старого A свежая заметка, у C — давняя
        s.add_all([Note(card_id=a.id, text="вчера говорили", created_at=t + timedelta(days=15)),
                   Note(card_id=a.id, text="ещё", created_at=t + timedelta(days=15)),
                   Note(card_id=c.id, text="давно", created_at=t + timedelta(days=10)),
                   Note(card_id=b.id, text="системная", kind="system", created_at=t + timedelta(days=20))])
        s.commit()
    client.post("/login", data={"username": "anna", "password": "pw"})

    def order(sort):
        page = client.get(f"/?q=Sort&sort={sort}").text
        return [n for _, n in sorted((page.index(n), n) for n in ("SortA", "SortB", "SortC"))]

    assert order("new") == ["SortC", "SortB", "SortA"]
    assert order("old") == ["SortA", "SortB", "SortC"]
    assert order("stale") == ["SortB", "SortC", "SortA"]   # B: системная отметка не считается активностью
    assert order("fresh") == ["SortA", "SortC", "SortB"]
    assert order("due") == ["SortB", "SortC", "SortA"]     # просроченный, ближайший, без срока
    assert order("temp") == ["SortB", "SortA", "SortC"]    # 🔥, ❄️, без температуры
    assert order("status") == ["SortB", "SortC", "SortA"]  # договорились, новый, отказ
    assert order("notes")[0] == "SortA"
    assert order("incomplete")[-1] == "SortA" and order("complete")[0] == "SortA"
    assert "▦ 8/8" in client.get("/?q=SortA&sort=complete").text
    assert "нет: компания, должность, телефон" in client.get("/?q=SortB&sort=incomplete").text
    # выбранная сортировка запоминается
    client.get("/?sort=old")
    assert 'value="old" selected' in client.get("/").text
    assert len(SORTS) == 10


def test_multi_select_filters(env):
    client, _ = env
    with SessionLocal() as s:
        s.add_all([
            Card(full_name="MS мужчина горячий PT", gender="male", temp="hot", address="Porto, Portugal", status="new",
                 kind="investor", stages=["seed"], created_by="Jane", ball="us"),
            Card(full_name="MS женщина тёплая GB", gender="female", temp="warm", address="London", status="agreed",
                 kind="investor", stages=["unicorn"], created_by="Surnik", ball="us"),
            Card(full_name="MS неизвестный холодный", temp="cold", status="rejected", kind="partner", created_by="Jane"),
        ])
        s.commit()
    client.post("/login", data={"username": "anna", "password": "pw"})

    def names(url):
        page = client.get(url).text
        return {n for n in ("MS мужчина", "MS женщина", "MS неизвестный") if n in page}

    assert names("/?q=MS&gender=male&gender=female") == {"MS мужчина", "MS женщина"}
    assert names("/?q=MS&gender=male&gender=none") == {"MS мужчина", "MS неизвестный"}
    assert names("/?q=MS&temp=hot&temp=cold") == {"MS мужчина", "MS неизвестный"}
    assert names("/?q=MS&country=PT&country=GB") == {"MS мужчина", "MS женщина"}
    assert names("/?q=MS&stage=seed&stage=unicorn") == {"MS мужчина", "MS женщина"}
    assert names("/?q=MS&status=agreed&status=rejected") == {"MS женщина", "MS неизвестный"}
    assert names("/?q=MS&author=Jane&author=Surnik") == {"MS мужчина", "MS женщина", "MS неизвестный"}
    # между фильтрами — «и»
    assert names("/?q=MS&gender=male&gender=female&temp=warm") == {"MS женщина"}
    assert names("/?q=MS&gender=bogus") == {"MS мужчина", "MS женщина", "MS неизвестный"}  # чужое значение игнорируется
    page = client.get("/?q=MS&gender=male&gender=female").text
    assert "Пол: 👨 Мужчина, 👩 Женщина" in page
    assert 'name="gender" value="male" checked' in page and 'name="gender" value="female" checked' in page
    assert "× сбросить все фильтры" in page
    # сортировка сохраняет все выбранные значения
    assert '<input type="hidden" name="gender" value="male">' in page and '<input type="hidden" name="gender" value="female">' in page
    # страница «Работа»
    work_names = {n for n in ("MS мужчина", "MS женщина") if n in client.get("/work?temp=hot&temp=warm&gender=female").text}
    assert work_names == {"MS женщина"}
