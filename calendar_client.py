"""CalDAV-клиент, мультипользовательский и мультикалендарный.

Поддерживает двух провайдеров через один протокол: Apple iCloud и Google
Calendar. Различие сводится к базовому URL (config.CALDAV_URLS) — оба ходят
по CalDAV с паролем приложения.

UserCalDAV — обёртка над одним аккаунтом (пароль приложения). Кэширует ВСЕ
writable-календари; запись идёт в выбранный (или дефолтный), чтение
(list_events) — по всем календарям сразу, каждый результат помечается полем
"calendar". Операции по UID принимают подсказку calendar_name и при промахе
перебирают остальные календари (event_by_uid ищет только внутри одного).

Особенность Google: подписные/праздничные календари тоже принимают VEVENT,
но read-only — они отфильтровываются по current-user-privilege-set. Для
iCloud эта проверка не выполняется (лишние запросы, поведение не меняем).

Правки (update_event) идут ПО МЕСТУ: меняются только управляемые поля
(название, время, место, заметки, напоминания, RRULE), всё остальное —
участники, организатор, ссылки на видеозвонок, вложения, исключения серии —
сохраняется как есть. Раньше iCal пересобирался с нуля и эти поля молча
уничтожались. exclude_occurrence убирает одно вхождение серии через EXDATE.
"""
from __future__ import annotations
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date

import caldav
import pytz
from caldav.elements.base import ValuedBaseElement
from caldav.lib.namespace import ns
from icalendar import Calendar as ICalendar, Event as IEvent, Alarm, vRecur

import config
import security
import store
from models import Event

DEFAULT_TZ = pytz.timezone(config.TIMEZONE)


# ---------- общие помощники ----------

def _norm_name(name) -> str:
    """Имя календаря без хвостовых пробелов/NBSP — iCloud их сохраняет как есть."""
    return (name or "").replace("\u00a0", " ").strip()


def _url_for(provider: str | None) -> str:
    """Базовый CalDAV-URL по имени провайдера (icloud | google)."""
    key = (provider or config.DEFAULT_PROVIDER).lower()
    return config.CALDAV_URLS.get(key, config.CALDAV_URL)


def _is_google(url: str | None) -> bool:
    return "google" in (url or "").lower()


_PRIV_TAG = ns("D", "current-user-privilege-set")
_WRITE_PRIVILEGES = {"write", "write-content", "write-properties", "bind", "all"}


class _CurrentUserPrivilegeSet(ValuedBaseElement):
    """DAV:current-user-privilege-set (RFC 3744) — в caldav-библиотеке его нет."""
    tag = _PRIV_TAG


def _privilege_names(c) -> set[str]:
    """Локальные имена привилегий календаря. Пустое множество = не удалось узнать.

    Свойство имеет сложный тип, поэтому просим сырой XML (parse_props=False)
    и обходим дерево сами.
    """
    try:
        props = c.get_properties([_CurrentUserPrivilegeSet()], parse_props=False)
    except Exception:
        return set()
    val = props.get(_PRIV_TAG)
    if val is None:
        return set()
    if isinstance(val, str):  # сервер/библиотека вернули текст — грубый разбор
        return {w for w in _WRITE_PRIVILEGES | {"read"} if w in val.lower()}
    names = set()
    try:
        for node in val.iter():
            tag = str(getattr(node, "tag", ""))
            if "}" in tag:
                names.add(tag.split("}", 1)[1].lower())
    except Exception:
        return set()
    return names


def _has_write_privilege(c) -> bool:
    """True, если сервер сообщает право записи (или не сообщает ничего).

    Нужен для Google: праздники/подписки — VEVENT, но read-only. Логика
    консервативная: исключаем календарь только когда привилегии явно
    прочитаны и записи среди них нет; любой сбой = считаем записываемым.
    """
    names = _privilege_names(c)
    if not names:
        return True                       # узнать не вышло — не рискуем исключать
    if names & _WRITE_PRIVILEGES:
        return True
    return "read" not in names            # есть read и нет write → read-only


def _writable(calendars, check_privileges: bool = False) -> list:
    """Только календари, принимающие VEVENT (не Reminders/Birthdays).
    check_privileges=True дополнительно отсекает read-only (Google)."""
    out = []
    for c in calendars:
        try:
            if "VEVENT" not in c.get_supported_components():
                continue
        except Exception:
            continue
        if check_privileges and not _has_write_privilege(c):
            continue
        out.append(c)
    return out


def test_connection(username: str, password: str, url: str | None = None) -> list[str]:
    """Проверить креды и вернуть имена календарей для событий. Бросает при ошибке."""
    base = url or config.CALDAV_URL
    client = caldav.DAVClient(
        url=base, username=username, password=password, timeout=30,
    )
    cals = _writable(client.principal().calendars(),
                     check_privileges=_is_google(base))
    return [_norm_name(c.name) or "?" for c in cals]


def _aware(dt: datetime, tz) -> datetime:
    return tz.localize(dt) if dt.tzinfo is None else dt


def _managed_alarms(ev: Event) -> list[Alarm]:
    """Наши DISPLAY-напоминания из reminders_minutes."""
    out = []
    for minutes in ev.reminders_minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", ev.title)
        alarm.add("trigger", timedelta(minutes=-abs(minutes)))
        out.append(alarm)
    return out


def _master_component(ical):
    """Мастер-VEVENT серии (без RECURRENCE-ID); для обычного события — сам VEVENT.

    В объекте повторяющегося события кроме мастера могут лежать компоненты-
    исключения (перенесённые вхождения) — их трогать нельзя.
    """
    comps = list(ical.walk("VEVENT"))
    if not comps:
        return None
    for c in comps:
        if c.get("RECURRENCE-ID") is None:
            return c
    return comps[0]


def _bump_meta(comp) -> None:
    """SEQUENCE+1 и свежий DTSTAMP — чтобы клиенты подхватили правку."""
    try:
        seq = int(comp.get("SEQUENCE", 0) or 0)
    except (TypeError, ValueError):
        seq = 0
    comp.pop("SEQUENCE", None)
    comp.add("SEQUENCE", seq + 1)
    comp.pop("DTSTAMP", None)
    comp.add("DTSTAMP", datetime.now(pytz.utc))
    comp.pop("LAST-MODIFIED", None)
    comp.add("LAST-MODIFIED", datetime.now(pytz.utc))


def _patch_vevent(comp, ev: Event, tz) -> None:
    """Обновить существующий VEVENT по месту.

    Меняем только управляемые поля; ATTENDEE/ORGANIZER/ссылки на созвон/
    X-поля/вложения не трогаем. DURATION убираем всегда: мы пишем DTEND,
    а по RFC они взаимоисключающие.
    """
    comp.pop("SUMMARY", None)
    comp.add("SUMMARY", ev.title)

    comp.pop("DTSTART", None)
    comp.pop("DTEND", None)
    comp.pop("DURATION", None)
    if ev.all_day:
        d = ev.start.date() if isinstance(ev.start, datetime) else ev.start
        comp.add("DTSTART", d)
        comp.add("DTEND", d + timedelta(days=1))
    else:
        start = _aware(ev.start, tz)
        end = _aware(ev.end, tz) if ev.end else start + timedelta(hours=1)
        comp.add("DTSTART", start)
        comp.add("DTEND", end)

    comp.pop("LOCATION", None)
    if ev.location:
        comp.add("LOCATION", ev.location)
    comp.pop("DESCRIPTION", None)
    if ev.notes:
        comp.add("DESCRIPTION", ev.notes)

    comp.pop("RRULE", None)
    if ev.recurrence:
        try:
            comp.add("RRULE", vRecur.from_ical(ev.recurrence))
        except Exception:
            pass  # кривой RRULE — оставляем событие без повтора

    # наши напоминания заменяем целиком (их же мы и читаем в reminders_minutes)
    comp.subcomponents = [c for c in comp.subcomponents if c.name != "VALARM"]
    for alarm in _managed_alarms(ev):
        comp.add_component(alarm)

    _bump_meta(comp)


def _add_exdate(comp, occ_start, default_tz) -> None:
    """Добавить EXDATE для одного вхождения серии.

    Форма значения подгоняется под DTSTART мастера: для all-day серии — дата,
    для timed — datetime в ТОМ ЖЕ поясе, что DTSTART (одинаковая форма TZID —
    самый совместимый вариант для iCloud/Google).
    """
    dtstart = comp.get("DTSTART")
    ref = getattr(dtstart, "dt", None) if dtstart is not None else None
    value = occ_start
    if ref is not None and not isinstance(ref, datetime):
        value = occ_start.date() if isinstance(occ_start, datetime) else occ_start
    elif isinstance(value, datetime):
        if value.tzinfo is None:
            value = default_tz.localize(value)
        if isinstance(ref, datetime) and ref.tzinfo is not None:
            value = value.astimezone(ref.tzinfo)
    comp.add("EXDATE", value)


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

    for alarm in _managed_alarms(ev):
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
                 calendar_name: str | None = None, tz_name: str | None = None,
                 url: str | None = None):
        self.username = username
        self.password = password
        self.calendar_name = calendar_name  # дефолтный календарь для записи
        self.url = url or config.CALDAV_URL
        try:
            self.tz = pytz.timezone(tz_name) if tz_name else DEFAULT_TZ
        except Exception:
            self.tz = DEFAULT_TZ
        self._cals: dict[str, "caldav.Calendar"] | None = None  # имя -> календарь
        self._cal_urls: dict[str, str] = {}                      # имя -> URL календаря
        self._events_cache: dict = {}                            # ключ окна -> (expiry, события)

    # --- подключение ---

    def _reset(self):
        self._cals = None
        self._cal_urls = {}
        # events-кэш при сетевом reset не трогаем: у него свой TTL, данные ещё валидны.
        # Он чистится только на записи (_invalidate_events).

    def _client(self) -> caldav.DAVClient:
        return caldav.DAVClient(
            url=self.url,
            username=self.username,
            password=self.password,
            timeout=30,
        )

    def _calendars(self) -> dict[str, "caldav.Calendar"]:
        """Все writable-календари одним запросом, с кэшем."""
        if self._cals is not None:
            return self._cals
        cals = _writable(self._client().principal().calendars(),
                         check_privileges=_is_google(self.url))
        if not cals:
            raise RuntimeError("Не найдено календарей для событий (VEVENT).")
        self._cals = {}
        self._cal_urls = {}
        for c in cals:
            nm = _norm_name(c.name) or "?"
            self._cals[nm] = c
            try:
                self._cal_urls[nm] = str(c.url)
            except Exception:
                pass  # без URL просто не пойдём по параллельному пути для этого календаря
        return self._cals

    def _fresh_calendar(self, url: str) -> "caldav.Calendar":
        """Календарь по известному URL на СВЕЖЕМ DAVClient.

        Для параллельного чтения каждому потоку нужен свой клиент: requests.Session
        внутри caldav не потокобезопасна, а principal-discovery мы пропускаем —
        URL уже закэширован (_cal_urls)."""
        return caldav.Calendar(client=self._client(), url=url)

    # --- events-кэш (короткий TTL, чтобы повторные окна отдавались мгновенно) ---

    def _events_key(self, dt_from: datetime, dt_to: datetime,
                    calendar_name: str | None) -> tuple:
        a = _aware(dt_from, self.tz).astimezone(pytz.utc).replace(microsecond=0)
        b = _aware(dt_to, self.tz).astimezone(pytz.utc).replace(microsecond=0)
        return (calendar_name or "*", a.isoformat(), b.isoformat())

    def _cache_get(self, key: tuple):
        ttl = config.EVENTS_CACHE_TTL
        if ttl <= 0:
            return None
        hit = self._events_cache.get(key)
        if not hit:
            return None
        exp, data = hit
        if exp < time.monotonic():
            self._events_cache.pop(key, None)
            return None
        return list(data)  # копия: вызывающий код иногда фильтрует список

    def _cache_put(self, key: tuple, data: list):
        ttl = config.EVENTS_CACHE_TTL
        if ttl <= 0:
            return
        self._events_cache[key] = (time.monotonic() + ttl, list(data))
        if len(self._events_cache) > 64:  # лёгкая уборка протухших ключей
            nowm = time.monotonic()
            for k in [k for k, (e, _) in self._events_cache.items() if e < nowm]:
                self._events_cache.pop(k, None)

    def _invalidate_events(self):
        self._events_cache.clear()

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
            self._reset()
            return list(self._calendars().keys())
        return self._retry(op)

    def create_event(self, ev: Event, calendar_name: str | None = None) -> tuple[str, str]:
        """Создать событие. Возвращает (uid, имя календаря, куда записано)."""
        def op() -> tuple[str, str]:
            name, calendar = self._by_name(calendar_name)
            uid, ical = _build_ical(ev, self.tz)
            calendar.save_event(ical.decode())
            return uid, name
        res = self._retry(op)
        self._invalidate_events()
        return res

    def delete_event(self, uid: str, calendar_name: str | None = None) -> bool:
        def op() -> bool:
            obj, _ = self._find_object(uid, calendar_name)
            if obj is None:
                return False
            obj.delete()
            return True
        res = self._retry(op)
        self._invalidate_events()
        return res

    def update_event(self, uid: str, new: Event, calendar_name: str | None = None) -> bool:
        """Обновить событие ПО МЕСТУ: только управляемые поля, остальное
        (участники, организатор, ссылки на созвон, исключения серии) сохраняется."""
        def op() -> bool:
            obj, _ = self._find_object(uid, calendar_name)
            if obj is None:
                return False
            ical = obj.icalendar_instance
            comp = _master_component(ical)
            if comp is None:
                return False
            new.uid = uid
            _patch_vevent(comp, new, self.tz)
            obj.data = ical.to_ical().decode()
            obj.save()
            return True
        res = self._retry(op)
        self._invalidate_events()
        return res

    def exclude_occurrence(self, uid: str, occ_start: datetime,
                           calendar_name: str | None = None) -> bool:
        """Убрать одно вхождение повторяющейся серии (EXDATE), серию не трогая."""
        def op() -> bool:
            obj, _ = self._find_object(uid, calendar_name)
            if obj is None:
                return False
            ical = obj.icalendar_instance
            comp = _master_component(ical)
            if comp is None:
                return False
            _add_exdate(comp, occ_start, self.tz)
            _bump_meta(comp)
            obj.data = ical.to_ical().decode()
            obj.save()
            return True
        res = self._retry(op)
        self._invalidate_events()
        return res

    def get_event(self, uid: str, calendar_name: str | None = None) -> dict | None:
        """Прочитать одно событие целиком (с заметками, напоминаниями, повтором)."""
        def op():
            obj, name = self._find_object(uid, calendar_name)
            if obj is None:
                return None
            comp = _master_component(obj.icalendar_instance)
            if comp is None:
                return None
            d = _parse_component(comp)
            d["calendar"] = name
            return d
        return self._retry(op)

    def _search_one(self, name: str, c, dt_from: datetime, dt_to: datetime) -> list[dict]:
        """Поиск в одном календаре. Ошибку одного календаря глушим — он не должен
        ронять чтение остальных (поведение как раньше)."""
        try:
            results = c.search(
                start=_aware(dt_from, self.tz),
                end=_aware(dt_to, self.tz),
                event=True,
                expand=True,
            )
        except Exception:
            return []
        found = []
        for r in results:
            for comp in r.icalendar_instance.walk("VEVENT"):
                d = _parse_component(comp)
                d["calendar"] = name
                found.append(d)
        return found

    def list_events(self, dt_from: datetime, dt_to: datetime,
                    calendar_name: str | None = None) -> list[dict]:
        """События за период. По умолчанию — по ВСЕМ календарям, каждое событие
        помечается полем "calendar". calendar_name сужает до одного.

        Одинаковые окна отдаются из TTL-кэша; чтение по нескольким календарям
        распараллеливается (свой DAVClient на поток)."""
        key = self._events_key(dt_from, dt_to, calendar_name)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        def op():
            if calendar_name:
                name, c = self._by_name(calendar_name)
                return self._search_one(name, c, dt_from, dt_to)

            cals = self._calendars()
            names = list(cals.keys())

            if config.CALDAV_PARALLEL and len(names) > 1:
                def worker(name):
                    url = self._cal_urls.get(name)
                    # у каждого потока свой клиент; если URL не закэширован —
                    # запасной путь через общий (последовательный) объект календаря
                    c = self._fresh_calendar(url) if url else cals[name]
                    return self._search_one(name, c, dt_from, dt_to)

                found = []
                with ThreadPoolExecutor(max_workers=min(len(names), 8)) as ex:
                    for part in ex.map(worker, names):
                        found.extend(part)
                return found

            # последовательный путь (один календарь / параллель выключена)
            found = []
            for name in names:
                found.extend(self._search_one(name, cals[name], dt_from, dt_to))
            return found

        out = self._retry(op)
        out.sort(key=lambda e: _sort_key(e["start"], self.tz))
        self._cache_put(key, out)
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
    """Клиент календаря конкретного пользователя. RuntimeError, если аккаунт не подключён."""
    u = store.get_user(user_id)
    if not u or not u.get("icloud_username"):
        raise RuntimeError("Календарь не подключён — открой ⚙️ Настройки.")
    url = _url_for(u.get("provider"))
    inst = _instances.get(user_id)
    if (inst and inst.username == u["icloud_username"]
            and inst.calendar_name == u.get("calendar_name")
            and inst.url == url):
        return inst
    inst = UserCalDAV(
        u["icloud_username"],
        security.decrypt(u["icloud_password"]),
        u.get("calendar_name"),
        u.get("timezone"),
        url,
    )
    _instances[user_id] = inst
    return inst


def drop(user_id: int):
    """Сбросить кэш (смена кредов / календаря / пояса / удаление аккаунта)."""
    _instances.pop(user_id, None)
