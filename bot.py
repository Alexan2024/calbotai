"""Telegram-бот: события в любом формате -> Apple Calendar (через iCloud CalDAV).

UX кнопочный: постоянная reply-клавиатура (Сегодня/Неделя/Месяц/Напоминания),
инлайн-навигация по дням, тапабельные события с действиями (перенос, переименование,
напоминания, удаление). Правки идут по UID события, а не по нечёткому совпадению
названия — надёжнее. Текстовый разбор («перенеси врача») остаётся как запасной путь.
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
from models import Event

TZ = pytz.timezone(config.TIMEZONE)
dp = Dispatcher()

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# category -> эмодзи. Значок выбирает бот по category от LLM, не сама модель,
# чтобы одинаковые события всегда получали один и тот же символ.
CATEGORY_EMOJI = {
    "health": "🩺", "sport": "💪", "call_online": "🎧", "meeting": "🤝",
    "call": "📞", "deadline": "⏰", "birthday": "🎂", "travel": "✈️",
    "food": "🍽", "event": "🎫", "study": "📚", "service": "💇", "other": "📌",
}

REMINDER_PRESETS = [10, 60, 1440]  # 10 мин, 1 час, 1 день

# --- состояние в памяти ---
# подтверждения создания/правки: token -> {"action","event","chat_id",...}
PENDING: dict[str, dict] = {}
# тапнутые события календаря: token -> {"uid","offset"}
EVENTS: dict[str, dict] = {}
# рабочий набор напоминаний в меню существующего события: token -> set(minutes)
REMWORK: dict[str, set] = {}
# ожидание текстового ввода: user_id -> {"mode","uid"/"token"}
AWAITING: dict[int, dict] = {}


# ---------- вспомогательное ----------

def now() -> datetime:
    return datetime.now(TZ)


def allowed(message: Message) -> bool:
    return not config.ALLOWED_USER_IDS or message.from_user.id in config.ALLOWED_USER_IDS


def new_token() -> str:
    return uuid.uuid4().hex[:12]


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="🗓 Неделя")],
            [KeyboardButton(text="📆 Месяц"), KeyboardButton(text="🔔 Напоминания")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def fmt_dt(dt, all_day: bool = False) -> str:
    if isinstance(dt, datetime) and dt.tzinfo:
        dt = dt.astimezone(TZ)
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


def event_card(ev: Event) -> str:
    # эмодзи уже внутри ev.title (ставится при разборе), поэтому без ведущего 📌
    lines = [f"<b>{ev.title}</b>", f"🕒 {fmt_dt(ev.start, ev.all_day)}"]
    if ev.end and not ev.all_day:
        lines[-1] += f" – {ev.end.astimezone(TZ).strftime('%H:%M')}"
    if ev.location:
        lines.append(f"📍 {ev.location}")
    if ev.notes:
        lines.append(f"📝 {ev.notes}")
    if ev.reminders_minutes:
        rem = ", ".join(rem_label(m) for m in ev.reminders_minutes)
        lines.append(f"🔔 {rem}")
    return "\n".join(lines)


def event_from_llm(d: dict) -> Event:
    start = datetime.fromisoformat(d["start"])
    if start.tzinfo is None:
        start = TZ.localize(start)
    end = None
    if d.get("end"):
        end = datetime.fromisoformat(d["end"])
        if end.tzinfo is None:
            end = TZ.localize(end)
    reminders = d.get("reminders_minutes") or list(config.DEFAULT_REMINDERS)
    emoji = CATEGORY_EMOJI.get(d.get("category") or "other", "📌")
    raw_title = (d.get("title") or "Событие").strip()
    return Event(
        title=f"{emoji} {raw_title}",
        start=start,
        end=end,
        all_day=bool(d.get("all_day")),
        location=d.get("location"),
        notes=d.get("notes"),
        reminders_minutes=reminders,
    )


def event_from_caldav(d: dict) -> Event:
    """Собрать Event из полного словаря get_event (для правок без потери данных)."""
    return Event(
        title=d["title"],
        start=_to_dt(d["start"]),
        end=_to_dt(d["end"]) if d.get("end") else None,
        all_day=d.get("all_day", False),
        location=d.get("location"),
        notes=d.get("notes"),
        reminders_minutes=list(d.get("reminders_minutes") or []),
        uid=d["uid"],
    )


def _to_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else TZ.localize(v)
    return TZ.localize(datetime.combine(v, datetime.min.time()))  # date


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return TZ.localize(dt) if dt.tzinfo is None else dt
    except ValueError:
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


def create_confirm_kb(token: str, active: set) -> InlineKeyboardMarkup:
    rows = [
        reminder_toggle_row(active, "crt", token),
        [InlineKeyboardButton(text="✏️ Своё напоминание", callback_data=f"crc:{token}")],
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


# ---------- разбор результата LLM ----------

async def handle_parsed(message: Message, parsed: dict):
    intent = parsed.get("intent", "chitchat")

    if intent == "create":
        events = parsed.get("events") or []
        if not events:
            await message.answer("Не увидел события. Уточни дату/время?")
            return
        for ed in events:
            ev = event_from_llm(ed)
            token = new_token()
            PENDING[token] = {"action": "create", "event": ev.to_dict(),
                              "chat_id": message.chat.id}
            await message.answer(
                event_card(ev) + "\n\nДобавить в календарь?",
                reply_markup=create_confirm_kb(token, set(ev.reminders_minutes)),
            )

    elif intent == "query":
        q = parsed.get("query") or {}
        dt_from = _iso(q.get("from")) or now()
        dt_to = _iso(q.get("to")) or (dt_from + timedelta(days=1))
        await send_schedule(message, dt_from, dt_to)

    elif intent == "edit":
        await handle_edit(message, parsed.get("edit") or {})

    else:
        await message.answer(parsed.get("reply") or "Пришли событие текстом, голосом или афишей 🙂")


async def send_schedule(message: Message, dt_from: datetime, dt_to: datetime):
    try:
        events = await asyncio.to_thread(cal.list_events, dt_from, dt_to)
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
        lines.append(_schedule_line(e))
    await message.answer(header + "\n".join(lines))


def _schedule_line(e: dict) -> str:
    when = fmt_dt(e["start"], e["all_day"])
    # в дневной/недельной сводке дата уже в заголовке — оставим только время
    time_part = when.split(") ")[-1] if isinstance(e["start"], datetime) and not e["all_day"] else "весь день"
    line = f"• {time_part} — {e['title']}"
    if e.get("location"):
        line += f" ({e['location']})"
    return line


# --- текстовая правка (запасной путь для «перенеси врача») ---

async def handle_edit(message: Message, edit: dict):
    title = (edit.get("match_title") or "").strip()
    changes = edit.get("changes") or {}
    if not title:
        await message.answer("Какое событие изменить? Напиши название или тапни его в списке (📅 Сегодня).")
        return
    matches = await asyncio.to_thread(
        cal.find_by_title, title, now() - timedelta(days=30), now() + timedelta(days=60)
    )
    if not matches:
        await message.answer(f"Не нашёл событие «{title}» в календаре.")
        return
    if len(matches) > 1:
        opts = "\n".join(f"• {fmt_dt(m['start'], m['all_day'])} — {m['title']}" for m in matches[:8])
        await message.answer(f"Нашёл несколько «{title}». Уточни или тапни в списке:\n{opts}")
        return

    target = matches[0]
    base = await asyncio.to_thread(cal.get_event, target["uid"]) or target
    new = event_from_caldav(base)
    if changes.get("title"):
        new.title = changes["title"]
    if _iso(changes.get("start")):
        new.start = _iso(changes["start"])
    if _iso(changes.get("end")):
        new.end = _iso(changes["end"])
    if "location" in changes:
        new.location = changes["location"]
    if "notes" in changes:
        new.notes = changes["notes"]
    if changes.get("reminders_minutes"):
        new.reminders_minutes = changes["reminders_minutes"]

    token = new_token()
    PENDING[token] = {"action": "edit", "event": new.to_dict(),
                      "uid": target["uid"], "chat_id": message.chat.id}
    await message.answer(
        "Изменить на:\n\n" + event_card(new) + "\n\nПрименить?",
        reply_markup=edit_confirm_kb(token),
    )


# ---------- дневная навигация ----------

def day_bounds(offset: int) -> tuple[datetime, datetime]:
    start = now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset)
    return start, start + timedelta(days=1)


def day_title(offset: int) -> str:
    start, _ = day_bounds(offset)
    label = f"{start.strftime('%d.%m')} ({DAYS[start.weekday()]})"
    if offset == 0:
        return f"Сегодня, {label}"
    if offset == 1:
        return f"Завтра, {label}"
    if offset == -1:
        return f"Вчера, {label}"
    return label


async def render_day(offset: int) -> tuple[str, InlineKeyboardMarkup]:
    dt_from, dt_to = day_bounds(offset)
    try:
        events = await asyncio.to_thread(cal.list_events, dt_from, dt_to)
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

    if not events:
        text = f"🗓 <b>{day_title(offset)}</b>\n\nПусто 🎉"
    else:
        lines = [f"🗓 <b>{day_title(offset)}</b>", "", "Тапни событие, чтобы изменить:"]
        for e in events:
            token = new_token()
            EVENTS[token] = {"uid": e["uid"], "offset": offset}
            when = (e["start"].astimezone(TZ).strftime("%H:%M")
                    if isinstance(e["start"], datetime) and not e["all_day"] else "весь день")
            rows.append([InlineKeyboardButton(
                text=f"{when} · {e['title']}"[:60],
                callback_data=f"ev:{token}",
            )])
        text = "\n".join(lines)

    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def render_month(message: Message):
    dt_from, _ = day_bounds(0)
    dt_to = dt_from + timedelta(days=30)
    try:
        events = await asyncio.to_thread(cal.list_events, dt_from, dt_to)
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
        lines.append(_schedule_line(e))
    await message.answer("\n".join(lines))


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
    ev_dict = await asyncio.to_thread(cal.get_event, data["uid"])
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено (возможно, удалено).")
        return
    ev = event_from_caldav(ev_dict)
    await cq.message.edit_text(event_card(ev), reply_markup=event_actions_kb(token))


# ---------- хендлеры: команды и кнопки ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not allowed(message):
        return
    await message.answer(
        "Привет! Я добавляю события в твой Apple Calendar.\n\n"
        "Просто пришли событие в любом виде — текст, голосовое, фото афиши или "
        "пересланное сообщение. Я покажу карточку и запишу после подтверждения.\n\n"
        "Кнопки снизу — расписание и напоминания. Чтобы изменить событие, "
        "открой день и тапни по нему.",
        reply_markup=main_kb(),
    )


@dp.message(F.text == "📅 Сегодня")
async def btn_today(message: Message):
    if not allowed(message):
        return
    AWAITING.pop(message.from_user.id, None)
    text, kb = await render_day(0)
    await message.answer(text, reply_markup=kb)


@dp.message(F.text == "🗓 Неделя")
async def btn_week(message: Message):
    if not allowed(message):
        return
    AWAITING.pop(message.from_user.id, None)
    start, _ = day_bounds(0)
    await send_schedule(message, start, start + timedelta(days=7))


@dp.message(F.text == "📆 Месяц")
async def btn_month(message: Message):
    if not allowed(message):
        return
    AWAITING.pop(message.from_user.id, None)
    await render_month(message)


@dp.message(F.text == "🔔 Напоминания")
async def btn_reminders(message: Message):
    if not allowed(message):
        return
    AWAITING.pop(message.from_user.id, None)
    dt_from, _ = day_bounds(0)
    try:
        events = await asyncio.to_thread(cal.list_events, dt_from, dt_from + timedelta(days=30))
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not events:
        await message.answer("Ближайших событий нет — напоминать не о чем 🎉")
        return
    rows = []
    for e in events[:20]:
        token = new_token()
        EVENTS[token] = {"uid": e["uid"], "offset": 0}
        rem = ("🔔 " + ", ".join(rem_label(m) for m in e["reminders_minutes"])
               if e.get("reminders_minutes") else "🔕 нет")
        rows.append([InlineKeyboardButton(
            text=f"{fmt_dt(e['start'], e['all_day'])} · {e['title']} — {rem}"[:60],
            callback_data=f"rem:{token}",
        )])
    await message.answer(
        "🔔 <b>Напоминания</b>\nТапни событие, чтобы настроить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ---------- хендлеры: контент ----------

@dp.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot):
    if not allowed(message):
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
    parsed = await llm.parse_text(text, now(), config.TIMEZONE)
    await handle_parsed(message, parsed)


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    if not allowed(message):
        return
    buf = io.BytesIO()
    await bot.download(message.photo[-1].file_id, destination=buf)
    try:
        parsed = await llm.parse_image(buf.getvalue(), message.caption or "", now(), config.TIMEZONE)
    except Exception as e:
        await message.answer(f"⚠️ Не разобрал изображение: {e}")
        return
    await handle_parsed(message, parsed)


@dp.message(F.text)
async def on_text(message: Message):
    if not allowed(message):
        return
    # сначала — ждём ли мы ввод для ручного переноса / переименования / своего напоминания
    pending = AWAITING.pop(message.from_user.id, None)
    if pending:
        await handle_awaited_text(message, pending)
        return
    parsed = await llm.parse_text(message.text, now(), config.TIMEZONE)
    await handle_parsed(message, parsed)


async def handle_awaited_text(message: Message, pending: dict):
    mode = pending.get("mode")

    if mode == "rename":
        ev_dict = await asyncio.to_thread(cal.get_event, pending["uid"])
        if not ev_dict:
            await message.answer("⚠️ Событие не найдено.")
            return
        ev = event_from_caldav(ev_dict)
        ev.title = message.text.strip()
        await _apply_update(message, pending["uid"], ev, "✅ Переименовано")

    elif mode == "reschedule":
        ev_dict = await asyncio.to_thread(cal.get_event, pending["uid"])
        if not ev_dict:
            await message.answer("⚠️ Событие не найдено.")
            return
        try:
            when = await llm.parse_when(message.text, now(), config.TIMEZONE)
        except Exception as e:
            await message.answer(f"⚠️ Не разобрал время: {e}")
            return
        new_start = _iso(when.get("start"))
        if not new_start:
            await message.answer("Не понял время. Напиши, например: «завтра в 15:00».")
            return
        ev = event_from_caldav(ev_dict)
        _shift_to(ev, new_start, _iso(when.get("end")), bool(when.get("all_day")))
        await _apply_update(message, pending["uid"], ev, "✅ Перенесено")

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
        ev = Event.from_dict(data["event"])
        await message.answer(
            event_card(ev) + "\n\nДобавить в календарь?",
            reply_markup=create_confirm_kb(token, set(ev.reminders_minutes)),
        )


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


async def _apply_update(message: Message, uid: str, ev: Event, ok_text: str):
    try:
        ok = await asyncio.to_thread(cal.update_event, uid, ev)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка записи в календарь: {e}")
        return
    if ok:
        store.update_start(uid, ev.title, ev.start)
        await message.answer(ok_text + "\n\n" + event_card(ev))
    else:
        await message.answer("⚠️ Событие не найдено в календаре.")


# ---------- колбэки: навигация ----------

@dp.callback_query(F.data.startswith("day:"))
async def cb_day(cq: CallbackQuery):
    offset = int(cq.data.split(":", 1)[1])
    text, kb = await render_day(offset)
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data.startswith("ev:"))
async def cb_event(cq: CallbackQuery):
    await show_event_card(cq, cq.data.split(":", 1)[1])
    await cq.answer()


@dp.callback_query(F.data.startswith("back:"))
async def cb_back(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    offset = (EVENTS.get(token) or {}).get("offset", 0)
    text, kb = await render_day(offset)
    await cq.message.edit_text(text, reply_markup=kb)
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
    ev_dict = await asyncio.to_thread(cal.get_event, data["uid"])
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    ev = event_from_caldav(ev_dict)
    delta = timedelta(days=int(days))
    ev.start = _to_dt(ev.start) + delta
    if ev.end:
        ev.end = _to_dt(ev.end) + delta
    try:
        ok = await asyncio.to_thread(cal.update_event, data["uid"], ev)
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи: {e}")
        await cq.answer()
        return
    if ok:
        store.update_start(data["uid"], ev.title, ev.start)
        await cq.message.edit_text("✅ Перенесено\n\n" + event_card(ev),
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
    AWAITING[cq.from_user.id] = {"mode": "reschedule", "uid": data["uid"]}
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
    AWAITING[cq.from_user.id] = {"mode": "rename", "uid": data["uid"]}
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
    ev_dict = await asyncio.to_thread(cal.get_event, data["uid"])
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    REMWORK[token] = set(ev_dict.get("reminders_minutes") or [])
    data["title"] = ev_dict["title"]
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
    ev_dict = await asyncio.to_thread(cal.get_event, data["uid"])
    if not ev_dict:
        await cq.message.edit_text("⚠️ Событие не найдено.")
        await cq.answer()
        return
    ev = event_from_caldav(ev_dict)
    ev.reminders_minutes = sorted(REMWORK.get(token, set()))
    try:
        ok = await asyncio.to_thread(cal.update_event, data["uid"], ev)
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи: {e}")
        await cq.answer()
        return
    await cq.message.edit_text(
        ("✅ Напоминания обновлены\n\n" if ok else "⚠️ Не удалось обновить\n\n") + event_card(ev),
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
    try:
        ok = await asyncio.to_thread(cal.delete_event, data["uid"])
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
    ev = Event.from_dict(data["event"])
    await cq.message.edit_text(
        event_card(ev) + "\n\nДобавить в календарь?",
        reply_markup=create_confirm_kb(token, rem),
    )
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
    ev = Event.from_dict(data["event"])
    await cq.message.edit_text("⏳ Записываю в календарь…")
    try:
        if data["action"] == "create":
            uid = await asyncio.to_thread(cal.create_event, ev)
            store.add(uid, data["chat_id"], ev.title, ev.start)
            await cq.message.edit_text("✅ Добавлено в календарь\n\n" + event_card(ev))
        else:  # edit
            ok = await asyncio.to_thread(cal.update_event, data["uid"], ev)
            if ok:
                store.update_start(data["uid"], ev.title, ev.start)
                await cq.message.edit_text("✅ Изменено\n\n" + event_card(ev))
            else:
                await cq.message.edit_text("⚠️ Событие не найдено в календаре.")
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи в календарь: {e}")


@dp.callback_query(F.data.startswith("no:"))
async def on_no(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    PENDING.pop(token, None)
    await cq.message.edit_text("Отменил.")
    await cq.answer()


# ---------- запуск ----------

async def main():
    bot = Bot(config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.set_my_commands([BotCommand(command="start", description="Запуск / меню")])
    if config.TELEGRAM_REMINDERS:
        notifier.setup(bot, asyncio.get_running_loop())
    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
