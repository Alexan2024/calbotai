"""Обёртка над iCloud CalDAV: создание, редактирование, удаление, выборка событий.

iCloud требует Apple ID + пароль приложения (app-specific password).
Подключение ленивое и кэшируется.
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

_calendar = None  # кэш выбранного caldav.Calendar


def _connect() -> "caldav.Calendar":
    global _calendar
    if _calendar is not None:
        return _calendar
    client = caldav.DAVClient(
        url=config.CALDAV_URL,
        username=config.ICLOUD_USERNAME,
        password=config.ICLOUD_PASSWORD,
    )
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise RuntimeError("В iCloud не найдено ни одного календаря.")
    if config.ICLOUD_CALENDAR_NAME:
        for c in calendars:
            if (c.name or "").strip() == config.ICLOUD_CALENDAR_NAME:
                _calendar = c
                break
        if _calendar is None:
            names = ", ".join((c.name or "?") for c in calendars)
            raise RuntimeError(
                f"Календарь '{config.ICLOUD_CALENDAR_NAME}' не найден. Доступны: {names}"
            )
    else:
        chosen = None
        for c in calendars:
            try:
                if "VEVENT" in c.get_supported_components():
                    chosen = c
                    break
            except Exception:
                continue
        _calendar = chosen or calendars[0]
    return _calendar


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


def create_event(ev: Event) -> str:
    """Создать событие. Возвращает UID."""
    calendar = _connect()
    uid, ical = _build_ical(ev)
    calendar.save_event(ical.decode())
    return uid


def delete_event(uid: str) -> bool:
    calendar = _connect()
    try:
        obj = calendar.event_by_uid(uid)
    except caldav.error.NotFoundError:
        return False
    obj.delete()
    return True


def update_event(uid: str, new: Event) -> bool:
    """Полностью переписать событие с данным UID новыми значениями."""
    calendar = _connect()
    try:
        obj = calendar.event_by_uid(uid)
    except caldav.error.NotFoundError:
        return False
    new.uid = uid
    _, ical = _build_ical(new)
    obj.data = ical.decode()
    obj.save()
    return True


def _parse_component(comp) -> dict:
    start = comp.get("dtstart").dt if comp.get("dtstart") else None
    end = comp.get("dtend").dt if comp.get("dtend") else None
    return {
        "uid": str(comp.get("uid")),
        "title": str(comp.get("summary") or "(без названия)"),
        "start": start,
        "end": end,
        "location": str(comp.get("location")) if comp.get("location") else None,
        "all_day": not isinstance(start, datetime),
    }


def list_events(dt_from: datetime, dt_to: datetime) -> list[dict]:
    """События в интервале, отсортированные по началу."""
    calendar = _connect()
    results = calendar.search(
        start=_aware(dt_from),
        end=_aware(dt_to),
        event=True,
        expand=True,
    )
    out: list[dict] = []
    for r in results:
        for comp in r.icalendar_instance.walk("VEVENT"):
            out.append(_parse_component(comp))
    out.sort(key=lambda e: _sort_key(e["start"]))
    return out


def find_by_title(title: str, dt_from: datetime, dt_to: datetime) -> list[dict]:
    """Нечёткий поиск события по названию в интервале (для редактирования)."""
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
