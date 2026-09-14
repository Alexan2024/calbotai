"""Telegram-бот: события в любом формате -> Apple / Google Calendar (CalDAV).

Мультипользовательский: новые пользователи запрашивают доступ, владелец
подтверждает кнопкой, дальше каждый подключает СВОЙ аккаунт — Apple iCloud
или Google Calendar (пароль приложения). Оба провайдера работают по одному
протоколу CalDAV, различается только базовый URL. Креды шифруются
(security.py).

UX кнопочный: постоянная reply-клавиатура, инлайн-навигация по дням,
тапабельные события с действиями. Правки идут по UID (+ имя календаря);
текстовый разбор («перенеси врача») — запасной путь.

Новое: выбор провайдера (Apple / Google) в визарде подключения; выбор
календаря прямо в карточке создания; чтение (день/неделя/месяц/напоминания/
дайджест/статистика) идёт по ВСЕМ календарям; повторяющиеся события (RRULE);
утренний дайджест; кнопки на пинге напоминания (+15 мин / карточка);
детектор пересечений; поиск свободного слота; undo после создания;
массовые операции («отмени всё в пятницу»); статистика за неделю.

Визуальный слой (render.py): день рисуется вертикальным таймлайном, неделя —
hour-heatmap, поиск слота — полосой занятости, статистика — бар-чартом,
накладки — мини-диаграммой, дайджест — «рельсой». Всё моноширинное — в <pre>.
"""
from __future__ import annotations
import asyncio
import io
import re
import uuid
from datetime import datetime, timedelta, date

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery, BotCommand,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

import config
import llm
import transcribe
import calendar_client as cal
import store
import notifier
import security
import render
from models import Event

DEFAULT_TZ = pytz.timezone(config.TIMEZONE)
dp = Dispatcher()

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# category -> эмодзи. Значок выбирает бот по category от LLM, не сама модель,
# чтобы одинаковые события всегда получали один и тот же символ.
CATEGORY_EMOJI = {
    "health": "🩺", "sport": "💪", "call_online": "🎧", "meeting": "🤝",
    "call": "📞", "deadline": "⏰", "birthday": "🎂", "travel": "✈️",
    "food": "🍽", "event": "🎫", "study": "📚", "service": "💇", "other": "📌",
}
CATEGORY_LABEL = {
    "health": "здоровье", "sport": "спорт", "call_online": "созвоны",
    "meeting": "встречи", "call": "звонки", "deadline": "дедлайны",
    "birthday": "дни рождения", "travel": "поездки", "food": "еда",
    "event": "мероприятия", "study": "учёба", "service": "услуги",
    "other": "прочее",
}
EMOJI_TO_CATEGORY = {v: k for k, v in CATEGORY_EMOJI.items()}

REMINDER_PRESETS = [10, 60, 1440]  # 10 мин, 1 час, 1 день

STATUS_ICON = {"approved": "✅", "pending": "🕓", "blocked": "⛔"}

PROVIDER_LABEL = {"icloud": "🍏 Apple (iCloud)", "google": "🟦 Google"}

# Тексты шагов визарда подключения — по провайдерам.
SETUP_STEP1 = {
    "icloud": (
        "🍏 <b>Apple Calendar</b> — шаг 1 из 2\n\n"
        "Пришли свой Apple ID (email).\n\n"
        "На шаге 2 понадобится <b>пароль приложения</b> (не обычный пароль!):\n"
        "appleid.apple.com → Вход и безопасность → Пароли приложений.\n"
        "Двухфакторная аутентификация должна быть включена.\n\nОтмена — /cancel"
    ),
    "google": (
        "🟦 <b>Google Calendar</b> — шаг 1 из 2\n\n"
        "Пришли свой Google-аккаунт (Gmail-адрес целиком).\n\n"
        "На шаге 2 понадобится <b>пароль приложения</b> (не обычный пароль!):\n"
        "myaccount.google.com/apppasswords — создай пароль с любым названием.\n"
        "Двухэтапная аутентификация должна быть включена, иначе страница "
        "паролей приложений недоступна.\n\nОтмена — /cancel"
    ),
}
SETUP_STEP2 = {
    "icloud": (
        "Шаг 2 из 2 — пришли <b>пароль приложения</b> "
        "(вид <code>abcd-efgh-ijkl-mnop</code>).\n\n"
        "🔒 Сообщение с паролем я удалю сразу после проверки."
    ),
    "google": (
        "Шаг 2 из 2 — пришли <b>пароль приложения</b> "
        "(16 символов, вид <code>abcd efgh ijkl mnop</code> — "
        "пробелы можно не убирать, я уберу сам).\n\n"
        "🔒 Сообщение с паролем я удалю сразу после проверки."
    ),
}

RRULE_FREQ_LABEL = {
    "YEARLY": "ежегодно", "MONTHLY": "ежемесячно",
    "WEEKLY": "еженедельно", "DAILY": "ежедневно",
}

# --- состояние в памяти ---
# подтверждения создания/правки/bulk: token -> {"action","event",...}
PENDING: dict[str, dict] = {}
# тапнутые события календаря: token -> {"uid","offset","cal"}
EVENTS: dict[str, dict] = {}
# рабочий набор напоминаний в меню существующего события: token -> set(minutes)
REMWORK: dict[str, set] = {}
# ожидание текстового ввода: user_id -> {"mode",...}
AWAITING: dict[int, dict] = {}
# визард подключения / выбор календаря в настройках: user_id -> список имён
SETUP: dict[int, list[str]] = {}
# выбор календаря в карточке создания: token -> список имён
CALPICK: dict[str, list[str]] = {}
# найденные свободные слоты: token -> {"title","dur","slots":[iso]}
SLOTS: dict[str, dict] = {}
# undo после создания: token -> {"uid","cal"}
UNDO: dict[str, dict] = {}


# ---------- вспомогательное ----------

def is_admin(user_id: int) -> bool:
    return user_id == config.ADMIN_USER_ID


def user_tz(user_id: int):
    u = store.get_user(user_id)
    name = (u or {}).get("timezone") or config.TIMEZONE
    try:
        return pytz.timezone(name)
    except Exception:
        return DEFAULT_TZ


def user_tz_name(u: dict | None) -> str:
    return (u or {}).get("timezone") or config.TIMEZONE


def now(tz=None) -> datetime:
    return datetime.now(tz or DEFAULT_TZ)


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def tg_display_name(from_user) -> str:
    return f"@{from_user.username}" if from_user.username else (from_user.full_name or str(from_user.id))


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🗓 Неделя")],
            [KeyboardButton(text="📆 Месяц"), KeyboardButton(text="🔔 Напоминания")],
            [KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def fmt_dt(dt, all_day: bool = False, tz=None) -> str:
    tz = tz or DEFAULT_TZ
    if isinstance(dt, datetime) and dt.tzinfo:
        dt = dt.astimezone(tz)
    if all_day or not isinstance(dt, datetime):
        d = dt.date() if isinstance(dt, datetime) else dt
        return f"{d.strftime('%d.%m')} ({DAYS[d.weekday()]}), весь день"
    return f"{dt.strftime('%d.%m')} ({DAYS[dt.weekday()]}) {dt.strftime('%H:%M')}"


def rem_label(m: int) -> str:
    if m % 1440 == 0:
        return f"{m // 1440} дн"
    if m % 60 == 0:
        return f"{m // 60} ч"
    return f"{m} мин"


def rec_label(rrule: str | None) -> str | None:
    if not rrule:
        return None
    up = rrule.upper()
    for freq, label in RRULE_FREQ_LABEL.items():
        if f"FREQ={freq}" in up:
            m = re.search(r"INTERVAL=(\d+)", up)
            if m and int(m.group(1)) > 1:
                return f"{label}, интервал {m.group(1)}"
            return label
    return "повторяется"


def event_card(ev: Event, tz=None, cal_name: str | None = None) -> str:
    tz = tz or DEFAULT_TZ
    # эмодзи уже внутри ev.title (ставится при разборе), поэтому без ведущего 📌
    lines = [f"<b>{ev.title}</b>", f"🕒 {fmt_dt(ev.start, ev.all_day, tz)}"]
    if ev.end and not ev.all_day:
        lines[-1] += f" – {ev.end.astimezone(tz).strftime('%H:%M')}"
    r = rec_label(ev.recurrence)
    if r:
        lines.append(f"🔁 {r}")
    if ev.location:
        lines.append(f"📍 {ev.location}")
    if ev.notes:
        lines.append(f"📝 {ev.notes}")
    if ev.reminders_minutes:
        rem = ", ".join(rem_label(m) for m in ev.reminders_minutes)
        lines.append(f"🔔 {rem}")
    if cal_name:
        lines.append(f"📁 {cal_name}")
    return "\n".join(lines)


def event_from_llm(d: dict, tz) -> Event:
    start = datetime.fromisoformat(d["start"])
    if start.tzinfo is None:
        start = tz.localize(start)
    end = None
    if d.get("end"):
        end = datetime.fromisoformat(d["end"])
        if end.tzinfo is None:
            end = tz.localize(end)
    reminders = d.get("reminders_minutes") or list(config.DEFAULT_REMINDERS)
    category = d.get("category") or "other"
    emoji = CATEGORY_EMOJI.get(category, "📌")
    raw_title = (d.get("title") or "Событие").strip()
    recurrence = (d.get("recurrence") or "").strip() or None
    # дни рождения повторяются ежегодно, даже если пользователь не сказал
    if category == "birthday" and not recurrence:
        recurrence = "FREQ=YEARLY"
    return Event(
        title=f"{emoji} {raw_title}",
        start=start,
        end=end,
        all_day=bool(d.get("all_day")),
        location=d.get("location"),
        notes=d.get("notes"),
        reminders_minutes=reminders,
        recurrence=recurrence,
    )


def event_from_caldav(d: dict, tz) -> Event:
    """Собрать Event из полного словаря get_event (для правок без потери данных)."""
    return Event(
        title=d["title"],
        start=_to_dt(d["start"], tz),
        end=_to_dt(d["end"], tz) if d.get("end") else None,
        all_day=d.get("all_day", False),
        location=d.get("location"),
        notes=d.get("notes"),
        reminders_minutes=list(d.get("reminders_minutes") or []),
        recurrence=d.get("recurrence"),
        uid=d["uid"],
    )


def _to_dt(v, tz) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else tz.localize(v)
    return tz.localize(datetime.combine(v, datetime.min.time()))  # date


def _iso(s: str | None, tz) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return tz.localize(dt) if dt.tzinfo is None else dt
    except ValueError:
        return None


def _norm_title(t: str) -> str:
    return re.sub(r"^\W+", "", t or "").lower().strip()


# ---------- доступ ----------

def request_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Запросить доступ", callback_data="reg"),
    ]])


def setup_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔗 Подключить календарь", callback_data="setup"),
    ]])


def provider_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍏 Apple (iCloud)", callback_data="setpp:icloud")],
        [InlineKeyboardButton(text="🟦 Google Calendar", callback_data="setpp:google")],
    ])


async def ensure_ready(message: Message) -> dict | None:
    """Пользователь одобрен и с подключённым календарём — иначе подсказать шаг и вернуть None."""
    uid = message.from_user.id
    u = store.get_user(uid)
    if u is None and is_admin(uid):
        store.create_user(uid, tg_display_name(message.from_user), status="approved")
        u = store.get_user(uid)
    if u is None:
        await message.answer(
            "Привет! Это персональный календарный бот — доступ выдаёт владелец.\n"
            "Нажми кнопку, я отправлю ему запрос.",
            reply_markup=request_kb(),
        )
        return None
    if u["status"] == "blocked":
        return None  # молча игнорируем
    if u["status"] == "pending":
        await message.answer("Запрос на доступ уже отправлен — жди подтверждения 🙂")
        return None
    if not u["icloud_username"]:
        await message.answer(
            "Доступ открыт! Осталось подключить календарь — Apple или Google.",
            reply_markup=setup_kb(),
        )
        return None
    return u


def client_or_none(user_id: int):
    try:
        return cal.for_user(user_id)
    except RuntimeError:
        return None


# ---------- клавиатуры подтверждения / напоминаний ----------

def reminder_toggle_row(active: set, prefix: str, token: str) -> list[InlineKeyboardButton]:
    row = []
    for m in REMINDER_PRESETS:
        mark = "☑️" if m in active else "⬜️"
        row.append(InlineKeyboardButton(
            text=f"{mark} {rem_label(m)}",
            callback_data=f"{prefix}:{token}:{m}",
        ))
    return row


def create_confirm_kb(token: str, active: set, cal_name: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        reminder_toggle_row(active, "crt", token),
        [InlineKeyboardButton(text="✏️ Своё напоминание", callback_data=f"crc:{token}")],
        [InlineKeyboardButton(
            text=f"📁 {cal_name}"[:60] if cal_name else "📁 Календарь по умолчанию",
            callback_data=f"crl:{token}",
        )],
        [
            InlineKeyboardButton(text="✅ Добавить", callback_data=f"ok:{token}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{token}"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_confirm_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data=f"ok:{token}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{token}"),
    ]])


async def _render_create_card(msg_target, token: str, tz, edit: bool = True):
    """Перерисовать карточку подтверждения создания (после тоглов/выбора календаря)."""
    data = PENDING.get(token)
    if not data:
        return
    ev = Event.from_dict(data["event"])
    text = (event_card(ev, tz, data.get("calendar"))
            + data.get("warn", "") + "\n\nДобавить в календарь?")
    kb = create_confirm_kb(token, set(ev.reminders_minutes), data.get("calendar"))
    if edit:
        await msg_target.edit_text(text, reply_markup=kb)
    else:
        await msg_target.answer(text, reply_markup=kb)


# ---------- проверка дублей и пересечений ----------

async def _conflict_warning(user_id: int, ev: Event, tz) -> str:
    """Дубль (то же название рядом) или пересечение по времени — по всем календарям."""
    client = client_or_none(user_id)
    if client is None:
        return ""
    ev_end = ev.end or ev.start + timedelta(hours=1)
    try:
        lo = ev.start - timedelta(hours=1)
        hi = ev_end + timedelta(hours=1)
        nearby = await asyncio.to_thread(client.list_events, lo, hi)
    except Exception:
        return ""
    mine = _norm_title(ev.title)
    warns = []
    diagram = None
    for e in nearby:
        if mine and _norm_title(e["title"]) == mine:
            warns.append(f"⚠️ Похожее событие уже есть: {fmt_dt(e['start'], e['all_day'], tz)}")
            continue
        if ev.all_day or e["all_day"] or not isinstance(e["start"], datetime):
            continue
        e_start = _to_dt(e["start"], tz)
        e_end = _to_dt(e["end"], tz) if e.get("end") else e_start + timedelta(hours=1)
        if ev.start < e_end and e_start < ev_end:
            span = f"{e_start.astimezone(tz).strftime('%H:%M')}–{e_end.astimezone(tz).strftime('%H:%M')}"
            warns.append(f"⚠️ Пересекается с «{e['title']}» ({span})")
            if diagram is None:  # диаграмма только для первой накладки по времени
                diagram = render.conflict_diagram(
                    ev.title, ev.start, ev_end, e["title"], e_start, e_end, tz)
    if not warns:
        return ""
    out = "\n\n" + "\n".join(warns[:3])
    if diagram:
        out += "\n" + diagram
    return out


async def _attach_conflict_warning(sent_msg, token: str, user_id: int, ev: Event, tz):
    """Посчитать дубли/пересечения в фоне и, если они есть, дорисовать карточку.

    Показ карточки этим не блокируется (раньше проверка добавляла +1–3 c к ожиданию).
    Перерисовка идёт через _render_create_card, поэтому подхватывает тоглы напоминаний
    и выбранный календарь, если пользователь успел их поменять."""
    try:
        warn = await _conflict_warning(user_id, ev, tz)
    except Exception:
        return
    if not warn:
        return
    data = PENDING.get(token)
    if not data:  # пользователь уже подтвердил/отменил — карточки больше нет
        return
    data["warn"] = warn
    try:
        await _render_create_card(sent_msg, token, tz, edit=True)
    except Exception:
        pass  # сообщение изменено/удалено параллельно — не критично


# ---------- разбор результата LLM ----------

async def handle_parsed(message: Message, parsed: dict, u: dict):
    intent = parsed.get("intent", "chitchat")
    tz = user_tz(u["user_id"])

    if intent == "create":
        events = parsed.get("events") or []
        if not events:
            await message.answer("Не увидел события. Уточни дату/время?")
            return
        for ed in events:
            ev = event_from_llm(ed, tz)
            token = new_token()
            PENDING[token] = {"action": "create", "event": ev.to_dict(),
                              "user_id": u["user_id"], "chat_id": message.chat.id,
                              "calendar": None, "warn": ""}
            sent = await message.answer(
                event_card(ev, tz) + "\n\nДобавить в календарь?",
                reply_markup=create_confirm_kb(token, set(ev.reminders_minutes)),
            )
            # дубли/пересечения считаем в фоне и дорисовываем карточку — не ждём CalDAV
            asyncio.create_task(
                _attach_conflict_warning(sent, token, u["user_id"], ev, tz)
            )

    elif intent == "query":
        q = parsed.get("query") or {}
        dt_from = _iso(q.get("from"), tz) or now(tz)
        dt_to = _iso(q.get("to"), tz) or (dt_from + timedelta(days=1))
        await send_schedule(message, dt_from, dt_to, u["user_id"])

    elif intent == "edit":
        await handle_edit(message, parsed.get("edit") or {}, u)

    elif intent == "find_slot":
        await handle_find_slot(message, parsed.get("slot") or {}, u)

    elif intent == "bulk":
        await handle_bulk(message, parsed.get("bulk") or {}, u)

    else:
        await message.answer(parsed.get("reply") or "Пришли событие текстом, голосом или афишей 🙂")


async def send_schedule(message: Message, dt_from: datetime, dt_to: datetime, user_id: int,
                        heatmap: bool = False):
    tz = user_tz(user_id)
    try:
        client = cal.for_user(user_id)
        events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not events:
        await message.answer("На этот период событий нет 🎉")
        return
    header = f"🗓 {dt_from.strftime('%d.%m')} – {dt_to.strftime('%d.%m')}\n"
    lines = []
    cur_day = None
    for e in events:
        d = e["start"].date() if isinstance(e["start"], datetime) else e["start"]
        if d != cur_day:
            cur_day = d
            lines.append(f"\n<b>{d.strftime('%d.%m')} ({DAYS[d.weekday()]})</b>")
        lines.append(_schedule_line(e, tz))
    body = header + "\n".join(lines)
    if heatmap:
        hm = render.week_heatmap(events, tz, dt_from.date(),
                                 config.WORKDAY_START, config.WORKDAY_END)
        body = hm + "\n" + body
    await message.answer(body)


def _schedule_line(e: dict, tz) -> str:
    when = fmt_dt(e["start"], e["all_day"], tz)
    # в дневной/недельной сводке дата уже в заголовке — оставим только время
    time_part = when.split(") ")[-1] if isinstance(e["start"], datetime) and not e["all_day"] else "весь день"
    line = f"• {time_part} — {e['title']}"
    if e.get("location"):
        line += f" ({e['location']})"
    return line


# --- текстовая правка (запасной путь для «перенеси врача») ---

async def handle_edit(message: Message, edit: dict, u: dict):
    tz = user_tz(u["user_id"])
    title = (edit.get("match_title") or "").strip()
    changes = edit.get("changes") or {}
    if not title:
        await message.answer("Какое событие изменить? Напиши название или тапни его в списке (📅 Сегодня).")
        return
    try:
        client = cal.for_user(u["user_id"])
        matches = await asyncio.to_thread(
            client.find_by_title, title, now(tz) - timedelta(days=30), now(tz) + timedelta(days=60)
        )
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not matches:
        await message.answer(f"Не нашёл событие «{title}» в календаре.")
        return
    if len(matches) > 1:
        opts = "\n".join(f"• {fmt_dt(m['start'], m['all_day'], tz)} — {m['title']}" for m in matches[:8])
        await message.answer(f"Нашёл несколько «{title}». Уточни или тапни в списке:\n{opts}")
        return

    target = matches[0]
    base = await asyncio.to_thread(client.get_event, target["uid"], target.get("calendar")) or target
    new = event_from_caldav(base, tz)
    if changes.get("title"):
        new.title = changes["title"]
    if _iso(changes.get("start"), tz):
        new.start = _iso(changes["start"], tz)
    if _iso(changes.get("end"), tz):
        new.end = _iso(changes["end"], tz)
    if "location" in changes:
        new.location = changes["location"]
    if "notes" in changes:
        new.notes = changes["notes"]
    if changes.get("reminders_minutes"):
        new.reminders_minutes = changes["reminders_minutes"]

    token = new_token()
    PENDING[token] = {"action": "edit", "event": new.to_dict(),
                      "uid": target["uid"], "cal": target.get("calendar"),
                      "user_id": u["user_id"], "chat_id": message.chat.id}
    await message.answer(
        "Изменить на:\n\n" + event_card(new, tz) + "\n\nПрименить?",
        reply_markup=edit_confirm_kb(token),
    )


# ---------- поиск свободного слота ----------

def _ceil_half_hour(dt: datetime) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    add = (30 - dt.minute % 30) % 30
    return dt + timedelta(minutes=add)


def _compute_free_slots(events: list[dict], dt_from: datetime, dt_to: datetime,
                        duration_min: int, tz, limit: int = 6) -> list[datetime]:
    """Свободные окна в рабочие часы (WORKDAY_START..WORKDAY_END) по всем календарям."""
    dur = timedelta(minutes=duration_min)
    busy = []
    for e in events:
        if e["all_day"] or not isinstance(e["start"], datetime):
            continue
        s = _to_dt(e["start"], tz)
        en = _to_dt(e["end"], tz) if e.get("end") else s + timedelta(hours=1)
        busy.append((s, en))
    busy.sort()
    merged: list[tuple[datetime, datetime]] = []
    for s, en in busy:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((s, en))

    slots: list[datetime] = []
    d0 = dt_from.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    n_days = max(1, (dt_to.astimezone(tz) - d0).days + 1)
    for i in range(n_days):
        day = d0 + timedelta(days=i)
        ws = day + timedelta(hours=config.WORKDAY_START)
        we = day + timedelta(hours=config.WORKDAY_END)
        cursor = _ceil_half_hour(max(ws, dt_from))
        if cursor >= we:
            continue
        for s, en in merged:
            if en <= cursor or s >= we:
                continue
            if s - cursor >= dur:
                slots.append(cursor)
                if len(slots) >= limit:
                    return slots
            cursor = _ceil_half_hour(max(cursor, en))
        if we - cursor >= dur:
            slots.append(cursor)
            if len(slots) >= limit:
                return slots
    return slots


async def handle_find_slot(message: Message, slot: dict, u: dict):
    tz = user_tz(u["user_id"])
    dur = int(slot.get("duration_minutes") or 60)
    dt_from = _iso(slot.get("from"), tz) or now(tz)
    dt_to = _iso(slot.get("to"), tz) or (dt_from + timedelta(days=7))
    title = (slot.get("title") or "Встреча").strip()
    try:
        client = cal.for_user(u["user_id"])
        events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    free = _compute_free_slots(events, dt_from, dt_to, dur, tz)
    if not free:
        await message.answer(
            f"Свободных окон на {rem_label(dur)} в этом периоде не нашёл 😕 "
            "Попробуй расширить период."
        )
        return
    token = new_token()
    SLOTS[token] = {"title": title, "dur": dur,
                    "slots": [s.isoformat() for s in free],
                    "user_id": u["user_id"], "chat_id": message.chat.id}
    rows = []
    for i, s in enumerate(free):
        rows.append([InlineKeyboardButton(
            text=f"{s.strftime('%d.%m')} ({DAYS[s.weekday()]}) {s.strftime('%H:%M')}",
            callback_data=f"slp:{token}:{i}",
        )])
    occ = render.slot_occupancy(
        events, free[0].replace(hour=0, minute=0, second=0, microsecond=0), tz,
        config.WORKDAY_START, config.WORKDAY_END,
        [s for s in free if s.date() == free[0].date()],
    )
    await message.answer(
        occ + "\n\n"
        + f"🔍 Свободные окна на <b>{rem_label(dur)}</b> "
        f"({dt_from.strftime('%d.%m')}–{dt_to.strftime('%d.%m')}).\n"
        f"Тапни, чтобы создать «{title}»:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data.startswith("slp:"))
async def cb_slot_pick(cq: CallbackQuery):
    _, token, idx = cq.data.split(":")
    data = SLOTS.get(token)
    if not data or int(idx) >= len(data["slots"]):
        await cq.answer("Слоты устарели — попроси найти заново", show_alert=True)
        return
    tz = user_tz(cq.from_user.id)
    start = datetime.fromisoformat(data["slots"][int(idx)])
    ev = Event(
        title=f"🤝 {data['title']}",
        start=start,
        end=start + timedelta(minutes=data["dur"]),
        reminders_minutes=list(config.DEFAULT_REMINDERS),
    )
    tok = new_token()
    PENDING[tok] = {"action": "create", "event": ev.to_dict(),
                    "user_id": data["user_id"], "chat_id": data["chat_id"],
                    "calendar": None, "warn": ""}
    await cq.message.edit_text(
        event_card(ev, tz) + "\n\nДобавить в календарь?",
        reply_markup=create_confirm_kb(tok, set(ev.reminders_minutes)),
    )
    await cq.answer()


# ---------- массовые операции ----------

async def handle_bulk(message: Message, bulk: dict, u: dict):
    tz = user_tz(u["user_id"])
    op = bulk.get("op")
    dt_from = _iso(bulk.get("from"), tz)
    dt_to = _iso(bulk.get("to"), tz)
    shift = int(bulk.get("shift_minutes") or 0)
    if op not in ("delete", "shift") or not dt_from or not dt_to or (op == "shift" and not shift):
        await message.answer("Не понял массовую операцию. Пример: «отмени всё в пятницу» "
                             "или «перенеси все созвоны завтра на час позже».")
        return
    try:
        client = cal.for_user(u["user_id"])
        events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    needle = (bulk.get("match_title") or "").lower().strip()
    if needle:
        events = [e for e in events if needle in e["title"].lower()]
    if not events:
        await message.answer("Под эти условия ничего не попало.")
        return
    events = events[:30]
    items = [{"uid": e["uid"], "cal": e.get("calendar")} for e in events]
    token = new_token()
    PENDING[token] = {"action": "bulk", "op": op, "shift": shift, "items": items,
                      "user_id": u["user_id"], "chat_id": message.chat.id}
    verb = "🗑 Удалить" if op == "delete" else f"🕒 Сдвинуть на {rem_label(abs(shift))}" + (" назад" if shift < 0 else "")
    lines = [f"• {fmt_dt(e['start'], e['all_day'], tz)} — {e['title']}" for e in events[:10]]
    more = f"\n…и ещё {len(events) - 10}" if len(events) > 10 else ""
    await message.answer(
        f"{verb} — событий: <b>{len(events)}</b>\n\n" + "\n".join(lines) + more + "\n\nПрименить?",
        reply_markup=edit_confirm_kb(token),
    )


async def _apply_bulk(cq: CallbackQuery, data: dict):
    user_id = data["user_id"]
    tz = user_tz(user_id)
    client = cal.for_user(user_id)
    op, shift = data["op"], data.get("shift", 0)
    done, failed = 0, 0
    for it in data["items"]:
        try:
            if op == "delete":
                ok = await asyncio.to_thread(client.delete_event, it["uid"], it.get("cal"))
                if ok:
                    store.remove(it["uid"])
            else:  # shift
                ev_dict = await asyncio.to_thread(client.get_event, it["uid"], it.get("cal"))
                if not ev_dict:
                    failed += 1
                    continue
                ev = event_from_caldav(ev_dict, tz)
                delta = timedelta(minutes=shift)
                ev.start = _to_dt(ev.start, tz) + delta
                if ev.end:
                    ev.end = _to_dt(ev.end, tz) + delta
                ok = await asyncio.to_thread(client.update_event, it["uid"], ev, it.get("cal"))
                if ok:
                    store.update_start(it["uid"], ev.title, ev.start)
            done += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception:
            failed += 1
    verb = "удалено" if op == "delete" else "перенесено"
    text = f"✅ Готово: {verb} {done}"
    if failed:
        text += f", не получилось: {failed}"
    await cq.message.edit_text(text)


# ---------- дневная навигация ----------

def day_bounds(offset: int, tz) -> tuple[datetime, datetime]:
    start = now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
    return start, start + timedelta(days=1)


def day_title(offset: int, tz) -> str:
    start, _ = day_bounds(offset, tz)
    label = f"{start.strftime('%d.%m')} ({DAYS[start.weekday()]})"
    if offset == 0:
        return f"Сегодня, {label}"
    if offset == 1:
        return f"Завтра, {label}"
    if offset == -1:
        return f"Вчера, {label}"
    return label


async def render_day(offset: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    tz = user_tz(user_id)
    dt_from, dt_to = day_bounds(offset, tz)
    try:
        client = cal.for_user(user_id)
        events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    except Exception as e:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️", callback_data=f"day:{offset - 1}"),
            InlineKeyboardButton(text="▶️", callback_data=f"day:{offset + 1}"),
        ]])
        return f"⚠️ Не смог прочитать календарь: {e}", kb

    nav = [
        InlineKeyboardButton(text="◀️", callback_data=f"day:{offset - 1}"),
        InlineKeyboardButton(text="📅 Сегодня", callback_data="day:0"),
        InlineKeyboardButton(text="▶️", callback_data=f"day:{offset + 1}"),
    ]
    rows = [nav]

    # текст — вертикальный таймлайн (render.py); кнопки-события оставляем тапабельными
    for e in events:
        token = new_token()
        EVENTS[token] = {"uid": e["uid"], "offset": offset, "cal": e.get("calendar")}
        when = (e["start"].astimezone(tz).strftime("%H:%M")
                if isinstance(e["start"], datetime) and not e["all_day"] else "весь день")
        rows.append([InlineKeyboardButton(
            text=f"{when} · {e['title']}"[:60],
            callback_data=f"ev:{token}",
        )])
    text = render.day_timeline(events, tz, day_title(offset, tz))
    if events:
        text += "\nТапни событие ниже, чтобы изменить."

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_month(message: Message, user_id: int):
    tz = user_tz(user_id)
    dt_from, _ = day_bounds(0, tz)
    dt_to = dt_from + timedelta(days=30)
    try:
        client = cal.for_user(user_id)
        events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not events:
        await message.answer("На ближайший месяц событий нет 🎉")
        return
    lines = ["📆 <b>Ближайшие 30 дней</b>"]
    cur_day = None
    for e in events:
        d = e["start"].date() if isinstance(e["start"], datetime) else e["start"]
        if d != cur_day:
            cur_day = d
            lines.append(f"\n<b>{d.strftime('%d.%m')} ({DAYS[d.weekday()]})</b>")
        lines.append(_schedule_line(e, tz))
    await message.answer("\n".join(lines))


# ---------- статистика за неделю ----------

async def build_stats(user_id: int) -> str:
    tz = user_tz(user_id)
    dt_to = now(tz)
    dt_from = dt_to - timedelta(days=7)
    client = cal.for_user(user_id)
    events = await asyncio.to_thread(client.list_events, dt_from, dt_to)
    if not events:
        return "📊 За последние 7 дней событий не было."
    agg: dict[str, list[float]] = {}  # category -> [count, hours]
    for e in events:
        first = (e["title"] or "").split(" ")[0]
        category = EMOJI_TO_CATEGORY.get(first, "other")
        hours = 0.0
        if isinstance(e["start"], datetime) and not e["all_day"]:
            s = _to_dt(e["start"], tz)
            en = _to_dt(e["end"], tz) if e.get("end") else s + timedelta(hours=1)
            hours = max(0.0, (en - s).total_seconds() / 3600)
        item = agg.setdefault(category, [0, 0.0])
        item[0] += 1
        item[1] += hours
    return render.stat_bars(agg, CATEGORY_LABEL, len(events), CATEGORY_EMOJI)


# ---------- карточка события и действия ----------

def event_actions_kb(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🕒 Перенести", callback_data=f"rsc:{token}"),
            InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"rnm:{token}"),
        ],
        [
            InlineKeyboardButton(text="🔔 Напоминание", callback_data=f"rem:{token}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{token}"),
        ],
        [InlineKeyboardButton(text="◀️ К дню", callback_data=f"back:{token}")],
    ])


async def show_event_card(cq: CallbackQuery, token: str):
    data = EVENTS.get(token)
    if not data:
        await cq.message.edit_text("Событие устарело — открой список заново (📅 Сегодня).")
        return
    tz = user_tz(cq.from_user.id)
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.message.edit_text("Календарь не подключён — открой ⚙️ Настройки.")
        return
    ev_dict = await asyncio.to_thread(client.get_event, data["uid"], data.get("cal"))
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено (возможно, удалено).")
        return
    data["cal"] = ev_dict.get("calendar") or data.get("cal")
    ev = event_from_caldav(ev_dict, tz)
    await cq.message.edit_text(
        event_card(ev, tz, ev_dict.get("calendar")),
        reply_markup=event_actions_kb(token),
    )


# ---------- хендлеры: команды и кнопки ----------

START_TEXT = (
    "Привет! Я добавляю события в твой календарь — Apple (iCloud) или Google.\n\n"
    "<b>Что я умею:</b>\n"
    "📝 События в любом виде — текст, голосовое, фото афиши, пересланное сообщение\n"
    "🔁 Повторяющиеся события — «йога каждый вторник в 19:00»\n"
    "🧠 Вопросы о планах — «что у меня завтра?»\n"
    "🔍 Поиск свободного времени — «найди час на этой неделе для встречи»\n"
    "🧹 Массовые операции — «отмени всё в пятницу», «сдвинь созвоны на час»\n"
    "✏️ Правки кнопками (тапни событие в списке) или текстом — «перенеси врача на 18:00»\n"
    "⚠️ Предупреждаю о дублях и пересечениях в расписании\n"
    "📁 Выбор календаря прямо в карточке события\n"
    "⏰ Напоминания: нативные + пинг в чат с кнопкой «+15 мин»\n"
    "🌅 Утренний дайджест дня (вкл/выкл в настройках)\n"
    "📊 Статистика недели — в ⚙️ Настройки\n\n"
    "Кнопки снизу — расписание, напоминания и настройки. Показываю события "
    "из всех твоих календарей. Отмена текущего ввода — /cancel."
)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    u = await ensure_ready(message)
    if not u:
        return
    await message.answer(START_TEXT, reply_markup=main_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    AWAITING.pop(message.from_user.id, None)
    SETUP.pop(message.from_user.id, None)
    await message.answer("Ок, отменил.")


@dp.message(F.text == "📅 Сегодня")
async def btn_today(message: Message):
    u = await ensure_ready(message)
    if not u:
        return
    AWAITING.pop(message.from_user.id, None)
    text, kb = await render_day(0, u["user_id"])
    await message.answer(text, reply_markup=kb)


@dp.message(F.text == "🗓 Неделя")
async def btn_week(message: Message):
    u = await ensure_ready(message)
    if not u:
        return
    AWAITING.pop(message.from_user.id, None)
    start, _ = day_bounds(0, user_tz(u["user_id"]))
    await send_schedule(message, start, start + timedelta(days=7), u["user_id"], heatmap=True)


@dp.message(F.text == "📆 Месяц")
async def btn_month(message: Message):
    u = await ensure_ready(message)
    if not u:
        return
    AWAITING.pop(message.from_user.id, None)
    await render_month(message, u["user_id"])


@dp.message(F.text == "🔔 Напоминания")
async def btn_reminders(message: Message):
    u = await ensure_ready(message)
    if not u:
        return
    AWAITING.pop(message.from_user.id, None)
    tz = user_tz(u["user_id"])
    dt_from, _ = day_bounds(0, tz)
    try:
        client = cal.for_user(u["user_id"])
        events = await asyncio.to_thread(client.list_events, dt_from, dt_from + timedelta(days=30))
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not events:
        await message.answer("Ближайших событий нет — напоминать не о чем 🎉")
        return
    rows = []
    for e in events[:20]:
        token = new_token()
        EVENTS[token] = {"uid": e["uid"], "offset": 0, "cal": e.get("calendar")}
        rem = ("🔔 " + ", ".join(rem_label(m) for m in e["reminders_minutes"])
               if e.get("reminders_minutes") else "🔕 нет")
        rows.append([InlineKeyboardButton(
            text=f"{fmt_dt(e['start'], e['all_day'], tz)} · {e['title']} — {rem}"[:60],
            callback_data=f"rem:{token}",
        )])
    await message.answer(
        "🔔 <b>Напоминания</b>\nТапни событие, чтобы настроить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---------- настройки ----------

def render_settings(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    u = store.get_user(user_id) or {}
    cal_name = u.get("calendar_name") or "первый доступный"
    tz_name = user_tz_name(u)
    digest_on = bool(u.get("digest_enabled", 1))
    prov = (u.get("provider") or "icloud")
    if u.get("icloud_username"):
        acc_text = f"🔗 {PROVIDER_LABEL.get(prov, prov)}: {u['icloud_username']}"
    else:
        acc_text = "🔗 Подключить календарь (Apple / Google)"
    rows = [
        [InlineKeyboardButton(text=f"📁 Календарь: {cal_name}"[:60], callback_data="set:cal")],
        [InlineKeyboardButton(text=f"🌍 Часовой пояс: {tz_name}"[:60], callback_data="set:tz")],
        [InlineKeyboardButton(
            text=f"🌅 Утренний дайджест: {'вкл' if digest_on else 'выкл'}",
            callback_data="set:dig")],
        [InlineKeyboardButton(text="📊 Статистика недели", callback_data="set:stats")],
        [InlineKeyboardButton(text=acc_text[:60], callback_data="setup")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="👥 Пользователи", callback_data="adm:list")])
    rows.append([InlineKeyboardButton(text="🚪 Отключить и стереть данные", callback_data="set:bye")])
    return "⚙️ <b>Настройки</b>", InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message):
    uid = message.from_user.id
    u = store.get_user(uid)
    if u is None and is_admin(uid):
        store.create_user(uid, tg_display_name(message.from_user), status="approved")
        u = store.get_user(uid)
    if u is None or u["status"] != "approved":
        await ensure_ready(message)
        return
    AWAITING.pop(uid, None)
    text, kb = render_settings(uid)
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data == "set:menu")
async def cb_settings_menu(cq: CallbackQuery):
    text, kb = render_settings(cq.from_user.id)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data == "set:dig")
async def cb_settings_digest(cq: CallbackQuery):
    u = store.get_user(cq.from_user.id) or {}
    store.set_digest(cq.from_user.id, not bool(u.get("digest_enabled", 1)))
    text, kb = render_settings(cq.from_user.id)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data == "set:stats")
async def cb_settings_stats(cq: CallbackQuery):
    await cq.answer()
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.message.edit_text("Календарь не подключён — открой ⚙️ Настройки.")
        return
    await cq.message.edit_text("⏳ Считаю статистику…")
    try:
        text = await build_stats(cq.from_user.id)
    except Exception as e:
        text = f"⚠️ Не смог прочитать календарь: {e}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Настройки", callback_data="set:menu"),
    ]])
    await cq.message.edit_text(text, reply_markup=kb)


def _calendars_kb(names: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📁 {n}"[:60], callback_data=f"calpick:{i}")]
            for i, n in enumerate(names[:20])]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "set:cal")
async def cb_settings_calendar(cq: CallbackQuery):
    uid = cq.from_user.id
    client = client_or_none(uid)
    if client is None:
        await cq.answer("Сначала подключи календарь", show_alert=True)
        return
    try:
        names = await asyncio.to_thread(client.list_calendar_names)
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Не смог получить календари: {e}")
        await cq.answer()
        return
    if not names:
        await cq.message.edit_text("⚠️ Не нашёл календарей для событий.")
        await cq.answer()
        return
    SETUP[uid] = names
    await cq.message.edit_text("📁 Выбери календарь по умолчанию:", reply_markup=_calendars_kb(names))
    await cq.answer()


@dp.callback_query(F.data.startswith("calpick:"))
async def cb_calendar_pick(cq: CallbackQuery):
    uid = cq.from_user.id
    names = SETUP.get(uid)
    idx = int(cq.data.split(":", 1)[1])
    if not names or idx >= len(names):
        await cq.answer("Устарело — открой настройки заново", show_alert=True)
        return
    name = names[idx]
    store.set_calendar(uid, name)
    cal.drop(uid)
    SETUP.pop(uid, None)
    await cq.message.edit_text(
        f"📁 Календарь по умолчанию: <b>{name}</b>\n\nГотово! Пришли событие — "
        "текстом, голосом или фото афиши. Календарь можно поменять прямо в "
        "карточке события."
    )
    await cq.message.answer("Кнопки снизу — расписание и настройки.", reply_markup=main_kb())
    await cq.answer()


@dp.callback_query(F.data == "set:tz")
async def cb_settings_tz(cq: CallbackQuery):
    AWAITING[cq.from_user.id] = {"mode": "set_tz"}
    await cq.message.edit_text(
        "🌍 Пришли часовой пояс в формате IANA, например:\n"
        "<code>Europe/Moscow</code>, <code>Europe/Belgrade</code>, "
        "<code>Asia/Almaty</code>, <code>America/New_York</code>\n\nОтмена — /cancel"
    )
    await cq.answer()


@dp.callback_query(F.data == "set:bye")
async def cb_settings_bye(cq: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, стереть", callback_data="set:byey"),
        InlineKeyboardButton(text="❌ Нет", callback_data="set:menu"),
    ]])
    await cq.message.edit_text(
        "Удалить твой аккаунт календаря, пароль приложения и все данные из бота?\n"
        "События в самом календаре останутся нетронутыми.",
        reply_markup=kb,
    )
    await cq.answer()


@dp.callback_query(F.data == "set:byey")
async def cb_settings_bye_yes(cq: CallbackQuery):
    uid = cq.from_user.id
    store.remove_user_events(uid)
    store.delete_user(uid)
    cal.drop(uid)
    AWAITING.pop(uid, None)
    SETUP.pop(uid, None)
    await cq.message.edit_text("🚪 Данные удалены. Чтобы вернуться — /start.")
    await cq.answer()


# ---------- регистрация и выдача доступа ----------

@dp.callback_query(F.data == "reg")
async def cb_register(cq: CallbackQuery, bot: Bot):
    uid = cq.from_user.id
    u = store.get_user(uid)
    if u and u["status"] == "approved":
        await cq.answer("Доступ уже есть 🙂")
        return
    if u and u["status"] == "blocked":
        await cq.answer("Доступ закрыт.", show_alert=True)
        return
    if u and u["status"] == "pending":
        await cq.answer("Запрос уже отправлен, жди 🙂")
        return
    name = tg_display_name(cq.from_user)
    store.create_user(uid, name, status="pending")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"acc:ok:{uid}"),
        InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"acc:no:{uid}"),
    ]])
    try:
        await bot.send_message(
            config.ADMIN_USER_ID,
            f"🔑 <b>Запрос доступа</b>\n{name} (id <code>{uid}</code>)",
            reply_markup=kb,
        )
    except Exception:
        pass
    await cq.message.edit_text("Запрос отправлен владельцу. Напишу, как только доступ откроют 🙂")
    await cq.answer()


@dp.callback_query(F.data.startswith("acc:"))
async def cb_access_verdict(cq: CallbackQuery, bot: Bot):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    _, verdict, raw_uid = cq.data.split(":")
    target = int(raw_uid)
    u = store.get_user(target)
    name = (u or {}).get("tg_name") or str(target)
    if verdict == "ok":
        store.set_status(target, "approved")
        await cq.message.edit_text(f"✅ Доступ выдан: {name}")
        try:
            await bot.send_message(
                target,
                "✅ Доступ открыт! Осталось подключить календарь — Apple или Google:",
                reply_markup=setup_kb(),
            )
        except Exception:
            pass
    else:
        store.set_status(target, "blocked")
        await cq.message.edit_text(f"⛔ Отклонено: {name}")
        try:
            await bot.send_message(target, "Владелец отклонил запрос на доступ.")
        except Exception:
            pass
    await cq.answer()


# ---------- визард подключения календаря ----------

@dp.callback_query(F.data == "setup")
async def cb_setup(cq: CallbackQuery):
    uid = cq.from_user.id
    u = store.get_user(uid)
    if not u or u["status"] != "approved":
        await cq.answer("Сначала нужен доступ.", show_alert=True)
        return
    await cq.message.answer(
        "🔗 <b>Подключение календаря</b>\n\nВыбери провайдера:",
        reply_markup=provider_kb(),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("setpp:"))
async def cb_setup_provider(cq: CallbackQuery):
    uid = cq.from_user.id
    prov = cq.data.split(":", 1)[1]
    if prov not in config.CALDAV_URLS:
        await cq.answer("Неизвестный провайдер", show_alert=True)
        return
    u = store.get_user(uid)
    if not u or u["status"] != "approved":
        await cq.answer("Сначала нужен доступ.", show_alert=True)
        return
    AWAITING[uid] = {"mode": "setup_email", "provider": prov}
    await cq.message.edit_text(SETUP_STEP1[prov])
    await cq.answer()


# ---------- хендлеры: контент ----------

@dp.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot):
    u = await ensure_ready(message)
    if not u:
        return
    if not transcribe.enabled():
        await message.answer("Голосовые сейчас отключены. Пришли событие текстом или фото афиши 🙂")
        return
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    try:
        text = await transcribe.transcribe(buf.getvalue())
    except Exception as e:
        await message.answer(f"⚠️ Не расшифровал голос: {e}")
        return
    if not text:
        await message.answer("Не разобрал голосовое, повтори?")
        return
    tzname = user_tz_name(u)
    parsed = await llm.parse_text(text, now(user_tz(u["user_id"])), tzname)
    await handle_parsed(message, parsed, u)


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    u = await ensure_ready(message)
    if not u:
        return
    buf = io.BytesIO()
    await bot.download(message.photo[-1].file_id, destination=buf)
    tzname = user_tz_name(u)
    try:
        parsed = await llm.parse_image(
            buf.getvalue(), message.caption or "", now(user_tz(u["user_id"])), tzname
        )
    except Exception as e:
        await message.answer(f"⚠️ Не разобрал изображение: {e}")
        return
    await handle_parsed(message, parsed, u)


@dp.message(F.document)
async def on_document(message: Message, bot: Bot):
    # Картинка, присланная «как файл» (без сжатия) — частый случай при пересылке.
    # Telegram отдаёт её как document, а не photo, поэтому обычный фото-хендлер
    # её не ловит. Если это изображение — гоним по тому же пути, что и фото.
    u = await ensure_ready(message)
    if not u:
        return
    doc = message.document
    mime = (doc.mime_type or "").lower()
    if not mime.startswith("image/"):
        await message.answer(
            "Это файл, но не картинка. Пришли афишу картинкой или событие текстом 🙂"
        )
        return
    # Claude принимает jpeg/png/gif/webp; для прочего мягко откатываемся на jpeg.
    media_type = mime if mime in (
        "image/jpeg", "image/png", "image/gif", "image/webp"
    ) else "image/jpeg"
    buf = io.BytesIO()
    await bot.download(doc.file_id, destination=buf)
    tzname = user_tz_name(u)
    try:
        parsed = await llm.parse_image(
            buf.getvalue(), message.caption or "",
            now(user_tz(u["user_id"])), tzname, media_type=media_type,
        )
    except Exception as e:
        await message.answer(f"⚠️ Не разобрал изображение: {e}")
        return
    await handle_parsed(message, parsed, u)


@dp.message(F.text)
async def on_text(message: Message, bot: Bot):
    uid = message.from_user.id
    # шаги визарда/настроек доступны и до полного подключения
    pending = AWAITING.get(uid)
    if pending and pending.get("mode") in ("setup_email", "setup_password", "set_tz"):
        AWAITING.pop(uid, None)
        await handle_setup_text(message, pending, bot)
        return
    u = await ensure_ready(message)
    if not u:
        return
    pending = AWAITING.pop(uid, None)
    if pending:
        await handle_awaited_text(message, pending, u)
        return
    tzname = user_tz_name(u)
    parsed = await llm.parse_text(message.text, now(user_tz(uid)), tzname)
    await handle_parsed(message, parsed, u)


@dp.message()
async def on_unknown(message: Message, bot: Bot):
    # Запасной обработчик: всё, что не попало выше (видео, стикер, гео и т.п.).
    # Раньше такие сообщения молча терялись — теперь бот всегда отвечает.
    u = await ensure_ready(message)
    if not u:
        return
    await message.answer(
        "Не понял такой формат 🤔 Пришли событие текстом, голосом, "
        "картинкой афиши или скриншотом."
    )


async def handle_setup_text(message: Message, pending: dict, bot: Bot):
    uid = message.from_user.id
    u = store.get_user(uid)
    if not u or u["status"] != "approved":
        return  # доступ отозвали посреди визарда
    mode = pending["mode"]
    prov = pending.get("provider", "icloud")

    if mode == "setup_email":
        email = message.text.strip()
        if "@" not in email or "." not in email or " " in email:
            AWAITING[uid] = {"mode": "setup_email", "provider": prov}
            await message.answer("Это не похоже на email. Пришли адрес ещё раз (или /cancel).")
            return
        AWAITING[uid] = {"mode": "setup_password", "email": email, "provider": prov}
        await message.answer(SETUP_STEP2[prov])

    elif mode == "setup_password":
        email = pending["email"]
        pwd = message.text.strip()
        if prov == "google":
            pwd = pwd.replace(" ", "")  # Google показывает пароль с пробелами, они не часть пароля
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        status = await message.answer("⏳ Проверяю подключение к календарю…")
        url = config.CALDAV_URLS.get(prov, config.CALDAV_URL)
        try:
            names = await asyncio.to_thread(cal.test_connection, email, pwd, url)
        except Exception as e:
            AWAITING[uid] = {"mode": "setup_email", "provider": prov}
            await status.edit_text(
                f"⚠️ Не удалось подключиться: {e}\n\n"
                "Проверь, что это именно пароль приложения (не обычный пароль) "
                "и что двухфакторная аутентификация включена. Пришли email "
                "ещё раз (или /cancel)."
            )
            return
        store.set_credentials(uid, email, security.encrypt(pwd), prov)
        cal.drop(uid)
        if not names:
            await status.edit_text(
                "⚠️ Подключился, но не нашёл ни одного календаря для событий. "
                "Создай календарь у провайдера и открой ⚙️ Настройки → Календарь."
            )
            return
        SETUP[uid] = names
        await status.edit_text(
            "✅ Подключился! Выбери календарь по умолчанию:",
            reply_markup=_calendars_kb(names),
        )

    elif mode == "set_tz":
        name = message.text.strip()
        try:
            pytz.timezone(name)
        except Exception:
            AWAITING[uid] = {"mode": "set_tz"}
            await message.answer(
                "Не знаю такой пояс. Формат IANA, например <code>Europe/Moscow</code> "
                "или <code>Asia/Tbilisi</code> (или /cancel)."
            )
            return
        store.set_timezone(uid, name)
        cal.drop(uid)
        await message.answer(f"🌍 Часовой пояс: <b>{name}</b>")


async def handle_awaited_text(message: Message, pending: dict, u: dict):
    mode = pending.get("mode")
    tz = user_tz(u["user_id"])
    client = client_or_none(u["user_id"])
    if client is None:
        await message.answer("Календарь не подключён — открой ⚙️ Настройки.")
        return

    if mode == "rename":
        ev_dict = await asyncio.to_thread(client.get_event, pending["uid"], pending.get("cal"))
        if not ev_dict:
            await message.answer("⚠️ Событие не найдено.")
            return
        ev = event_from_caldav(ev_dict, tz)
        ev.title = message.text.strip()
        await _apply_update(message, pending["uid"], ev, "✅ Переименовано", u,
                            ev_dict.get("calendar"))

    elif mode == "reschedule":
        ev_dict = await asyncio.to_thread(client.get_event, pending["uid"], pending.get("cal"))
        if not ev_dict:
            await message.answer("⚠️ Событие не найдено.")
            return
        try:
            when = await llm.parse_when(message.text, now(tz), user_tz_name(u))
        except Exception as e:
            await message.answer(f"⚠️ Не разобрал время: {e}")
            return
        new_start = _iso(when.get("start"), tz)
        if not new_start:
            await message.answer("Не понял время. Напиши, например: «завтра в 15:00».")
            return
        ev = event_from_caldav(ev_dict, tz)
        _shift_to(ev, new_start, _iso(when.get("end"), tz), bool(when.get("all_day")))
        await _apply_update(message, pending["uid"], ev, "✅ Перенесено", u,
                            ev_dict.get("calendar"))

    elif mode == "remind_custom":
        m = _parse_reminder_minutes(message.text)
        if m is None:
            await message.answer("Не понял. Напиши, например: «за 30 минут» или «за 2 часа».")
            return
        token = pending["token"]
        data = PENDING.get(token)
        if not data:
            await message.answer("Действие устарело — пришли событие заново.")
            return
        rem = set(data["event"].get("reminders_minutes") or [])
        rem.add(m)
        data["event"]["reminders_minutes"] = sorted(rem)
        await _render_create_card(message, token, tz, edit=False)


def _shift_to(ev: Event, new_start: datetime, new_end: datetime | None, all_day: bool):
    duration = (ev.end - ev.start) if (ev.end and ev.start) else None
    ev.all_day = all_day
    ev.start = new_start
    if new_end:
        ev.end = new_end
    elif duration:
        ev.end = new_start + duration
    else:
        ev.end = None


def _parse_reminder_minutes(text: str) -> int | None:
    t = text.lower()
    m = re.search(r"(\d+)", t)
    if not m:
        return None
    n = int(m.group(1))
    if "час" in t:
        return n * 60
    if "дн" in t or "день" in t or "сут" in t:
        return n * 1440
    return n


async def _apply_update(message: Message, uid: str, ev: Event, ok_text: str, u: dict,
                        cal_name: str | None = None):
    tz = user_tz(u["user_id"])
    try:
        client = cal.for_user(u["user_id"])
        ok = await asyncio.to_thread(client.update_event, uid, ev, cal_name)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка записи в календарь: {e}")
        return
    if ok:
        store.update_start(uid, ev.title, ev.start)
        await message.answer(ok_text + "\n\n" + event_card(ev, tz))
    else:
        await message.answer("⚠️ Событие не найдено в календаре.")


# ---------- колбэки: навигация ----------

@dp.callback_query(F.data.startswith("day:"))
async def cb_day(cq: CallbackQuery):
    offset = int(cq.data.split(":", 1)[1])
    text, kb = await render_day(offset, cq.from_user.id)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("ev:"))
async def cb_event(cq: CallbackQuery):
    await show_event_card(cq, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("evu:"))
async def cb_event_by_uid(cq: CallbackQuery):
    """Открыть карточку по UID (кнопка «Карточка» на пинге напоминания)."""
    ev_uid = cq.data.split(":", 1)[1]
    token = new_token()
    EVENTS[token] = {"uid": ev_uid, "offset": 0,
                     "cal": store.get_event_calendar(ev_uid)}
    await show_event_card(cq, token)
    await cq.answer()


@dp.callback_query(F.data.startswith("snz:"))
async def cb_snooze(cq: CallbackQuery):
    """«+15 мин» на пинге напоминания."""
    rest = cq.data.split(":", 1)[1]
    ev_uid, minutes = rest.rsplit(":", 1)
    when = datetime.now().astimezone() + timedelta(minutes=int(minutes))
    store.set_snooze(ev_uid, when.isoformat())
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cq.answer(f"⏰ Напомню ещё раз через {minutes} мин")


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    offset = (EVENTS.get(token) or {}).get("offset", 0)
    text, kb = await render_day(offset, cq.from_user.id)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


# ---------- колбэки: выбор календаря в карточке создания ----------

@dp.callback_query(F.data.startswith("crl:"))
async def cb_create_calendar_menu(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    if token not in PENDING:
        await cq.answer("Устарело", show_alert=True)
        return
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    try:
        names = await asyncio.to_thread(client.list_calendar_names)
    except Exception as e:
        await cq.answer(f"Не смог получить календари: {e}"[:190], show_alert=True)
        return
    CALPICK[token] = names
    rows = [[InlineKeyboardButton(text=f"📁 {n}"[:60], callback_data=f"crp:{token}:{i}")]
            for i, n in enumerate(names[:20])]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"crb:{token}")])
    await cq.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cq.answer()


@dp.callback_query(F.data.startswith("crp:"))
async def cb_create_calendar_pick(cq: CallbackQuery):
    _, token, idx = cq.data.split(":")
    data = PENDING.get(token)
    names = CALPICK.get(token)
    if not data or not names or int(idx) >= len(names):
        await cq.answer("Устарело", show_alert=True)
        return
    data["calendar"] = names[int(idx)]
    CALPICK.pop(token, None)
    await _render_create_card(cq.message, token, user_tz(cq.from_user.id))
    await cq.answer(f"📁 {data['calendar']}")


@dp.callback_query(F.data.startswith("crb:"))
async def cb_create_calendar_back(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    if token not in PENDING:
        await cq.answer("Устарело", show_alert=True)
        return
    CALPICK.pop(token, None)
    await _render_create_card(cq.message, token, user_tz(cq.from_user.id))
    await cq.answer()


# ---------- колбэки: перенос ----------

@dp.callback_query(F.data.startswith("rsc:"))
async def cb_reschedule_menu(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="На завтра", callback_data=f"rsp:{token}:1"),
            InlineKeyboardButton(text="+1 неделя", callback_data=f"rsp:{token}:7"),
        ],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data=f"rsm:{token}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"ev:{token}")],
    ])
    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("rsp:"))
async def cb_reschedule_preset(cq: CallbackQuery):
    _, token, days = cq.data.split(":")
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    tz = user_tz(cq.from_user.id)
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    ev_dict = await asyncio.to_thread(client.get_event, data["uid"], data.get("cal"))
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    ev = event_from_caldav(ev_dict, tz)
    delta = timedelta(days=int(days))
    ev.start = _to_dt(ev.start, tz) + delta
    if ev.end:
        ev.end = _to_dt(ev.end, tz) + delta
    try:
        ok = await asyncio.to_thread(client.update_event, data["uid"], ev,
                                     ev_dict.get("calendar"))
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи: {e}")
        await cq.answer()
        return
    if ok:
        store.update_start(data["uid"], ev.title, ev.start)
        await cq.message.edit_text("✅ Перенесено\n\n" + event_card(ev, tz),
                                   reply_markup=event_actions_kb(token))
    else:
        await cq.message.edit_text("⚠️ Событие не найдено в календаре.")
    await cq.answer()


@dp.callback_query(F.data.startswith("rsm:"))
async def cb_reschedule_manual(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    AWAITING[cq.from_user.id] = {"mode": "reschedule", "uid": data["uid"], "cal": data.get("cal")}
    await cq.message.edit_text("🕒 Напиши новое время, например «завтра в 15:00» или «в пятницу 18:00».")
    await cq.answer()


# ---------- колбэки: переименование ----------

@dp.callback_query(F.data.startswith("rnm:"))
async def cb_rename(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    AWAITING[cq.from_user.id] = {"mode": "rename", "uid": data["uid"], "cal": data.get("cal")}
    await cq.message.edit_text("✏️ Напиши новое название события.")
    await cq.answer()


# ---------- колбэки: напоминания существующего события ----------

@dp.callback_query(F.data.startswith("rem:"))
async def cb_reminder_menu(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    ev_dict = await asyncio.to_thread(client.get_event, data["uid"], data.get("cal"))
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    REMWORK[token] = set(ev_dict.get("reminders_minutes") or [])
    data["title"] = ev_dict["title"]
    data["cal"] = ev_dict.get("calendar") or data.get("cal")
    await _render_reminder_menu(cq, token, ev_dict["title"])
    await cq.answer()


async def _render_reminder_menu(cq: CallbackQuery, token: str, title: str):
    active = REMWORK.get(token, set())
    cur = ", ".join(rem_label(m) for m in sorted(active)) if active else "выключены"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        reminder_toggle_row(active, "rmt", token),
        [
            InlineKeyboardButton(text="💾 Сохранить", callback_data=f"rms:{token}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"ev:{token}"),
        ],
    ])
    await cq.message.edit_text(f"🔔 <b>{title}</b>\nНапоминания: {cur}", reply_markup=kb)


@dp.callback_query(F.data.startswith("rmt:"))
async def cb_reminder_toggle(cq: CallbackQuery):
    _, token, m = cq.data.split(":")
    m = int(m)
    active = REMWORK.setdefault(token, set())
    active.discard(m) if m in active else active.add(m)
    title = (EVENTS.get(token) or {}).get("title", "Событие")
    await _render_reminder_menu(cq, token, title)
    await cq.answer()


@dp.callback_query(F.data.startswith("rms:"))
async def cb_reminder_save(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    tz = user_tz(cq.from_user.id)
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    ev_dict = await asyncio.to_thread(client.get_event, data["uid"], data.get("cal"))
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    ev = event_from_caldav(ev_dict, tz)
    ev.reminders_minutes = sorted(REMWORK.get(token, set()))
    try:
        ok = await asyncio.to_thread(client.update_event, data["uid"], ev,
                                     ev_dict.get("calendar"))
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи: {e}")
        await cq.answer()
        return
    await cq.message.edit_text(
        ("✅ Напоминания обновлены\n\n" if ok else "⚠️ Не удалось обновить\n\n") + event_card(ev, tz),
        reply_markup=event_actions_kb(token),
    )
    await cq.answer()


# ---------- колбэки: удаление ----------

@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_confirm(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"dely:{token}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"ev:{token}"),
    ]])
    await cq.message.edit_reply_markup(reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("dely:"))
async def cb_delete_yes(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = EVENTS.get(token)
    if not data:
        await cq.answer("Событие устарело", show_alert=True)
        return
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    try:
        ok = await asyncio.to_thread(client.delete_event, data["uid"], data.get("cal"))
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка удаления: {e}")
        await cq.answer()
        return
    if ok:
        store.remove(data["uid"])
        await cq.message.edit_text("🗑 Событие удалено.")
    else:
        await cq.message.edit_text("⚠️ Событие не найдено (возможно, уже удалено).")
    await cq.answer()


# ---------- колбэки: карточка создания (тоглы напоминаний + подтверждение) ----------

@dp.callback_query(F.data.startswith("crt:"))
async def cb_create_reminder_toggle(cq: CallbackQuery):
    _, token, m = cq.data.split(":")
    m = int(m)
    data = PENDING.get(token)
    if not data:
        await cq.answer("Устарело", show_alert=True)
        return
    rem = set(data["event"].get("reminders_minutes") or [])
    rem.discard(m) if m in rem else rem.add(m)
    data["event"]["reminders_minutes"] = sorted(rem)
    await _render_create_card(cq.message, token, user_tz(cq.from_user.id))
    await cq.answer()


@dp.callback_query(F.data.startswith("crc:"))
async def cb_create_reminder_custom(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    if token not in PENDING:
        await cq.answer("Устарело", show_alert=True)
        return
    AWAITING[cq.from_user.id] = {"mode": "remind_custom", "token": token}
    await cq.answer("Напиши, за сколько напомнить — например «за 30 минут»", show_alert=True)


@dp.callback_query(F.data.startswith("ok:"))
async def on_ok(cq: CallbackQuery):
    await cq.answer()
    token = cq.data.split(":", 1)[1]
    data = PENDING.pop(token, None)
    if not data:
        await cq.message.edit_text("Действие устарело — пришли событие заново.")
        return
    user_id = data.get("user_id", cq.from_user.id)
    tz = user_tz(user_id)

    if data["action"] == "bulk":
        await cq.message.edit_text("⏳ Применяю…")
        try:
            await _apply_bulk(cq, data)
        except Exception as e:
            await cq.message.edit_text(f"⚠️ Ошибка: {e}")
        return

    ev = Event.from_dict(data["event"])
    await cq.message.edit_text("⏳ Записываю в календарь…")
    try:
        client = cal.for_user(user_id)
        if data["action"] == "create":
            uid, cal_name = await asyncio.to_thread(
                client.create_event, ev, data.get("calendar")
            )
            store.add(uid, data["chat_id"], ev.title, ev.start, cal_name)
            undo_token = new_token()
            UNDO[undo_token] = {"uid": uid, "cal": cal_name}
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="↩️ Отменить", callback_data=f"und:{undo_token}"),
            ]])
            await cq.message.edit_text(
                "✅ Добавлено в календарь\n\n" + event_card(ev, tz, cal_name),
                reply_markup=kb,
            )
        else:  # edit
            ok = await asyncio.to_thread(
                client.update_event, data["uid"], ev, data.get("cal")
            )
            if ok:
                store.update_start(data["uid"], ev.title, ev.start)
                await cq.message.edit_text("✅ Изменено\n\n" + event_card(ev, tz))
            else:
                await cq.message.edit_text("⚠️ Событие не найдено в календаре.")
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи в календарь: {e}")


@dp.callback_query(F.data.startswith("und:"))
async def cb_undo(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = UNDO.pop(token, None)
    if not data:
        await cq.answer("Отменять уже нечего", show_alert=True)
        return
    client = client_or_none(cq.from_user.id)
    if client is None:
        await cq.answer("Календарь не подключён", show_alert=True)
        return
    try:
        ok = await asyncio.to_thread(client.delete_event, data["uid"], data.get("cal"))
    except Exception as e:
        await cq.answer(f"Не получилось: {e}"[:190], show_alert=True)
        return
    if ok:
        store.remove(data["uid"])
        await cq.message.edit_text("↩️ Отменено — событие удалено из календаря.")
    else:
        await cq.message.edit_text("⚠️ Событие не найдено (возможно, уже удалено).")
    await cq.answer()


@dp.callback_query(F.data.startswith("no:"))
async def on_no(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    PENDING.pop(token, None)
    CALPICK.pop(token, None)
    await cq.message.edit_text("Отменил.")
    await cq.answer()


# ---------- админ-панель ----------

def _admin_list_view() -> tuple[str, InlineKeyboardMarkup]:
    users = store.list_users()
    rows = []
    for u in users:
        icon = STATUS_ICON.get(u["status"], "❔")
        me = " (я)" if u["user_id"] == config.ADMIN_USER_ID else ""
        rows.append([InlineKeyboardButton(
            text=f"{icon} {u['tg_name'] or u['user_id']}{me}"[:60],
            callback_data=f"adm:u:{u['user_id']}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Настройки", callback_data="set:menu")])
    return (f"👥 <b>Пользователи</b> ({len(users)})",
            InlineKeyboardMarkup(inline_keyboard=rows))


def _admin_user_view(target: int) -> tuple[str, InlineKeyboardMarkup] | None:
    u = store.get_user(target)
    if not u:
        return None
    icon = STATUS_ICON.get(u["status"], "❔")
    prov = PROVIDER_LABEL.get(u.get("provider") or "icloud", u.get("provider") or "—")
    text = (
        f"{icon} <b>{u['tg_name'] or target}</b>\n"
        f"id: <code>{target}</code>\n"
        f"Статус: {u['status']}\n"
        f"Провайдер: {prov if u['icloud_username'] else '—'}\n"
        f"Аккаунт: {u['icloud_username'] or '—'}\n"
        f"Календарь: {u['calendar_name'] or '—'}\n"
        f"Пояс: {u['timezone'] or config.TIMEZONE}\n"
        f"Создан: {(u['created_at'] or '')[:10]}"
    )
    rows = []
    if u["status"] == "pending":
        rows.append([
            InlineKeyboardButton(text="✅ Дать доступ", callback_data=f"acc:ok:{target}"),
            InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"acc:no:{target}"),
        ])
    elif target != config.ADMIN_USER_ID:
        if u["status"] == "blocked":
            rows.append([InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm:blk:{target}:0")])
        else:
            rows.append([InlineKeyboardButton(text="⛔ Заблокировать", callback_data=f"adm:blk:{target}:1")])
        rows.append([InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"adm:delq:{target}")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="adm:list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm:list")
async def cb_admin_list(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    text, kb = _admin_list_view()
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("adm:u:"))
async def cb_admin_user(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    target = int(cq.data.split(":")[2])
    view = _admin_user_view(target)
    if view is None:
        await cq.answer("Пользователь уже удалён", show_alert=True)
        return
    await cq.message.edit_text(view[0], reply_markup=view[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("adm:blk:"))
async def cb_admin_block(cq: CallbackQuery, bot: Bot):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    _, _, raw_uid, flag = cq.data.split(":")
    target = int(raw_uid)
    if flag == "1":
        store.set_status(target, "blocked")
        cal.drop(target)
        try:
            await bot.send_message(target, "⛔ Владелец приостановил твой доступ к боту.")
        except Exception:
            pass
    else:
        store.set_status(target, "approved")
        try:
            await bot.send_message(target, "✅ Доступ к боту снова открыт.")
        except Exception:
            pass
    view = _admin_user_view(target)
    if view:
        await cq.message.edit_text(view[0], reply_markup=view[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("adm:delq:"))
async def cb_admin_delete_confirm(cq: CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    target = int(cq.data.split(":")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm:delc:{target}"),
        InlineKeyboardButton(text="❌ Нет", callback_data=f"adm:u:{target}"),
    ]])
    await cq.message.edit_text(
        "Удалить пользователя со всеми кредами и данными?\n"
        "События в его календаре останутся нетронутыми.",
        reply_markup=kb,
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("adm:delc:"))
async def cb_admin_delete_yes(cq: CallbackQuery, bot: Bot):
    if not is_admin(cq.from_user.id):
        await cq.answer()
        return
    target = int(cq.data.split(":")[2])
    store.remove_user_events(target)
    store.delete_user(target)
    cal.drop(target)
    try:
        await bot.send_message(target, "Твои данные удалены из бота. Чтобы вернуться — /start.")
    except Exception:
        pass
    text, kb = _admin_list_view()
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


# ---------- запуск ----------

def bootstrap_admin():
    """Гарантировать запись админа; при первом запуске импортировать его
    iCloud-креды из переменных окружения (обратная совместимость)."""
    u = store.get_user(config.ADMIN_USER_ID)
    if not u:
        store.create_user(config.ADMIN_USER_ID, "admin", status="approved")
        u = store.get_user(config.ADMIN_USER_ID)
    if u["status"] != "approved":
        store.set_status(config.ADMIN_USER_ID, "approved")
    if not u["icloud_username"] and config.ICLOUD_USERNAME and config.ICLOUD_PASSWORD:
        store.set_credentials(
            config.ADMIN_USER_ID,
            config.ICLOUD_USERNAME,
            security.encrypt(config.ICLOUD_PASSWORD),
            provider="icloud",
        )
        if config.ICLOUD_CALENDAR_NAME:
            store.set_calendar(config.ADMIN_USER_ID, config.ICLOUD_CALENDAR_NAME)
        print("Admin iCloud credentials imported from environment.")


async def main():
    bootstrap_admin()
    bot = Bot(config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск / меню"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
    ])
    notifier.setup(bot, asyncio.get_running_loop())  # пинги + утренний дайджест
    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
