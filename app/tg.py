"""Минимальный клиент Telegram Bot API."""
import html

import httpx

API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.http = httpx.Client(timeout=60)

    def call(self, method: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        r = self.http.post(f"{API}/bot{self.token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    @staticmethod
    def _markup(buttons=None, force_reply=False, placeholder=None):
        if force_reply:
            return {"force_reply": True, "input_field_placeholder": placeholder}
        if not buttons:
            return None
        rows = []
        for row in buttons:
            out = []
            for text, value in row:
                if value.startswith("http"):
                    out.append({"text": text, "url": value})
                else:
                    out.append({"text": text, "callback_data": value})
            rows.append(out)
        return {"inline_keyboard": rows}

    def send(self, chat_id, text, reply_to=None, buttons=None, force_reply=False, placeholder=None) -> dict:
        return self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None,
            reply_markup=self._markup(buttons, force_reply, placeholder),
        )

    def edit(self, chat_id, message_id, text, buttons=None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
                      link_preview_options={"is_disabled": True}, reply_markup=self._markup(buttons))
        except TelegramError as e:
            if "not modified" not in str(e):
                raise

    def answer_callback(self, callback_id, text=None) -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass

    def download(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        r = self.http.get(f"{API}/file/bot{self.token}/{info['file_path']}")
        r.raise_for_status()
        return r.content

    def set_webhook(self, url: str, secret: str) -> None:
        self.call("setWebhook", url=url, secret_token=secret,
                  allowed_updates=["message", "edited_message", "callback_query"])
        self.call("setMyCommands", commands=[
            {"command": "find", "description": "Найти контакт: /find имя, компания, телефон"},
            {"command": "last", "description": "Последние визитки"},
            {"command": "new", "description": "Создать контакт без визитки: /new Имя"},
            {"command": "help", "description": "Как пользоваться"},
        ])
