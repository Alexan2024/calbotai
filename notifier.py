"""Опциональные напоминания в Telegram перед началом события.

Нативные напоминания Apple ставятся через VALARM в самом событии.
Этот модуль — дополнительный пинг прямо в чат бота (как у Dola).
"""
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import store

CHECK_EVERY_SECONDS = 60
LEAD_SECONDS = 10 * 60  # напомнить за 10 минут до начала


def setup(bot, loop):
    scheduler = AsyncIOScheduler(event_loop=loop)

    async def tick():
        for uid, chat_id, title, start_iso in store.due_for_reminder(LEAD_SECONDS):
            try:
                start = datetime.fromisoformat(start_iso)
                when = start.strftime("%H:%M")
            except ValueError:
                when = ""
            await bot.send_message(chat_id, f"⏰ Скоро: <b>{title}</b> в {when}")
            store.mark_reminded(uid)

    scheduler.add_job(tick, "interval", seconds=CHECK_EVERY_SECONDS)
    scheduler.start()
    return scheduler
