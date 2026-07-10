"""Лёгкое хранилище созданных ботом событий (SQLite).

Нужно для двух вещей:
  1) находить недавно созданные события для правок;
  2) слать напоминания в Telegram (если включено).

Также запоминаем, в какой календарь записано событие, чтобы правки/удаление
шли сразу в нужный календарь, а не перебирали все.
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
    calendar   TEXT,
    reminded   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
""")
_conn.commit()

# Миграция для старых БД, созданных до появления колонки calendar.
try:
    _conn.execute("ALTER TABLE events ADD COLUMN calendar TEXT")
    _conn.commit()
except sqlite3.OperationalError:
    pass  # колонка уже есть


def add(uid: str, chat_id: int, title: str, start: datetime, calendar: str | None = None):
    _conn.execute(
        "INSERT OR REPLACE INTO events "
        "(uid, chat_id, title, start_iso, calendar, reminded, created_at) "
        "VALUES (?,?,?,?,?,COALESCE((SELECT reminded FROM events WHERE uid=?),0),?)",
        (uid, chat_id, title, start.isoformat(), calendar, uid, datetime.now().isoformat()),
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


def get_calendar(uid: str) -> str | None:
    """Имя календаря, в который записано событие (или None, если неизвестно)."""
    row = _conn.execute("SELECT calendar FROM events WHERE uid=?", (uid,)).fetchone()
    return row[0] if row else None


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
