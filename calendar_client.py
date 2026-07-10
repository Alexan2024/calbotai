"""Обёртка над iCloud CalDAV: создание, редактирование, удаление, выборка событий.

iCloud требует Apple ID + пароль приложения (app-specific password).
Подключение ленивое и кэшируется.

Поддержка нескольких календарей:
- `calendar_names()` — список писабельных (VEVENT) календарей для выбора в боте;
- `create_event(ev, calendar_name)` — запись в конкретный календарь;
- чтение/правки/удаление умеют работать по имени календаря, а при его отсутствии
  ищут событие во всех писабельных календарях (fallback), чтобы ничего не терять.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, date

import caldav
import pytz
from icalendar import Calendar as ICalendar, Event as IEvent, Alarm

import config
from models import Event

TZ = pytz.timezone(config.TIMEZONE)

# --- кэш соединения и календарей ---
_principal = None
_by_name: dict[str, "caldav.Calendar"] = {}
_writable: list["caldav.Calendar"] | None = None


def _reset():
    """Сбросить кэш соединения — при следующем вызове подключимся заново."""
    global _principal, _by_name, _writable
    _principal = None
    _by_name = {}
    _writable = None


def _principal_obj():
    global _principal
    if _principal is None:
        client = caldav.DAVClient(
            url=config.CALDAV_URL,
            username=config.ICLOUD_USERNAME,
            password=config.ICLOUD_PASSWORD,
            timeout=30,
        )
        _principal = client.principal()
    return _principal


def _cal_name(c) -> str:
    return (c.name or "").strip() or "Без имени"


def _load_calendars() -> list["caldav.Calendar"]:
    """Загрузить и закэшировать все календари; выделить писабельные (VEVENT)."""
    global _by_name, _writable
    cals = _principal_obj().calendars()
    if not cals:
        raise RuntimeError("В iCloud не найдено ни одного календаря.")
    _by_name = {}
    writable: list["caldav.Calendar"] = []
    for c in cals:
        _by_name[_cal_name(c)] = c
        try:
            if "VEVENT" in c.get_supported_components():
                writable.append(c)
        except Exception:
            # обнаружение компонентов иногда капризничает — не прячем календарь
            writable.append(c)
    _writable = writable or list(cals)
    return _writable


def _writable_calendars() -> list["caldav.Calendar"]:
    if _writable is None:
        _load_calendars()
    return _writable


def calendar_names() -> list[str]:
    """Имена писабельных календарей (для кнопок выбора в боте)."""
    def op():
        return [_cal_name(c) for c in _writable_calendars()]
    try:
        return op()
    except Exception:
        _reset()
        return op()


def _resolve(calendar_name: str | None):
    """Календарь для записи: по имени, иначе из конфига, иначе первый писабельный."""
    _writable_calendars()  # гарантирует загрузку _by_name/_writable
    if calendar_name:
        c = _by_name.get(calendar_name.strip())
        if c is not None:
            return c
    if config.ICLOUD_CALENDAR_NAME:
        c = _by_name.get(config.ICLOUD_CALENDAR_NAME.strip())
        if c is not None:
            return c
    return _writable_calendars()[0]


def _find_object(uid: str, calendar_name: str | None = None):
    """Найти caldav-объект события по UID.

    Сначала пробуем указанный календарь, затем перебираем остальные писабельные.
    Возвращает (obj, calendar_name) или (None, None).
    """
    _writable_calendars()  # загрузить кэш
    order: list["caldav.Calendar"] = []
    if calendar_name:
        c = _by_name.get(calendar_name.strip())
        if c is not None:
            order.append(c)
    for c in _writable_calendars():
        if c not in order:
            order.append(c)
    for c in order:
        try:
            obj = c.event_by_uid(uid)
            return obj, _cal_name(c)
        except caldav.error.NotFoundError:
            continue
    return None, None


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return TZ.localize(dt)
    return dt


def _build_ical(ev: Event) -> tuple[str, bytes]:
    """Собрать VEVENT (+ VALARM) и вернуть (uid, ical_bytes)."""
    cal = ICalendar()
    cal.add("prodid", "-//tgcalbot//RU")
    cal.add("version", "2.0")

    ie = IEvent()
    uid = ev.uid or f"{uuid.uuid4()}@tgcalbot"
    ie.add("uid", uid)
    ie.add("summary", ev.title)
    ie.add("dtstamp", datetime.now(pytz.utc))

    if ev.all_day:
        d = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        ie.add("dtstart", d)
        ie.add("dtend", (d + timedelta(days=1)))
    else:
        start = _aware(ev.start)
        end = _aware(ev.end) if ev.end else start + timedelta(hours=1)
        ie.add("dtstart", start)
        ie.add("dtend", end)

    if ev.location:
        ie.add("location", ev.location)
    if ev.notes:
        ie.add("description", ev.notes)

    for minutes in ev.reminders_minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", ev.title)
        alarm.add("trigger", timedelta(minutes=-abs(minutes)))
        ie.add_component(alarm)

    cal.add_component(ie)
    return uid, cal.to_ical()


def create_event(ev: Event, calendar_name: str | None = None) -> tuple[str, str]:
    """Создать событие в выбранном (или дефолтном) календаре.

    Возвращает (uid, calendar_name). При сбое — переподключение и повтор.
    """
    def op() -> tuple[str, str]:
        calendar = _resolve(calendar_name)
        uid, ical = _build_ical(ev)
        calendar.save_event(ical.decode())
        return uid, _cal_name(calendar)
    try:
        return op()
    except Exception:
        _reset()
        return op()


def delete_event(uid: str, calendar_name: str | None = None) -> bool:
    def op() -> bool:
        obj, _ = _find_object(uid, calendar_name)
        if obj is None:
            return False
        obj.delete()
        return True
    try:
        return op()
    except caldav.error.NotFoundError:
        return False
    except Exception:
        _reset()
        return op()


def update_event(uid: str, new: Event, calendar_name: str | None = None) -> bool:
    """Полностью переписать событие с данным UID новыми значениями."""
    def op() -> bool:
        obj, _ = _find_object(uid, calendar_name)
        if obj is None:
            return False
        new.uid = uid
        _, ical = _build_ical(new)
        obj.data = ical.decode()
        obj.save()
        return True
    try:
        return op()
    except caldav.error.NotFoundError:
        return False
    except Exception:
        _reset()
        return op()


def _parse_component(comp) -> dict:
    start = comp.get("dtstart").dt if comp.get("dtstart") else None
    end = comp.get("dtend").dt if comp.get("dtend") else None

    reminders: list[int] = []
    for alarm in comp.walk("VALARM"):
        trig = alarm.get("trigger")
        if trig is None:
            continue
        td = getattr(trig, "dt", None)
        if isinstance(td, timedelta):
            reminders.append(int(round(-td.total_seconds() / 60)))

    return {
        "uid": str(comp.get("uid")),
        "title": str(comp.get("summary") or "(без названия)"),
        "start": start,
        "end": end,
        "location": str(comp.get("location")) if comp.get("location") else None,
        "notes": str(comp.get("description")) if comp.get("description") else None,
        "all_day": not isinstance(start, datetime),
        "reminders_minutes": reminders,
        "calendar": None,  # проставляется в list_events / get_event
    }


def get_event(uid: str, calendar_name: str | None = None) -> dict | None:
    """Прочитать одно событие по UID целиком (с заметками и напоминаниями).

    Нужно, чтобы правки (перенос/переименование/напоминания) не затирали
    поля, которых нет в кратком списке из list_events.
    """
    def op():
        obj, cname = _find_object(uid, calendar_name)
        if obj is None:
            return None
        for comp in obj.icalendar_instance.walk("VEVENT"):
            d = _parse_component(comp)
            d["calendar"] = cname
            return d
        return None
    try:
        return op()
    except caldav.error.NotFoundError:
        return None
    except Exception:
        _reset()
        return op()


def list_events(dt_from: datetime, dt_to: datetime) -> list[dict]:
    """События в интервале по всем писабельным календарям, отсортированные по началу."""
    def op():
        out: list[dict] = []
        for calendar in _writable_calendars():
            try:
                results = calendar.search(
                    start=_aware(dt_from),
                    end=_aware(dt_to),
                    event=True,
                    expand=True,
                )
            except Exception:
                continue  # один капризный календарь не должен ронять всю выборку
            cname = _cal_name(calendar)
            for r in results:
                for comp in r.icalendar_instance.walk("VEVENT"):
                    d = _parse_component(comp)
                    d["calendar"] = cname
                    out.append(d)
        return out
    try:
        out = op()
    except Exception:
        _reset()
        out = op()
    out.sort(key=lambda e: _sort_key(e["start"]))
    return out


def find_by_title(title: str, dt_from: datetime, dt_to: datetime) -> list[dict]:
    """Нечёткий поиск события по названию в интервале (для текстовых правок)."""
    needle = title.lower().strip()
    matches = []
    for e in list_events(dt_from, dt_to):
        t = e["title"].lower()
        if needle in t or t in needle or _overlap(needle, t):
            matches.append(e)
    return matches


def _overlap(a: str, b: str) -> bool:
    aw, bw = set(a.split()), set(b.split())
    return len(aw & bw) >= 1 and (len(aw & bw) / max(1, min(len(aw), len(bw)))) >= 0.5


def _sort_key(start):
    if isinstance(start, datetime):
        return start.astimezone(pytz.utc)
    if isinstance(start, date):
        return TZ.localize(datetime.combine(start, datetime.min.time())).astimezone(pytz.utc)
    return datetime.now(pytz.utc)
