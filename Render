"""Презентационный слой: ASCII/box-drawing оформление ответов бота.

Чистые функции без I/O и CalDAV — на вход список событий (dict-и того же вида,
что отдаёт calendar_client.list_events), на выход готовая к отправке HTML-строка.
Переиспользуются в bot.py (день/неделя/слоты/статистика/конфликты) и notifier.py
(дайджест), отдельно юнит-тестятся.

Правила выравнивания (важно, иначе поедет на чьём-то устройстве):
- Ровная сетка возможна ТОЛЬКО внутри <pre> (моноширинный блок Telegram).
- Эмодзи в <pre> занимают ~2 ячейки по-разному на iOS/Android/Desktop, поэтому
  в выровненных зонах эмодзи НЕТ: только ASCII/box-drawing/блоки + кириллица + цифры.
  Где эмодзи нужны — они в конце строки (перекос не каскадит) или в заголовке над блоком.
- Ширина холста ≤ ~24 символа, иначе горизонтальный скролл на мобиле.
- Дважды экранируем динамический текст (&,<,>) — он идёт внутрь <pre>/<b>.

Гарантированно fixed-width кирпичи: █ ▓ ▒ ░ ▏▎▍▌▐ и box-drawing ─ │ ┃ ━ ┆ └ ┬ ┘.
"""
from __future__ import annotations
import html
import math
import re
from datetime import datetime, date, timedelta

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# заполнители
BUSY_BLOCK = "█"
FREE_BLOCK = "░"
BUSY_SHADE = "▓"
RAIL = "▍"
BAR_TICK = "┃"
BAR_FREE = "┆"


# ---------- общие помощники ----------

def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _pre(body: str) -> str:
    return f"<pre>{body}</pre>"


def _hour_of(dt) -> float:
    return dt.hour + dt.minute / 60.0


def _as_dt(v, tz):
    """date/datetime -> tz-aware datetime (полночь для date)."""
    if isinstance(v, datetime):
        return v if v.tzinfo else tz.localize(v)
    return tz.localize(datetime.combine(v, datetime.min.time()))


def _local(dt, tz):
    return dt.astimezone(tz) if isinstance(dt, datetime) and dt.tzinfo else dt


def _timed(events, tz):
    """Только события со временем (не all_day), приведённые к локальному tz."""
    out = []
    for e in events:
        s = e.get("start")
        if e.get("all_day") or not isinstance(s, datetime):
            continue
        st = _local(_as_dt(s, tz), tz)
        en = e.get("end")
        en = _local(_as_dt(en, tz), tz) if en else st + timedelta(hours=1)
        out.append((st, en, e.get("title") or "(без названия)"))
    return out


def _short_title(title: str, width: int) -> str:
    """Без ведущего эмодзи, обрезка до width — для выровненных зон."""
    t = re.sub(r"^[^\w\d]+", "", title or "").strip() or (title or "")
    if len(t) > width:
        t = t[: width - 1] + "…"
    return t


# ---------- 1. Вертикальный таймлайн дня ----------

def day_timeline(events: list[dict], tz, header: str,
                 max_rows: int = 16) -> str:
    """Вертикальная шкала часов: ┃ занятый час, ┆ свободный, название на старт-часе.

    Возвращает готовый <pre>-блок с заголовком-строкой над сеткой.
    All-day события выносятся строками над сеткой.
    """
    timed = _timed(events, tz)
    all_day = [e.get("title") or "(без названия)"
               for e in events if e.get("all_day")]

    head = _esc(header)
    if not timed and not all_day:
        return _pre(f"{head}\n{'─' * len(header)}\n\nПусто")

    lines = [head, "─" * max(len(header), 18)]
    for t in all_day:
        lines.append(f"     {_esc(_short_title(t, 16))}  · весь день")

    if timed:
        lo = int(min(_hour_of(s) for s, _, _ in timed))
        hi = int(math.ceil(max(_hour_of(en) for _, en, _ in timed)))
        hi = max(hi, lo + 1)
        if hi - lo > max_rows:                 # не раздуваем сообщение
            hi = lo + max_rows

        starts: dict[int, list[str]] = {}
        for s, _, title in timed:
            starts.setdefault(s.hour, []).append(_short_title(title, 18))

        for h in range(lo, hi):
            busy = any(s < tz.localize(datetime(s.year, s.month, s.day, h)) + timedelta(hours=1)
                       and en > tz.localize(datetime(s.year, s.month, s.day, h))
                       for s, en, _ in timed)
            mark = BAR_TICK if busy else BAR_FREE
            label = " · ".join(starts.get(h, []))
            row = f"{h:2d} {mark}"
            if label:
                row += f" {_esc(label)}"
            lines.append(row)
        lines.append("─" * max(len(header), 18))

    return _pre("\n".join(lines))


# ---------- 2. Недельный hour-heatmap ----------

def week_heatmap(events: list[dict], tz, start_day: date,
                 work_start: int, work_end: int) -> str:
    """7 строк (дни) × часы рабочего окна: █ занято, ░ свободно.

    start_day — дата понедельника (или любого дня-начала недели).
    """
    cols = list(range(work_start, work_end))     # [9..20] для 9..21
    ncol = len(cols)
    label_w = 6                                   # "Пн 28 "

    timed = _timed(events, tz)
    # busy[day_index][hour] = bool
    grid = {i: set() for i in range(7)}
    for s, en, _ in timed:
        d0 = start_day
        di = (s.date() - d0).days
        if 0 <= di < 7:
            h = s.hour
            end_h = int(math.ceil(_hour_of(en)))
            for hh in range(h, max(h + 1, end_h)):
                if work_start <= hh < work_end:
                    grid[di].add(hh)

    # шапка с тиками часов каждые 3 часа
    headchars = [" "] * (label_w + ncol)
    for i, h in enumerate(cols):
        if h % 3 == 0:
            for k, ch in enumerate(str(h)):
                pos = label_w + i + k
                if pos < len(headchars):
                    headchars[pos] = ch
    lines = ["".join(headchars).rstrip()]

    for i in range(7):
        d = start_day + timedelta(days=i)
        label = f"{DAYS[d.weekday()].capitalize()} {d.day:2d} "
        cells = "".join(BUSY_BLOCK if h in grid[i] else FREE_BLOCK for h in cols)
        lines.append(f"{label}{cells}")

    return _pre("\n".join(lines))


# ---------- 3. Occupancy-полоса дня (для поиска слота) ----------

def slot_occupancy(events: list[dict], day_start: datetime, tz,
                   work_start: int, work_end: int,
                   free_slots: list[datetime] | None = None) -> str:
    """Одна полоса дня: ▓ занято, ░ свободно, по часам рабочего окна.
    Ниже — времена найденных свободных окон (если переданы).
    """
    busy_hours: set[int] = set()
    day = day_start.astimezone(tz).date()
    for s, en, _ in _timed(events, tz):
        if s.date() != day:
            continue
        end_h = int(math.ceil(_hour_of(en)))
        for hh in range(s.hour, max(s.hour + 1, end_h)):
            if work_start <= hh < work_end:
                busy_hours.add(hh)

    cells = "".join(BUSY_SHADE if h in busy_hours else FREE_BLOCK
                    for h in range(work_start, work_end))
    d = day_start.astimezone(tz)
    title = f"{d.strftime('%d.%m')} ({DAYS[d.weekday()]})  ▓ занято · ░ свободно"
    bar = f"{work_start:02d}│{cells}│{work_end:02d}"
    body = f"{_esc(title)}\n{bar}"
    if free_slots:
        times = " · ".join(s.astimezone(tz).strftime("%H:%M") for s in free_slots[:8])
        body += f"\nсвободно: {times}"
    return _pre(body)


# ---------- 4. Бар-чарт статистики ----------

def stat_bars(agg: dict, category_label: dict, total: int,
              category_emoji: dict | None = None,
              bar_width: int = 8, label_width: int = 9) -> str:
    """agg: category -> [count, hours]. Бары масштабируются к максимуму count.
    Эмодзи (если передан словарь) ставится В КОНЦЕ строки — безопасно для сетки.
    """
    if not agg:
        return _pre("За последние 7 дней событий не было.")
    items = sorted(agg.items(), key=lambda kv: -kv[1][0])
    max_cnt = max(v[0] for _, v in items) or 1

    lines = [f"За 7 дней — событий: {total}", ""]
    for cat, (cnt, hours) in items:
        label = category_label.get(cat, cat)[:label_width]
        fill = max(1, round(cnt / max_cnt * bar_width)) if cnt else 0
        bar = BUSY_BLOCK * fill + " " * (bar_width - fill)
        row = f"{label:<{label_width}}{bar} {cnt}"
        if hours:
            row += f"  {hours:.1f}ч"
        if category_emoji and category_emoji.get(cat):
            row += f" {category_emoji[cat]}"      # эмодзи только в самом конце
        lines.append(row)
    return _pre("\n".join(lines))


# ---------- 5. Мини-диаграмма накладки ----------

def conflict_diagram(a_title: str, a_start: datetime, a_end: datetime,
                     b_title: str, b_start: datetime, b_end: datetime,
                     tz, label_w: int = 7) -> str:
    """Две полосы по 15 мин, сдвиг визуализирует пересечение; ниже — скобка накладки."""
    a_s, a_e = _local(a_start, tz), _local(a_end, tz)
    b_s, b_e = _local(b_start, tz), _local(b_end, tz)
    origin = min(a_s, b_s).replace(minute=0, second=0, microsecond=0)

    def cell(dt):
        return int(round((dt - origin).total_seconds() / 900))

    def bar(s, e):
        lead = max(0, cell(s))
        dur = max(1, cell(e) - cell(s))
        return " " * lead + BUSY_SHADE * dur

    def line(title, s, e):
        lbl = f"{_short_title(title, label_w):<{label_w}}"
        return f"{_esc(lbl)} {s.strftime('%H:%M')} {bar(s, e)}"

    ov_s = max(a_s, b_s)
    ov_e = min(a_e, b_e)
    lines = [line(a_title, a_s, a_e), line(b_title, b_s, b_e)]
    if ov_s < ov_e:
        prefix = label_w + 1 + 5 + 1               # label + ' ' + HH:MM + ' '
        lead = max(0, cell(ov_s))
        width = max(1, cell(ov_e) - cell(ov_s))
        if width >= 2:
            bracket = "└" + "┬" * (width - 2) + "┘"
        else:
            bracket = "┬"
        lines.append(" " * (prefix + lead) + bracket)
        span = f"{ov_s.strftime('%H:%M')}–{ov_e.strftime('%H:%M')}"
        return f"⚠️ Накладка {span}\n" + _pre("\n".join(lines))
    return _pre("\n".join(lines))


# ---------- 6. Левая «рельса» дайджеста ----------

def digest_rail(events: list[dict], tz, header: str) -> str:
    """Построчная рельса ▍ — работает и ВНЕ <pre> (строки независимы, эмодзи можно).

    Заголовок жирным, каждая строка события со столбиком слева.
    """
    lines = [f"<b>{_esc(header)}</b>"]
    if not events:
        lines.append(f"{RAIL} событий нет 🎉")
        return "\n".join(lines)
    for e in events:
        s = e.get("start")
        if isinstance(s, datetime) and not e.get("all_day"):
            when = _local(_as_dt(s, tz), tz).strftime("%H:%M")
        else:
            when = "весь день"
        title = _esc(e.get("title") or "(без названия)")
        line = f"{RAIL} {when} {title}"
        if e.get("location"):
            line += f" · {_esc(e['location'])}"
        lines.append(line)
    return "\n".join(lines)
