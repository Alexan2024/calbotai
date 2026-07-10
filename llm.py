"""Разбор произвольного ввода (текст/картинка) в структурированное намерение.

Возвращает JSON вида:
{
  "intent": "create" | "edit" | "query" | "chitchat",
  "events": [ {title, start, end, all_day, location, notes, reminders_minutes} ],
  "query": {"from": ISO, "to": ISO} | null,
  "edit": {"match_title": str, "changes": {...same event fields...}} | null,
  "reply": "короткий ответ пользователю, если intent=chitchat"
}
Все даты — ISO 8601 с учётом часового пояса пользователя.
"""
from __future__ import annotations
import base64
import json
from datetime import datetime

from openai import AsyncOpenAI

import config

client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)


def _system_prompt(now: datetime, tz: str) -> str:
    return f"""Ты — движок разбора для календарного ассистента. По сообщению пользователя
определи намерение и извлеки данные о событии(ях). Пользователь пишет на русском или
английском, в свободной форме: текст, пересланное сообщение, распознанный текст с афиши,
расшифровка голосового.

Текущий момент: {now.isoformat()} (часовой пояс {tz}).
Все относительные даты («завтра», «в пятницу», «через час», «сегодня вечером»)
считай от текущего момента и возвращай абсолютными в ISO 8601 с оффсетом пояса.

Верни СТРОГО JSON без markdown со схемой:
{{
  "intent": "create" | "edit" | "query" | "chitchat",
  "events": [
    {{
      "title": "строка",
      "start": "ISO 8601 с оффсетом",
      "end": "ISO 8601 или null",
      "all_day": true|false,
      "location": "строка или null",
      "notes": "строка или null",
      "reminders_minutes": [числа минут до начала]
    }}
  ],
  "query": {{"from": "ISO", "to": "ISO"}} | null,
  "edit": {{"match_title": "строка", "changes": {{ поля события }}}} | null,
  "reply": "строка или null"
}}

Правила:
- intent=create — если пользователь описывает событие/встречу/дедлайн/афишу.
- intent=query — если спрашивает про расписание («что у меня завтра», «планы на неделю»).
  Заполни query.from и query.to границами периода; events оставь пустым.
- intent=edit — если просит изменить/перенести/удалить уже существующее событие.
  В edit.match_title — как назвать искомое событие; в edit.changes — новые значения.
- intent=chitchat — если это не про календарь; дай короткий ответ в reply.
- Если время не указано, но есть дата — можно all_day=true.
- Если конца нет — оставь end=null (длительность подставит бот).
- reminders_minutes оставляй пустым, если пользователь явно не просил напоминание;
  значения по умолчанию бот добавит сам.
- Не выдумывай место или детали, которых нет во вводе. Пустое поле лучше выдумки.
- Может быть несколько событий (например, «в пн зал, в ср врач») — верни их все.
"""


async def _chat(messages: list[dict]) -> dict:
    resp = await client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)


async def parse_text(text: str, now: datetime, tz: str) -> dict:
    return await _chat([
        {"role": "system", "content": _system_prompt(now, tz)},
        {"role": "user", "content": text},
    ])


async def parse_image(image_bytes: bytes, caption: str, now: datetime, tz: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "Извлеки событие(я) с этого изображения (афиша/скриншот)."
    user_content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    return await _chat([
        {"role": "system", "content": _system_prompt(now, tz)},
        {"role": "user", "content": user_content},
    ])
