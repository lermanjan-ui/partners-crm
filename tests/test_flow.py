"""Сквозные проверки бота и сайта с фейковыми Telegram и OpenAI (без сети)."""
import io
import os
import tempfile

os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TELEGRAM_BOT_TOKEN"] = "x"
os.environ["TELEGRAM_ALLOWED_IDS"] = "1,2"
os.environ["WEB_USERS"] = "anna:pw"
os.environ["OPENAI_API_KEY"] = "k"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import ai, crm, main, ocr  # noqa: E402
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
        self.edits.append({"id": message_id, "text": text})

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
    ai.summarize_note = lambda text, ctx="": "КРАТКО: " + text[:40]
    ai.summarize_card = lambda info, notes: f"Кто: {info[:30]}\nЗаметок: {len(notes)}"
    ai.extract_contact = fake_contact
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
        assert short.card_id == card.id and short.summary is None and short.ai_status == "none"
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
