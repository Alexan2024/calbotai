"""iCloud CalDAV, мультипользовательский и мультикалендарный.

UserCalDAV — обёртка над одним Apple ID (пароль приложения). Кэширует ВСЕ
writable-календари; запись идёт в выбранный (или дефолтный), чтение
(list_events) — по всем календарям сразу, каждый результат помечается полем
"calendar". Операции по UID принимают подсказку calendar_name и при промахе
перебирают остальные календари (iCloud ищет event_by_uid только внутри одного).

Производительность (спринт 2):
- ПАРАЛЛЕЛЬНОЕ чтение: календари опрашиваются в пуле потоков, а не по очереди.
  requests.Session внутри caldav НЕ потокобезопасна, поэтому у каждого календаря
  свой DAVClient (своя сессия) — гоняем их из разных потоков без гонок.
- TTL-КЭШ выборок (CACHE_TTL): навигация ◀️/▶️ по дням, повторные открытия
  недели/месяца и проверка конфликтов больше не бьют по iCloud каждый раз.
  Любая запись (create/update/delete) сбрасывает кэш пользователя.
- Кэш списка календарей (CAL_LIST_TTL): раньше list_calendar_names() форсил
  полный реквери принципала при каждом открытии пикера.
- Таймаут запроса снижен (CALDAV_TIMEOUT): 30 с + ретрай = минута ожидания.
"""
from __future__ import annotations
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date

import caldav
import pytz
from icalendar import Calendar as ICalendar, Event as IEvent, Alarm, vRecur

import config
import security
import store
from models import Event

DEFAULT_TZ = pytz.timezone(config.TIMEZONE)

# общий пул для параллельного опроса календарей
_POOL = ThreadPoolExecutor(
    max_workers=max(2, config.CALDAV_WORKERS), thread_name_prefix="caldav"
)


# ---------- общие помощники ----------

def _perf(label: str, t0: float):
    if config.PERF_LOG:
        print(f"[perf] caldav {label}: {(time.perf_counter() - t0) * 1000:.0f} ms",
              flush=True)


def _norm_name(name) -> str:
    """Имя календаря без хвостовых пробелов/NBSP — iCloud их сохраняет как есть."""
    return (name or "").replace("\u00a0", " ").strip()


def _make_client(username: str, password: str) -> caldav.DAVClient:
    return caldav.DAVClient(
        url=config.CALDAV_URL,
        username=username,
        password=password,
        timeout=config.CALDAV_TIMEOUT,
    )


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
    client = _make_client(username, password)
    return [_norm_name(c.name) or "?" for c in _writable(client.principal().calendars())]


def _aware(dt: datetime, tz) -> datetime:
    return tz.localize(dt) if dt.tzinfo is None else dt


def _build_ical(ev: Event, tz) -> tuple[str, bytes]:
    """Собрать VEVENT (+ VALARM, + RRULE) и вернуть (uid, ical_bytes)."""
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

    if ev.recurrence:
        try:
            ie.add("rrule", vRecur.from_ical(ev.recurrence))
        except Exception:
            pass  # кривой RRULE от LLM — событие создаём без повтора

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

    recurrence = None
    rr = comp.get("rrule")
    if rr is not None:
        try:
            recurrence = rr.to_ical().decode()
        except Exception:
            recurrence = None

    return {
        "uid": str(comp.get("uid")),
        "title": str(comp.get("summary") or "(без названия)"),
        "start": start,
        "end": end,
        "location": str(comp.get("location")) if comp.get("location") else None,
        "notes": str(comp.get("description")) if comp.get("description") else None,
        "all_day": not isinstance(start, datetime),
        "reminders_minutes": reminders,
        "recurrence": recurrence,
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
        self.calendar_name = calendar_name  # дефолтный календарь для записи
        try:
            self.tz = pytz.timezone(tz_name) if tz_name else DEFAULT_TZ
        except Exception:
            self.tz = DEFAULT_TZ
        self._cals: dict[str, "caldav.Calendar"] | None = None  # имя -> календарь
        self._cals_at: float = 0.0
        self._cache: dict[tuple, tuple[float, list[dict]]] = {}  # ключ -> (expires, events)

    # --- кэш выборок ---

    def _cache_get(self, key: tuple) -> list[dict] | None:
        if config.CACHE_TTL <= 0:
            return None
        item = self._cache.get(key)
        if not item:
            return None
        expires, data = item
        if time.time() > expires:
            self._cache.pop(key, None)
            return None
        return data

    def _cache_put(self, key: tuple, data: list[dict]):
        if config.CACHE_TTL <= 0:
            return
        if len(self._cache) > 64:  # простая защита от разрастания
            self._cache.clear()
        self._cache[key] = (time.time() + config.CACHE_TTL, data)

    def invalidate(self):
        """Сбросить кэш выборок (после любой записи в календарь)."""
        self._cache.clear()

    # --- подключение ---

    def _reset(self):
        self._cals = None
        self._cals_at = 0.0
        self._cache.clear()

    def _client(self) -> caldav.DAVClient:
        return _make_client(self.username, self.password)

    def _calendars(self) -> dict[str, "caldav.Calendar"]:
        """Все writable-календари, с кэшем на CAL_LIST_TTL.

        Каждому календарю выдаём СВОЙ DAVClient — так объекты можно безопасно
        дёргать из разных потоков (requests.Session не потокобезопасна).
        """
        if self._cals is not None and (time.time() - self._cals_at) < config.CAL_LIST_TTL:
            return self._cals
        t0 = time.perf_counter()
        base = self._client()
        cals = _writable(base.principal().calendars())
        if not cals:
            raise RuntimeError("В iCloud не найдено календарей для событий (VEVENT).")
        out: dict[str, "caldav.Calendar"] = {}
        for c in cals:
            name = _norm_name(c.name) or "?"
            try:
                out[name] = caldav.Calendar(
                    client=self._client(),      # отдельная сессия на календарь
                    url=c.url,
                    name=c.name,
                    id=getattr(c, "id", None),
                )
            except Exception:
                out[name] = c  # на всякий случай — исходный объект
        self._cals = out
        self._cals_at = time.time()
        _perf(f"discover {len(out)} calendars", t0)
        return out

    def _default(self) -> tuple[str, "caldav.Calendar"]:
        cals = self._calendars()
        if self.calendar_name:
            want = _norm_name(self.calendar_name)
            for n, c in cals.items():                 # точное совпадение
                if n == want:
                    return n, c
            for n, c in cals.items():                 # без учёта регистра
                if n.casefold() == want.casefold():
                    return n, c
            raise RuntimeError(
                f"Календарь '{self.calendar_name}' не найден. Доступны: {', '.join(cals)}"
            )
        name = next(iter(cals))
        return name, cals[name]

    def _by_name(self, name: str | None) -> tuple[str, "caldav.Calendar"]:
        """Календарь по имени; без имени / при промахе — дефолтный."""
        if not name:
            return self._default()
        cals = self._calendars()
        want = _norm_name(name)
        if want in cals:
            return want, cals[want]
        for n, c in cals.items():
            if n.casefold() == want.casefold():
                return n, c
        return self._default()

    def _retry(self, op):
        """Выполнить op; при сетевом сбое — переподключиться и повторить."""
        try:
            return op()
        except caldav.error.NotFoundError:
            raise
        except Exception:
            self._reset()
            return op()

    def _find_object(self, uid: str, calendar_name: str | None):
        """Найти объект события: сперва в подсказанном календаре, затем во всех."""
        tried = set()
        if calendar_name:
            name, c = self._by_name(calendar_name)
            tried.add(name)
            try:
                return c.event_by_uid(uid), name
            except caldav.error.NotFoundError:
                pass
        for name, c in self._calendars().items():
            if name in tried:
                continue
            try:
                return c.event_by_uid(uid), name
            except caldav.error.NotFoundError:
                continue
        return None, None

    # --- публичное API ---

    def list_calendar_names(self) -> list[str]:
        def op():
            return list(self._calendars().keys())
        return self._retry(op)

    def create_event(self, ev: Event, calendar_name: str | None = None) -> tuple[str, str]:
        """Создать событие. Возвращает (uid, имя календаря, куда записано)."""
        t0 = time.perf_counter()

        def op() -> tuple[str, str]:
            name, calendar = self._by_name(calendar_name)
            uid, ical = _build_ical(ev, self.tz)
            calendar.save_event(ical.decode())
            return uid, name

        res = self._retry(op)
        self.invalidate()
        _perf("create_event", t0)
        return res

    def delete_event(self, uid: str, calendar_name: str | None = None) -> bool:
        t0 = time.perf_counter()

        def op() -> bool:
            obj, _ = self._find_object(uid, calendar_name)
            if obj is None:
                return False
            obj.delete()
            return True

        res = self._retry(op)
        self.invalidate()
        _perf("delete_event", t0)
        return res

    def update_event(self, uid: str, new: Event, calendar_name: str | None = None) -> bool:
        t0 = time.perf_counter()

        def op() -> bool:
            obj, _ = self._find_object(uid, calendar_name)
            if obj is None:
                return False
            new.uid = uid
            _, ical = _build_ical(new, self.tz)
            obj.data = ical.decode()
            obj.save()
            return True

        res = self._retry(op)
        self.invalidate()
        _perf("update_event", t0)
        return res

    def get_event(self, uid: str, calendar_name: str | None = None) -> dict | None:
        """Прочитать одно событие целиком (с заметками, напоминаниями, повтором)."""
        t0 = time.perf_counter()

        def op():
            obj, name = self._find_object(uid, calendar_name)
            if obj is None:
                return None
            for comp in obj.icalendar_instance.walk("VEVENT"):
                d = _parse_component(comp)
                d["calendar"] = name
                return d
            return None

        res = self._retry(op)
        _perf("get_event", t0)
        return res

    def _search_one(self, calendar, dt_from: datetime, dt_to: datetime) -> list[dict]:
        """Опросить ОДИН календарь. Выполняется в отдельном потоке пула."""
        try:
            results = calendar.search(
                start=_aware(dt_from, self.tz),
                end=_aware(dt_to, self.tz),
                event=True,
                expand=True,
            )
        except Exception:
            return []  # один капризный календарь не должен ронять всё
        found = []
        for r in results:
            try:
                for comp in r.icalendar_instance.walk("VEVENT"):
                    found.append(_parse_component(comp))
            except Exception:
                continue
        return found

    def list_events(self, dt_from: datetime, dt_to: datetime,
                    calendar_name: str | None = None,
                    use_cache: bool = True) -> list[dict]:
        """События за период. По умолчанию — по ВСЕМ календарям (параллельно),
        каждое событие помечается полем "calendar". calendar_name сужает до одного.

        Результат кэшируется на CACHE_TTL секунд; любая запись сбрасывает кэш.
        """
        t0 = time.perf_counter()
        key = (dt_from.isoformat(), dt_to.isoformat(), calendar_name or "*")
        if use_cache:
            hit = self._cache_get(key)
            if hit is not None:
                _perf("list_events (cache hit)", t0)
                return hit

        def op():
            if calendar_name:
                name, c = self._by_name(calendar_name)
                targets = [(name, c)]
            else:
                targets = list(self._calendars().items())
            futures = [(name, _POOL.submit(self._search_one, c, dt_from, dt_to))
                       for name, c in targets]
            found: list[dict] = []
            for name, fut in futures:
                for d in fut.result():
                    d["calendar"] = name
                    found.append(d)
            return found

        out = self._retry(op)
        out.sort(key=lambda e: _sort_key(e["start"], self.tz))
        self._cache_put(key, out)
        _perf(f"list_events ({len(out)} ev)", t0)
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
    if (inst
            and inst.username == u["icloud_username"]
            and inst.calendar_name == u.get("calendar_name")):
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
