"""iCloud CalDAV, мультипользовательский: у каждого пользователя свой аккаунт.

UserCalDAV — обёртка над одним Apple ID (пароль приложения) с ленивым
подключением и переподключением при сбое. for_user(user_id) достаёт креды
из store, расшифровывает пароль и кэширует инстанс.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, date

import caldav
import pytz
from icalendar import Calendar as ICalendar, Event as IEvent, Alarm

import config
import security
import store
from models import Event

DEFAULT_TZ = pytz.timezone(config.TIMEZONE)


# ---------- общие помощники ----------

def _writable(calendars) -> list:
    """Только календари, принимающие VEVENT (не Reminders/Birthdays)."""
    out = []
    for c in calendars:
        try:
            if "VEVENT" in c.get_supported_components():
                out.append(c)
        except Exception:
            continue
    return out


def test_connection(username: str, password: str) -> list[str]:
    """Проверить креды и вернуть имена календарей для событий. Бросает при ошибке."""
    client = caldav.DAVClient(
        url=config.CALDAV_URL, username=username, password=password, timeout=30,
    )
    return [c.name or "?" for c in _writable(client.principal().calendars())]


def _aware(dt: datetime, tz) -> datetime:
    return tz.localize(dt) if dt.tzinfo is None else dt


def _build_ical(ev: Event, tz) -> tuple[str, bytes]:
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
        start = _aware(ev.start, tz)
        end = _aware(ev.end, tz) if ev.end else start + timedelta(hours=1)
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
    }


def _sort_key(start, tz):
    if isinstance(start, datetime):
        return start.astimezone(pytz.utc)
    if isinstance(start, date):
        return tz.localize(datetime.combine(start, datetime.min.time())).astimezone(pytz.utc)
    return datetime.now(pytz.utc)


# ---------- один пользователь = один аккаунт ----------

class UserCalDAV:
    def __init__(self, username: str, password: str,
                 calendar_name: str | None = None, tz_name: str | None = None):
        self.username = username
        self.password = password
        self.calendar_name = calendar_name
        try:
            self.tz = pytz.timezone(tz_name) if tz_name else DEFAULT_TZ
        except Exception:
            self.tz = DEFAULT_TZ
        self._calendar = None  # кэш выбранного caldav.Calendar

    # --- подключение ---

    def _reset(self):
        self._calendar = None

    def _client(self) -> caldav.DAVClient:
        return caldav.DAVClient(
            url=config.CALDAV_URL,
            username=self.username,
            password=self.password,
            timeout=30,
        )

    def _connect(self) -> "caldav.Calendar":
        if self._calendar is not None:
            return self._calendar
        calendars = _writable(self._client().principal().calendars())
        if not calendars:
            raise RuntimeError("В iCloud не найдено календарей для событий (VEVENT).")
        if self.calendar_name:
            for c in calendars:
                if (c.name or "").strip() == self.calendar_name:
                    self._calendar = c
                    break
            if self._calendar is None:
                names = ", ".join((c.name or "?") for c in calendars)
                raise RuntimeError(
                    f"Календарь '{self.calendar_name}' не найден. Доступны: {names}"
                )
        else:
            self._calendar = calendars[0]
        return self._calendar

    def _retry(self, op):
        """Выполнить op; при сетевом сбое — переподключиться и повторить."""
        try:
            return op()
        except caldav.error.NotFoundError:
            raise
        except Exception:
            self._reset()
            return op()

    # --- публичное API ---

    def list_calendar_names(self) -> list[str]:
        return [c.name or "?" for c in _writable(self._client().principal().calendars())]

    def create_event(self, ev: Event) -> str:
        def op() -> str:
            calendar = self._connect()
            uid, ical = _build_ical(ev, self.tz)
            calendar.save_event(ical.decode())
            return uid
        return self._retry(op)

    def delete_event(self, uid: str) -> bool:
        def op() -> bool:
            calendar = self._connect()
            try:
                obj = calendar.event_by_uid(uid)
            except caldav.error.NotFoundError:
                return False
            obj.delete()
            return True
        return self._retry(op)

    def update_event(self, uid: str, new: Event) -> bool:
        def op() -> bool:
            calendar = self._connect()
            try:
                obj = calendar.event_by_uid(uid)
            except caldav.error.NotFoundError:
                return False
            new.uid = uid
            _, ical = _build_ical(new, self.tz)
            obj.data = ical.decode()
            obj.save()
            return True
        return self._retry(op)

    def get_event(self, uid: str) -> dict | None:
        """Прочитать одно событие целиком (с заметками и напоминаниями)."""
        def op():
            calendar = self._connect()
            try:
                obj = calendar.event_by_uid(uid)
            except caldav.error.NotFoundError:
                return None
            for comp in obj.icalendar_instance.walk("VEVENT"):
                return _parse_component(comp)
            return None
        return self._retry(op)

    def list_events(self, dt_from: datetime, dt_to: datetime) -> list[dict]:
        def op():
            calendar = self._connect()
            return calendar.search(
                start=_aware(dt_from, self.tz),
                end=_aware(dt_to, self.tz),
                event=True,
                expand=True,
            )
        results = self._retry(op)
        out: list[dict] = []
        for r in results:
            for comp in r.icalendar_instance.walk("VEVENT"):
                out.append(_parse_component(comp))
        out.sort(key=lambda e: _sort_key(e["start"], self.tz))
        return out

    def find_by_title(self, title: str, dt_from: datetime, dt_to: datetime) -> list[dict]:
        """Нечёткий поиск по названию (запасной путь для текстовых правок)."""
        needle = title.lower().strip()
        matches = []
        for e in self.list_events(dt_from, dt_to):
            t = e["title"].lower()
            if needle in t or t in needle or _overlap(needle, t):
                matches.append(e)
        return matches


def _overlap(a: str, b: str) -> bool:
    aw, bw = set(a.split()), set(b.split())
    return len(aw & bw) >= 1 and (len(aw & bw) / max(1, min(len(aw), len(bw)))) >= 0.5


# ---------- кэш инстансов ----------

_instances: dict[int, UserCalDAV] = {}


def for_user(user_id: int) -> UserCalDAV:
    """Клиент календаря конкретного пользователя. RuntimeError, если Apple ID не подключён."""
    u = store.get_user(user_id)
    if not u or not u.get("icloud_username"):
        raise RuntimeError("Apple ID не подключён — открой ⚙️ Настройки.")
    inst = _instances.get(user_id)
    if inst and inst.username == u["icloud_username"] and inst.calendar_name == u.get("calendar_name"):
        return inst
    inst = UserCalDAV(
        u["icloud_username"],
        security.decrypt(u["icloud_password"]),
        u.get("calendar_name"),
        u.get("timezone"),
    )
    _instances[user_id] = inst
    return inst


def drop(user_id: int):
    """Сбросить кэш (смена кредов / календаря / пояса / удаление аккаунта)."""
    _instances.pop(user_id, None)
