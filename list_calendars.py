"""Показать календари iCloud и какие компоненты они принимают.

Запусти один раз, чтобы узнать точные имена календарей:
    python list_calendars.py
(на Railway:  railway run python list_calendars.py)

Ищи строку, где в components есть VEVENT — это календарь для событий.
Его имя впиши в переменную ICLOUD_CALENDAR_NAME.
"""
import caldav
import config

client = caldav.DAVClient(
    url=config.CALDAV_URL,
    username=config.ICLOUD_USERNAME,
    password=config.ICLOUD_PASSWORD,
)

print("Подключаюсь к iCloud...")
principal = client.principal()
calendars = principal.calendars()

if not calendars:
    print("Календарей не найдено.")
else:
    print(f"Найдено календарей: {len(calendars)}\n")
    for c in calendars:
        try:
            comps = c.get_supported_components()
        except Exception:
            comps = ["?"]
        writable = "события ✅" if "VEVENT" in comps else "НЕ для событий ❌"
        print(f"- {c.name!r:35} {writable}  (components={comps})")

    print("\nВпиши имя календаря со знаком ✅ в ICLOUD_CALENDAR_NAME "
          "(ровно как показано, без кавычек).")
