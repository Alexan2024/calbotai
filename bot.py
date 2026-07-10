"""Telegram-бот: события в любом формате -> Apple Calendar (через iCloud CalDAV)."""
from __future__ import annotations
import asyncio
import io
import uuid
from datetime import datetime, timedelta

import pytz
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
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

# Ожидающие подтверждения действия: token -> {"action": "create"|"edit", ...}
PENDING: dict[str, dict] = {}


# ---------- вспомогательное ----------

def now() -> datetime:
    return datetime.now(TZ)


def allowed(message: Message) -> bool:
    return not config.ALLOWED_USER_IDS or message.from_user.id in config.ALLOWED_USER_IDS


def fmt_dt(dt: datetime, all_day: bool = False) -> str:
    if isinstance(dt, datetime) and dt.tzinfo:
        dt = dt.astimezone(TZ)
    days = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    if all_day or not isinstance(dt, datetime):
        d = dt.date() if isinstance(dt, datetime) else dt
        return f"{d.strftime('%d.%m')} ({days[d.weekday()]}), весь день"
    return f"{dt.strftime('%d.%m')} ({days[dt.weekday()]}) {dt.strftime('%H:%M')}"


def event_card(ev: Event) -> str:
    lines = [f"📌 <b>{ev.title}</b>", f"🕒 {fmt_dt(ev.start, ev.all_day)}"]
    if ev.end and not ev.all_day:
        lines[-1] += f" – {ev.end.astimezone(TZ).strftime('%H:%M')}"
    if ev.location:
        lines.append(f"📍 {ev.location}")
    if ev.notes:
        lines.append(f"📝 {ev.notes}")
    if ev.reminders_minutes:
        rem = ", ".join(f"{m} мин" for m in ev.reminders_minutes)
        lines.append(f"🔔 напоминание: {rem}")
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
    return Event(
        title=d.get("title") or "Событие",
        start=start,
        end=end,
        all_day=bool(d.get("all_day")),
        location=d.get("location"),
        notes=d.get("notes"),
        reminders_minutes=reminders,
    )


def confirm_kb(token: str, action: str) -> InlineKeyboardMarkup:
    verb = "Добавить" if action == "create" else "Применить"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ {verb}", callback_data=f"ok:{token}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"no:{token}"),
    ]])


# ---------- нормализация ввода в текст/картинку ----------

async def download(bot: Bot, file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


# ---------- обработка результата LLM ----------

async def handle_parsed(message: Message, parsed: dict):
    intent = parsed.get("intent", "chitchat")

    if intent == "create":
        events = parsed.get("events") or []
        if not events:
            await message.answer("Не увидел события. Уточни дату/время?")
            return
        for ed in events:
            ev = event_from_llm(ed)
            token = uuid.uuid4().hex[:12]
            PENDING[token] = {"action": "create", "event": ev.to_dict(),
                              "chat_id": message.chat.id}
            await message.answer(
                event_card(ev) + "\n\nДобавить в календарь?",
                reply_markup=confirm_kb(token, "create"),
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


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return TZ.localize(dt) if dt.tzinfo is None else dt
    except ValueError:
        return None


async def send_schedule(message: Message, dt_from: datetime, dt_to: datetime):
    try:
        events = cal.list_events(dt_from, dt_to)
    except Exception as e:
        await message.answer(f"⚠️ Не смог прочитать календарь: {e}")
        return
    if not events:
        await message.answer("На этот период событий нет 🎉")
        return
    header = f"🗓 {dt_from.strftime('%d.%m')} – {dt_to.strftime('%d.%m')}\n"
    lines = []
    for e in events:
        line = f"• {fmt_dt(e['start'], e['all_day'])} — <b>{e['title']}</b>"
        if e.get("location"):
            line += f" ({e['location']})"
        lines.append(line)
    await message.answer(header + "\n".join(lines))


async def handle_edit(message: Message, edit: dict):
    title = (edit.get("match_title") or "").strip()
    changes = edit.get("changes") or {}
    if not title:
        await message.answer("Какое событие изменить? Напиши название.")
        return
    # ищем в окне ±60 дней
    matches = cal.find_by_title(title, now() - timedelta(days=30), now() + timedelta(days=60))
    if not matches:
        await message.answer(f"Не нашёл событие «{title}» в календаре.")
        return
    if len(matches) > 1:
        opts = "\n".join(f"• {fmt_dt(m['start'], m['all_day'])} — {m['title']}" for m in matches[:8])
        await message.answer(f"Нашёл несколько «{title}». Уточни, какое:\n{opts}")
        return

    target = matches[0]
    # строим новое событие: старые значения + изменения
    new = Event(
        title=changes.get("title") or target["title"],
        start=_iso(changes.get("start")) or _to_dt(target["start"]),
        end=_iso(changes.get("end")) or (_to_dt(target["end"]) if target.get("end") else None),
        all_day=changes.get("all_day", target["all_day"]),
        location=changes.get("location") if "location" in changes else target.get("location"),
        notes=changes.get("notes"),
        reminders_minutes=changes.get("reminders_minutes") or list(config.DEFAULT_REMINDERS),
        uid=target["uid"],
    )
    token = uuid.uuid4().hex[:12]
    PENDING[token] = {"action": "edit", "event": new.to_dict(),
                      "uid": target["uid"], "chat_id": message.chat.id}
    await message.answer(
        "Изменить на:\n\n" + event_card(new) + "\n\nПрименить?",
        reply_markup=confirm_kb(token, "edit"),
    )


def _to_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else TZ.localize(v)
    # date
    return TZ.localize(datetime.combine(v, datetime.min.time()))


# ---------- хендлеры ----------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not allowed(message):
        return
    await message.answer(
        "Привет! Я добавляю события в твой Apple Calendar.\n\n"
        "Присылай в любом виде: текст, голосовое, афишу фото или пересланное сообщение.\n\n"
        "Команды:\n"
        "/today — план на сегодня\n"
        "/week — план на неделю\n"
        "Ещё умею менять события и ставить напоминания — просто напиши, что нужно."
    )


@dp.message(Command("today"))
async def cmd_today(message: Message):
    if not allowed(message):
        return
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    await send_schedule(message, start, start + timedelta(days=1))


@dp.message(Command("week"))
async def cmd_week(message: Message):
    if not allowed(message):
        return
    start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    await send_schedule(message, start, start + timedelta(days=7))


@dp.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot):
    if not allowed(message):
        return
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    audio = await download(bot, file_id)
    try:
        text = await transcribe.transcribe(audio)
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
    img = await download(bot, message.photo[-1].file_id)  # самое большое разрешение
    try:
        parsed = await llm.parse_image(img, message.caption or "", now(), config.TIMEZONE)
    except Exception as e:
        await message.answer(f"⚠️ Не разобрал изображение: {e}")
        return
    await handle_parsed(message, parsed)


@dp.message(F.text)
async def on_text(message: Message):
    if not allowed(message):
        return
    parsed = await llm.parse_text(message.text, now(), config.TIMEZONE)
    await handle_parsed(message, parsed)


@dp.callback_query(F.data.startswith("ok:"))
async def on_ok(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    data = PENDING.pop(token, None)
    if not data:
        await cq.answer("Действие устарело.")
        return
    ev = Event.from_dict(data["event"])
    try:
        if data["action"] == "create":
            uid = cal.create_event(ev)
            store.add(uid, data["chat_id"], ev.title, ev.start)
            await cq.message.edit_text("✅ Добавлено в календарь\n\n" + event_card(ev))
        else:  # edit
            ok = cal.update_event(data["uid"], ev)
            if ok:
                store.update_start(data["uid"], ev.title, ev.start)
                await cq.message.edit_text("✅ Изменено\n\n" + event_card(ev))
            else:
                await cq.message.edit_text("⚠️ Событие не найдено в календаре.")
    except Exception as e:
        await cq.message.edit_text(f"⚠️ Ошибка записи в календарь: {e}")
    await cq.answer()


@dp.callback_query(F.data.startswith("no:"))
async def on_no(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    PENDING.pop(token, None)
    await cq.message.edit_text("Отменил.")
    await cq.answer()


# ---------- запуск ----------

async def main():
    bot = Bot(config.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if config.TELEGRAM_REMINDERS:
        notifier.setup(bot, asyncio.get_running_loop())
    print("Bot started.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
