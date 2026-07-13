"""Хранилище (SQLite): пользователи бота и созданные события.

users  — доступ (pending/approved/blocked), Apple ID + шифрованный пароль
         приложения, выбранный календарь, часовой пояс.
events — созданные ботом события: для правок и пинг-напоминаний в Telegram.
"""
import sqlite3
from datetime import datetime

import config

_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.execute("""
CREATE TABLE IF NOT EXISTS events (
    uid        TEXT PRIMARY KEY,
    chat_id    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    start_iso  TEXT NOT NULL,
    reminded   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
""")
_conn.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    tg_name         TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | blocked
    icloud_username TEXT,
    icloud_password TEXT,                              -- зашифровано (security.py)
    calendar_name   TEXT,
    timezone        TEXT,
    created_at      TEXT NOT NULL
)
""")
_conn.commit()

_USER_KEYS = [
    "user_id", "tg_name", "status", "icloud_username", "icloud_password",
    "calendar_name", "timezone", "created_at",
]
_USER_COLS = ", ".join(_USER_KEYS)


# ---------- пользователи ----------

def get_user(user_id: int) -> dict | None:
    row = _conn.execute(
        f"SELECT {_USER_COLS} FROM users WHERE user_id=?", (user_id,)
    ).fetchone()
    return dict(zip(_USER_KEYS, row)) if row else None


def create_user(user_id: int, tg_name: str, status: str = "pending"):
    _conn.execute(
        "INSERT OR IGNORE INTO users (user_id, tg_name, status, created_at) VALUES (?,?,?,?)",
        (user_id, tg_name, status, datetime.now().isoformat()),
    )
    _conn.execute("UPDATE users SET tg_name=? WHERE user_id=?", (tg_name, user_id))
    _conn.commit()


def set_status(user_id: int, status: str):
    _conn.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
    _conn.commit()


def set_credentials(user_id: int, icloud_username: str, icloud_password_enc: str):
    _conn.execute(
        "UPDATE users SET icloud_username=?, icloud_password=?, calendar_name=NULL WHERE user_id=?",
        (icloud_username, icloud_password_enc, user_id),
    )
    _conn.commit()


def set_calendar(user_id: int, calendar_name: str):
    name = (calendar_name or "").replace("\u00a0", " ").strip() or None
    _conn.execute("UPDATE users SET calendar_name=? WHERE user_id=?", (name, user_id))
    _conn.commit()


def set_timezone(user_id: int, tz_name: str):
    _conn.execute("UPDATE users SET timezone=? WHERE user_id=?", (tz_name, user_id))
    _conn.commit()


def delete_user(user_id: int):
    _conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
    _conn.commit()


def list_users() -> list[dict]:
    rows = _conn.execute(
        f"SELECT {_USER_COLS} FROM users ORDER BY created_at"
    ).fetchall()
    return [dict(zip(_USER_KEYS, r)) for r in rows]


def remove_user_events(chat_id: int):
    _conn.execute("DELETE FROM events WHERE chat_id=?", (chat_id,))
    _conn.commit()


# ---------- события ----------

def add(uid: str, chat_id: int, title: str, start: datetime):
    _conn.execute(
        "INSERT OR REPLACE INTO events (uid, chat_id, title, start_iso, reminded, created_at) "
        "VALUES (?,?,?,?,COALESCE((SELECT reminded FROM events WHERE uid=?),0),?)",
        (uid, chat_id, title, start.isoformat(), uid, datetime.now().isoformat()),
    )
    _conn.commit()


def remove(uid: str):
    _conn.execute("DELETE FROM events WHERE uid=?", (uid,))
    _conn.commit()


def update_start(uid: str, title: str, start: datetime):
    _conn.execute(
        "UPDATE events SET title=?, start_iso=?, reminded=0 WHERE uid=?",
        (title, start.isoformat(), uid),
    )
    _conn.commit()


def due_for_reminder(within_seconds: int) -> list[tuple[str, int, str, str]]:
    """События, начинающиеся в ближайшие within_seconds и ещё не напомненные."""
    now = datetime.now().astimezone()
    rows = _conn.execute(
        "SELECT uid, chat_id, title, start_iso FROM events WHERE reminded=0"
    ).fetchall()
    due = []
    for uid, chat_id, title, start_iso in rows:
        try:
            start = datetime.fromisoformat(start_iso)
        except ValueError:
            continue
        if start.tzinfo is None:
            start = start.astimezone()
        delta = (start - now).total_seconds()
        if 0 <= delta <= within_seconds:
            due.append((uid, chat_id, title, start_iso))
    return due


def mark_reminded(uid: str):
    _conn.execute("UPDATE events SET reminded=1 WHERE uid=?", (uid,))
    _conn.commit()
