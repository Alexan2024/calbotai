"""Показать календари провайдера и какие компоненты они принимают.

Одноразовый диагностический скрипт. Запуск:
    python list_calendars.py                 # iCloud, креды из ICLOUD_* в окружении
    python list_calendars.py google          # Google, креды спросит интерактивно
    python list_calendars.py google me@gmail.com
(на Railway:  railway run python list_calendars.py)

Ищи строку, где в components есть VEVENT и есть право записи — это календарь
для событий. Его имя можно выбрать в боте: ⚙️ Настройки → Календарь.

Для Google полезно проверить именно этот вывод перед подключением: подписные
и праздничные календари тоже отдают VEVENT, но доступны только на чтение.
"""
import getpass
import sys

import caldav

import calendar_client
import config

provider = (sys.argv[1] if len(sys.argv) > 1 else "icloud").lower()
if provider not in config.CALDAV_URLS:
    print(f"Неизвестный провайдер: {provider}. Доступны: {', '.join(config.CALDAV_URLS)}")
    raise SystemExit(1)

url = config.CALDAV_URLS[provider]

if len(sys.argv) > 2:
    username = sys.argv[2]
elif provider == "icloud" and config.ICLOUD_USERNAME:
    username = config.ICLOUD_USERNAME
else:
    username = input("Email аккаунта: ").strip()

if provider == "icloud" and config.ICLOUD_PASSWORD and len(sys.argv) <= 2:
    password = config.ICLOUD_PASSWORD
else:
    password = getpass.getpass("Пароль приложения: ").replace(" ", "")

client = caldav.DAVClient(url=url, username=username, password=password, timeout=30)

print(f"Подключаюсь к {provider} ({url})...")
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
        privs = calendar_client._privilege_names(c)
        writable = "VEVENT ✅" if "VEVENT" in comps else "не для событий ❌"
        if not privs:
            can_write = "права неизвестны ❔"
        elif calendar_client._has_write_privilege(c):
            can_write = "запись ✅"
        else:
            can_write = "read-only ⚠️"
        print(f"- {c.name!r:35} {writable}  {can_write}  (components={comps})")

    print("\nПодходят календари со знаками ✅ ✅. Выбрать нужный можно в боте: "
          "⚙️ Настройки → Календарь.")
