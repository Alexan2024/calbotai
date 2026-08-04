"""Загрузка конфигурации из окружения / .env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _int_list(raw: str) -> list[int]:
    return [int(x) for x in raw.replace(" ", "").split(",") if x.strip()]


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Владелец бота. Он подтверждает заявки друзей и видит панель пользователей.
# Для обратной совместимости: если ADMIN_USER_ID не задан — берётся первый id
# из старой переменной ALLOWED_USER_IDS.
_legacy_allowed = _int_list(os.environ.get("ALLOWED_USER_IDS", ""))
ADMIN_USER_ID = int(os.environ.get("ADMIN_USER_ID", "0") or 0) or (
    _legacy_allowed[0] if _legacy_allowed else 0
)
if not ADMIN_USER_ID:
    raise RuntimeError(
        "Задай ADMIN_USER_ID (свой Telegram id, узнать у @userinfobot)."
    )

# Ключ Fernet для шифрования паролей приложений в SQLite (рекомендуется).
# Сгенерировать: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or None

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")      # только для транскрипции (Whisper)
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")          # альтернативный провайдер STT

# Мозги бота — Claude (Anthropic)
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-5")

# --- Производительность разбора (добавлено перф-спринтом; llm.py это уже читает) ---
# Кэш системного промпта Anthropic: cache_control на статическом блоке.
LLM_CACHE = os.environ.get("LLM_CACHE", "true").lower() == "true"
# Потолок ответа LLM при разборе события(й). Многособытийным афишам нужен запас.
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
# Быстрая модель для мелких задач (parse_when: «перенеси на завтра в 15:00»).
# Пусто -> используется основная LLM_MODEL (безопасный дефолт, ничего не ломает).
LLM_MODEL_FAST = os.environ.get("LLM_MODEL_FAST", "").strip() or LLM_MODEL
# Логировать тайминги и токены каждого вызова LLM в stdout (строки "[perf] ...").
PERF_LOG = os.environ.get("PERF_LOG", "false").lower() == "true"

# Распознавание речи: openai | groq | none
STT_PROVIDER = os.environ.get("STT_PROVIDER", "openai").lower()
STT_MODEL = os.environ.get(
    "STT_MODEL",
    "whisper-large-v3" if STT_PROVIDER == "groq" else "whisper-1",
)

# iCloud-креды владельца — ОПЦИОНАЛЬНЫ: используются один раз для
# автоматической привязки аккаунта админа при первом запуске (bootstrap).
ICLOUD_USERNAME = os.environ.get("ICLOUD_USERNAME") or None
ICLOUD_PASSWORD = os.environ.get("ICLOUD_PASSWORD") or None
ICLOUD_CALENDAR_NAME = os.environ.get("ICLOUD_CALENDAR_NAME", "").strip() or None

# --- CalDAV-провайдеры ---
# Оба провайдера работают по одному протоколу (CalDAV + пароль приложения),
# различается только базовый URL. Пользователь выбирает провайдера в визарде
# подключения; выбор хранится в users.provider ("icloud" | "google").
CALDAV_URL = os.environ.get("CALDAV_URL", "https://caldav.icloud.com/")  # iCloud; имя оставлено для совместимости
GOOGLE_CALDAV_URL = os.environ.get(
    "GOOGLE_CALDAV_URL", "https://apidata.googleusercontent.com/caldav/v2/"
)
CALDAV_URLS = {
    "icloud": CALDAV_URL,
    "google": GOOGLE_CALDAV_URL,
}
DEFAULT_PROVIDER = "icloud"

TIMEZONE = os.environ.get("TIMEZONE", "Europe/Moscow")  # пояс по умолчанию
DEFAULT_REMINDERS = _int_list(os.environ.get("DEFAULT_REMINDERS", "60,10"))
TELEGRAM_REMINDERS = os.environ.get("TELEGRAM_REMINDERS", "true").lower() == "true"

# Утренний дайджест: час отправки по локальному поясу пользователя.
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "8"))

# Рабочее окно для поиска свободных слотов («найди час на этой неделе»).
WORKDAY_START = int(os.environ.get("WORKDAY_START", "9"))
WORKDAY_END = int(os.environ.get("WORKDAY_END", "21"))

DB_PATH = os.environ.get("DB_PATH", "tgcalbot.sqlite3")
