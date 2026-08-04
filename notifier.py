"""Telegram-напоминания и утренний дайджест.

Нативные напоминания Apple ставятся через VALARM в самом событии.
Здесь — дополнительный пинг в чат (с кнопками «+15 мин» и «Карточка»)
и ежеутренний дайджест дня по ВСЕМ календарям пользователя.

Дайджест рисуется «рельсой» ▍ (render.digest_rail) — построчно, вне <pre>.

Важно: перед каждым пингом событие сверяется с живым календарём по UID.
Если его удалили напрямую в Apple/Google — забываем и не пингуем; если
перенесли — берём новое время; если переименовали — новое название. Так
локальная база сама себя чистит от призраков.
"""
import asyncio
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import calendar_client as cal
import config
import store
import render

CHECK_EVERY_SECONDS = 60
LEAD_SECONDS = 10 * 60  # напомнить за 10 минут до начала

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _ping_kb(uid: str) -> InlineKeyboardMarkup | None:
    # callback_data ограничена 64 байтами — для чужих длинных UID кнопки не ставим
    if len(uid) > 55:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏰ +15 мин", callback_data=f"snz:{uid}:15"),
        InlineKeyboardButton(text="📋 Карточка", callback_data=f"evu:{uid}"),
    ]])


async def _send_digest(bot, u: dict):
    user_id = u["user_id"]
    try:
        tz = pytz.timezone(u.get("timezone") or config.TIMEZONE)
    except Exception:
        tz = pytz.timezone(config.TIMEZONE)
    day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        client = cal.for_user(user_id)
        events = await asyncio.to_thread(
            client.list_events, day_start, day_start + timedelta(days=1)
        )
    except Exception:
        return  # креды протухли/сеть — молча пропускаем, попробуем завтра
    header = f"🌅 План на {day_start.strftime('%d.%m')} ({DAYS[day_start.weekday()]})"
    text = render.digest_rail(events, tz, header)
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass  # пользователь заблокировал бота и т.п.


def setup(bot, loop):
    scheduler = AsyncIOScheduler(event_loop=loop)

    async def reminder_tick():
        for uid, chat_id, title, start_iso in store.due_for_reminder(LEAD_SECONDS):
            # --- сверка с живым календарём: событие могли изменить/удалить напрямую ---
            checked = False
            try:
                client = cal.for_user(chat_id)   # в личном чате chat_id == user_id
                cal_hint = store.get_event_calendar(uid)
                fresh = await asyncio.to_thread(client.get_event, uid, cal_hint)
                checked = True
            except Exception:
                fresh = None  # календарь недоступен (сеть/креды) — не сверяем

            if checked:
                if fresh is None:
                    # событие удалено в календаре напрямую — забываем и молчим
                    store.remove(uid)
                    continue
                # событие живо — берём свежие название и время
                new_title = fresh.get("title") or title
                new_start = fresh.get("start")
                if isinstance(new_start, datetime):
                    store.update_start(uid, new_title, new_start)
                    now_local = datetime.now().astimezone()
                    cmp_start = new_start if new_start.tzinfo else new_start.astimezone()
                    if (cmp_start - now_local).total_seconds() > LEAD_SECONDS:
                        # перенесли на потом — сейчас молчим, сработает ближе к делу
                        continue
                    start_iso = new_start.isoformat()
                title = new_title
            # если календарь недоступен — шлём по старым данным (как раньше)

            try:
                start = datetime.fromisoformat(start_iso)
                when = start.strftime("%H:%M")
            except ValueError:
                when = ""
            try:
                await bot.send_message(
                    chat_id, f"⏰ Скоро: <b>{title}</b> в {when}",
                    reply_markup=_ping_kb(uid),
                )
            except Exception:
                pass
            store.mark_reminded(uid)

    async def digest_tick():
        for u in store.users_for_digest():
            try:
                tz = pytz.timezone(u.get("timezone") or config.TIMEZONE)
            except Exception:
                tz = pytz.timezone(config.TIMEZONE)
            local = datetime.now(tz)
            if local.hour != config.DIGEST_HOUR:
                continue
            today = local.strftime("%Y-%m-%d")
            if u.get("last_digest_date") == today:
                continue
            store.mark_digest_sent(u["user_id"], today)  # до отправки: защита от спама при ошибках
            await _send_digest(bot, u)

    if config.TELEGRAM_REMINDERS:
        scheduler.add_job(reminder_tick, "interval", seconds=CHECK_EVERY_SECONDS)
    scheduler.add_job(digest_tick, "interval", seconds=CHECK_EVERY_SECONDS)
    scheduler.start()
    return scheduler
