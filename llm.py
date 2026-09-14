"""Разбор произвольного ввода (текст/картинка) в структурированное намерение.

Мозги на Claude (Anthropic Messages API). Все модели Claude поддерживают
текст и изображения, поэтому и разбор текста, и распознавание афиш идут здесь.
Аудио Claude не принимает — голос расшифровывается отдельно (см. transcribe.py).

Производительность:
- системный промпт разбит на два блока: статический (кэшируется, cache_control)
  и динамический («текущий момент»). Раньше время было вшито в начало промпта,
  из-за чего префикс менялся каждый вызов и prompt cache не срабатывал никогда.
- «текущий момент» округляется до минуты — иначе кэш промахивался бы на каждом
  запросе даже при разделении блоков.
- parse_when (разбор «завтра в 15:00») гоняется на быстрой модели LLM_MODEL_FAST.

Возвращает JSON:
{
  "intent": "create" | "edit" | "query" | "find_slot" | "bulk" | "chitchat",
  "events": [ {title, category, start, end, all_day, location, notes,
               reminders_minutes, recurrence} ],
  "query": {"from": ISO, "to": ISO} | null,
  "edit": {"match_title", "match_hint": {from, to, part_of_day},
           "op": "modify"|"delete", "changes": {...}} | null,
  "slot": {"title", "duration_minutes", "from", "to"} | null,
  "bulk": {"op", "from", "to", "match_title", "shift_minutes"} | null,
  "reply": "строка или null"
}

Эмодзи в названии НЕ придумывает модель — она только выбирает category из
фиксированного списка, а бот подставляет эмодзи по таблице (см. bot.CATEGORY_EMOJI).
Так одинаковые события всегда получают один и тот же значок.
"""
from __future__ import annotations
import base64
import json
import time
from datetime import datetime

from anthropic import AsyncAnthropic

import config

client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)


# ---------- статический системный промпт (кэшируется) ----------

STATIC_SYSTEM = """Ты — движок разбора для календарного ассистента. По сообщению пользователя
определи намерение и извлеки данные о событии(ях). Пользователь пишет на русском или
английском, в свободной форме: текст, пересланное сообщение, распознанный текст с афиши,
расшифровка голосового.

Текущий момент и часовой пояс пользователя даны отдельным блоком ниже.
Все относительные даты («завтра», «в пятницу», «через час», «сегодня вечером»)
считай от текущего момента и возвращай абсолютными в ISO 8601 с оффсетом пояса.

Верни СТРОГО один JSON-объект без markdown и без пояснений, со схемой:
{
  "intent": "create" | "edit" | "query" | "find_slot" | "bulk" | "chitchat",
  "events": [
    {
      "title": "строка",
      "category": "health|sport|call_online|meeting|call|deadline|birthday|travel|food|event|study|service|other",
      "start": "ISO 8601 с оффсетом",
      "end": "ISO 8601 или null",
      "all_day": true|false,
      "location": "строка или null",
      "notes": "строка или null",
      "reminders_minutes": [числа минут до начала],
      "recurrence": "RRULE-строка или null"
    }
  ],
  "query": {"from": "ISO", "to": "ISO"} | null,
  "edit": {"match_title": "строка или null",
           "match_hint": {"from": "ISO или null", "to": "ISO или null",
                          "part_of_day": "morning|afternoon|evening|night или null"},
           "op": "modify" | "delete",
           "changes": { поля события, а также
                        "shift_minutes": число или null,
                        "duration_minutes": число или null,
                        "part_of_day": "morning|afternoon|evening|night или null" }} | null,
  "slot": {"title": "строка или null", "duration_minutes": число,
           "from": "ISO или null", "to": "ISO или null"} | null,
  "bulk": {"op": "delete" | "shift", "from": "ISO", "to": "ISO",
           "match_title": "строка или null", "shift_minutes": число или null} | null,
  "reply": "строка или null"
}

Правила намерений:
- intent=create — если пользователь описывает событие/встречу/дедлайн/афишу.
- intent=query — если спрашивает про расписание («что у меня завтра», «планы на неделю»).
  Заполни query.from и query.to границами периода; events оставь пустым.
- intent=edit — если просит изменить/перенести/переименовать/удалить ОДНО конкретное
  событие: «перенеси врача на 18», «утренняя встреча перенеслась на вечер»,
  «отмени тренировку», «созвон будет на полчаса позже».
  op=delete — если событие нужно отменить/удалить; иначе op=modify.
  match_title — как пользователь называет событие; можно описательно
  («утренняя встреча»), бот сам сопоставит с реальным названием.
  match_hint — где искать событие: from/to — границы периода, если он назван
  («завтрашний созвон» → завтра; «встреча в четверг» → четверг); part_of_day —
  если событие описано частью дня («утренняя»). Не назван период — оставь null.
  changes — ТОЛЬКО то, что меняется:
  * точное новое время → start (и end, если назван конец);
  * относительный сдвиг («на полчаса позже», «на час раньше») → shift_minutes
    (отрицательное = раньше); start при этом НЕ заполняй;
  * неточная цель («на вечер», «на утро») → changes.part_of_day; start НЕ
    заполняй и точное время НЕ выдумывай;
  * новая длительность («поставь на два часа») → duration_minutes.
- intent=find_slot — если просит найти свободное время («найди час на этой неделе
  для встречи», «когда я свободен завтра на 30 минут»). duration_minutes — длительность
  (по умолчанию 60), from/to — границы поиска (null = ближайшая неделя),
  title — как назвать событие, если понятно из фразы.
- intent=bulk — если просит изменить сразу МНОГО событий: «отмени всё в пятницу»,
  «перенеси все созвоны завтра на час позже». op=delete — удалить, op=shift — сдвинуть
  на shift_minutes минут (может быть отрицательным). from/to — границы периода.
  match_title — фильтр по названию (null = все события периода).
- intent=chitchat — если это не про календарь; дай короткий ответ в reply.

Правило recurrence — заполняй ТОЛЬКО если пользователь явно описал повторение
(«каждый вторник», «ежемесячно», «раз в год», «по будням»):
- Формат — валидная RRULE-строка без префикса "RRULE:", например:
  «каждый вторник» → "FREQ=WEEKLY;BYDAY=TU"
  «по будням в 9» → "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
  «1-го числа каждого месяца» → "FREQ=MONTHLY;BYMONTHDAY=1"
  «раз в год» → "FREQ=YEARLY"
  «каждые 2 недели» → "FREQ=WEEKLY;INTERVAL=2"
- start при этом — первое вхождение события.
- Если повторение не упомянуто — recurrence=null. Дни рождения бот сам делает ежегодными.

Правила названий (title) — это тон-оф-войс, соблюдай строго:
- Естественная короткая фраза, 2–5 слов: «Приём у врача», «Созвон с командой», «Дедлайн по отчёту».
- Sentence case: первая буква заглавная, остальное как в обычном тексте. НЕ капсом.
  Если на афише всё капсом — приведи к нормальному виду.
- Без филлеров: «встреча по поводу обсуждения бюджета» → «Обсуждение бюджета».
- Без точки и лишней пунктуации в конце.
- Язык ввода сохраняй, ничего не переводи.
- НЕ добавляй эмодзи в title — за это отвечает category.
- Добавляй различающую деталь, если она есть во вводе (имя врача, контрагент, группа),
  но НИЧЕГО не выдумывай. Пустое поле лучше выдуманного.

Правило category — выбери одну, ближайшую по смыслу:
- health — врач, анализы, здоровье, стоматолог
- sport — тренировка, зал, пробежка, йога
- call_online — созвон, zoom/meet, онлайн-встреча, вебинар
- meeting — офлайн-встреча, переговоры
- call — телефонный звонок
- deadline — дедлайн, сдать, оплатить, отправить к дате
- birthday — день рождения, годовщина
- travel — перелёт, поезд, поездка, командировка
- food — ресторан, ужин, кафе, обед
- event — концерт, спектакль, выставка, афиша, мероприятие
- study — учёба, лекция, курс, экзамен
- service — услуги: парикмахер, маникюр, сервис, ремонт
- other — если ничего не подходит

Прочее:
- Если время не указано, но есть дата — можно all_day=true.
- Если конца нет — оставь end=null (длительность подставит бот).
- reminders_minutes оставляй пустым, если пользователь явно не просил напоминание.
- Может быть несколько событий — верни их все.
- Если пользователь задаёт вопрос про его планы, занятость и т.д. — анализируй и отвечай.
- Перед сообщением может идти блок «Недавно показанные события» — используй его,
  чтобы понять ссылки («её», «это», «вторую», «перенеси её на час»): в match_title
  подставь настоящее название события из контекста, в match_hint — его дату.
"""

WHEN_SYSTEM = """Ты извлекаешь дату и время из короткой фразы пользователя для переноса
уже существующего события. Текущий момент и часовой пояс даны отдельным блоком ниже.
Относительные даты считай от текущего момента, возвращай абсолютными в ISO 8601 с оффсетом.

Верни СТРОГО один JSON-объект без markdown:
{"start": "ISO 8601", "end": "ISO 8601 или null", "all_day": true|false}

Если названо только время без даты — возьми ближайшую подходящую дату (сегодня, если
время ещё не прошло, иначе завтра). Если названа только дата без времени — all_day=true."""

RESOLVE_SYSTEM = """Ты выбираешь, какое из СУЩЕСТВУЮЩИХ событий календаря пользователь
имеет в виду. Тебе дают его фразу и пронумерованный список реальных событий.
Текущий момент и часовой пояс даны отдельным блоком ниже.

Верни СТРОГО один JSON-объект без markdown:
{"pick": номер или null, "candidates": [номера] или null}

Правила:
- pick — если по смыслу однозначно понятно, какое событие имеется в виду.
  Сопоставляй по смыслу, а не по буквам: «утренняя встреча» = встреча/созвон в
  первой половине дня; «врач» = событие про здоровье; «тренировка» = спорт;
  «ужин с Лёшей» = еда/встреча с этим именем, и т.п.
- candidates — если разумно подходят несколько (2–6 номеров); pick тогда null.
- Если не подходит ни одно — pick=null и candidates=null. Не выбирай наугад.
"""


def _now_block(now: datetime, tz: str) -> dict:
    # округление до минуты: иначе динамический блок менялся бы каждую секунду
    stamp = now.replace(second=0, microsecond=0).isoformat()
    return {
        "type": "text",
        "text": f"Текущий момент: {stamp} (часовой пояс {tz}).",
    }


def _system_blocks(static: str, now: datetime, tz: str, cacheable: bool) -> list[dict]:
    head: dict = {"type": "text", "text": static}
    if cacheable and config.LLM_CACHE:
        head["cache_control"] = {"type": "ephemeral"}
    return [head, _now_block(now, tz)]


def _perf(label: str, t0: float, resp=None):
    if not config.PERF_LOG:
        return
    ms = (time.perf_counter() - t0) * 1000
    extra = ""
    u = getattr(resp, "usage", None)
    if u is not None:
        extra = (f" in={getattr(u, 'input_tokens', '?')}"
                 f" cache_r={getattr(u, 'cache_read_input_tokens', 0)}"
                 f" cache_w={getattr(u, 'cache_creation_input_tokens', 0)}"
                 f" out={getattr(u, 'output_tokens', '?')}")
    print(f"[perf] llm {label}: {ms:.0f} ms{extra}", flush=True)


def _extract_json(resp, prefill: str | None = None) -> dict:
    """Собрать текст из всех text-блоков ответа (пропуская thinking) и распарсить.

    При префилле API возвращает только продолжение, поэтому приклеиваем префикс
    обратно ({...} -> целостный объект)."""
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    if prefill:
        text = prefill + text
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


async def _ask(system_blocks: list[dict], user_content, model: str,
               max_tokens: int, label: str, prefill: str | None = None) -> dict:
    t0 = time.perf_counter()
    if not config.LLM_PREFILL:
        prefill = None  # модель может не поддерживать assistant-prefill
    messages = [{"role": "user", "content": user_content}]
    if prefill:
        # префилл ответа ассистента: модель не «разгоняется», сразу продолжает JSON.
        # Меньше выходных токенов -> меньше latency, и не бывает markdown-обёртки.
        messages.append({"role": "assistant", "content": prefill})
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=messages,
    )
    _perf(label, t0, resp)
    return _extract_json(resp, prefill)


async def parse_text(text: str, now: datetime, tz: str,
                     context: str | None = None) -> dict:
    # короткие сообщения -> быстрая модель; длинные форварды -> основная.
    # LLM_MODEL_FAST по умолчанию == LLM_MODEL, поэтому без явной настройки
    # окружения поведение не меняется.
    # Маршрутизация — по длине САМОГО сообщения: контекст диалога её не раздувает.
    model = (config.LLM_MODEL_FAST
             if len(text) <= config.LLM_FAST_MAXLEN else config.LLM_MODEL)
    payload = f"{context}\n---\nСообщение пользователя: {text}" if context else text
    return await _ask(
        _system_blocks(STATIC_SYSTEM, now, tz, cacheable=True),
        payload, model, config.LLM_MAX_TOKENS, "parse_text", prefill="{",
    )


async def resolve_edit(text: str, candidates: str, now: datetime, tz: str) -> dict:
    """Второй шаг текстовой правки: выбрать событие из списка РЕАЛЬНЫХ.

    Модель видит фразу пользователя и настоящие события календаря, поэтому
    «утренняя встреча» сопоставляется с «Созвон с командой» в 09:00 по смыслу.
    Задача мелкая — быстрая модель; промпт короткий, кэшировать нечего.
    """
    user = f"Фраза пользователя: «{text}»\n\nСобытия:\n{candidates}"
    return await _ask(
        _system_blocks(RESOLVE_SYSTEM, now, tz, cacheable=False),
        user, config.LLM_MODEL_FAST, 400, "resolve_edit", prefill="{",
    )


async def parse_image(image_bytes: bytes, caption: str, now: datetime, tz: str,
                      media_type: str = "image/jpeg") -> dict:
    b64 = base64.b64encode(image_bytes).decode()
    prompt = caption or "Извлеки событие(я) с этого изображения (афиша/скриншот)."
    user_content = [
        {"type": "text", "text": prompt},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        },
    ]
    return await _ask(
        _system_blocks(STATIC_SYSTEM, now, tz, cacheable=True),
        user_content, config.LLM_MODEL, config.LLM_MAX_TOKENS, "parse_image",
        prefill="{",
    )


async def parse_when(text: str, now: datetime, tz: str) -> dict:
    """Разобрать «на завтра в 15:00» → {start, end, all_day} для ручного переноса.

    Задача мелкая — гоняем на быстрой модели (LLM_MODEL_FAST). Промпт короткий,
    кэшировать его смысла нет (ниже минимального размера кэш-блока).
    """
    return await _ask(
        _system_blocks(WHEN_SYSTEM, now, tz, cacheable=False),
        text, config.LLM_MODEL_FAST, 300, "parse_when", prefill="{",
    )
