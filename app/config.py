import hashlib
import os


def _list(value: str | None) -> list[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _db_url(url: str) -> str:
    # Render отдаёт postgres://... — переводим на драйвер psycopg 3
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Settings:
    def __init__(self) -> None:
        self.database_url = _db_url(os.getenv("DATABASE_URL", "sqlite:///./crm.db"))
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.allowed_ids = {int(x) for x in _list(os.getenv("TELEGRAM_ALLOWED_IDS"))}
        self.public_url = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
        self.openai_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_model = os.getenv("OPENAI_MODEL", "gpt-6-luna")
        self.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")
        # WEB_USERS=anna:пароль1,boris:пароль2
        self.web_users = dict(x.split(":", 1) for x in _list(os.getenv("WEB_USERS")) if ":" in x)
        # Telegram допускает в secret_token только [A-Za-z0-9_-]
        self.webhook_secret = hashlib.sha256(("tg:" + self.secret_key).encode()).hexdigest()
        self.max_file_mb = 20
        self.timezone = os.getenv("DISPLAY_TZ", "Asia/Tashkent")
        # Расшифровка голосовых: whisper-1 и gpt-4o-transcribe OpenAI отключает 26.02.2027
        self.transcribe_model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe")
        self.transcribe_fallback = os.getenv("OPENAI_TRANSCRIBE_FALLBACK", "whisper-1")
        # Текстовые заметки длиннее этого получают короткую выжимку
        self.summary_min_chars = int(os.getenv("SUMMARY_MIN_CHARS", "300"))


settings = Settings()
