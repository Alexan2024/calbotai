"""Загрузка конфигурации из окружения / .env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x.strip()]


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USER_IDS = set(_int_list(os.environ.get("ALLOWED_USER_IDS", "")))

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")      # только для транскрипции (Whisper)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")          # альтернативный провайдер STT

# Мозги бота — Claude (Anthropic)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

# Распознавание речи: openai | groq | none
STT_PROVIDER = os.environ.get("STT_PROVIDER", "openai").lower()
STT_MODEL = os.environ.get(
    "STT_MODEL",
    "whisper-large-v3" if STT_PROVIDER == "groq" else "whisper-1",
)

ICLOUD_USERNAME = os.environ["ICLOUD_USERNAME"]
ICLOUD_PASSWORD = os.environ["ICLOUD_PASSWORD"]
ICLOUD_CALENDAR_NAME = os.environ.get("ICLOUD_CALENDAR_NAME", "").strip() or None
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")

TIMEZONE = os.environ.get("TIMEZONE", "Europe/Moscow")
DEFAULT_REMINDERS = _int_list(os.environ.get("DEFAULT_REMINDERS", "60,10"))
TELEGRAM_REMINDERS = os.environ.get("TELEGRAM_REMINDERS", "true").lower() == "true"

DB_PATH = os.environ.get("DB_PATH", "tgcalbot.sqlite3")
