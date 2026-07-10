"""Разбор произвольного ввода (текст/картинка) в структурированное намерение.

Мозги на Claude (Anthropic Messages API). Все модели Claude поддерживают
текст и изображения, поэтому и разбор текста, и распознавание афиш идут здесь.
Аудио Claude не принимает — голос расшифровывается отдельно (см. transcribe.py).

Возвращает JSON:
{
  "intent": "create" | "edit" | "query" | "chitchat",
  "events": [ {title, start, end, all_day, location, notes, reminders_minutes} ],
  "query": {"from": ISO, "to": ISO} | null,
  "edit": {"match_title": str, "changes": {...}} | null,
  "reply": "строка или null"
}
"""
from __future__ import annotations
import base64
import json
from datetime import datetime

from anthropic import AsyncAnthropic

import config

client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


def _system_prompt(now: datetime, tz: str) -> str:
    return f"""Ты — движок разбора для календарного ассистента. По сообщению пользователя
определи намерение и извлеки данные о событии(ях). Пользователь пишет на русском или
английском, в свободной форме: текст, пересланное сообщение, распознанный текст с афиши,
расшифровка голосового.

Текущий момент: {now.isoformat()} (часовой пояс {tz}).
Все относительные даты («завтра», «в пятницу», «через час», «сегодня вечером»)
считай от текущего момента и возвращай абсолютными в ISO 8601 с оффсетом пояса.

Верни СТРОГО один JSON-объект без markdown и без пояснений, со схемой:
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
- reminders_minutes оставляй пустым, если пользователь явно не просил напоминание.
- Не выдумывай место или детали, которых нет во вводе. Пустое поле лучше выдумки.
- Может быть несколько событий — верни их все.
"""


def _extract_json(resp) -> dict:
    """Собрать текст из всех text-блоков ответа (пропуская thinking) и распарсить."""
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    # взять от первой { до последней }
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    return json.loads(text)


async def _ask(system: str, user_content) -> dict:
    resp = await client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_json(resp)


async def parse_text(text: str, now: datetime, tz: str) -> dict:
    return await _ask(_system_prompt(now, tz), text)


async def parse_image(image_bytes: bytes, caption: str, now: datetime, tz: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "Извлеки событие(я) с этого изображения (афиша/скриншот)."
    user_content = [
        {"type": "text", "text": prompt},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        },
    ]
    return await _ask(_system_prompt(now, tz), user_content)
