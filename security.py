"""Шифрование паролей приложений iCloud перед записью в SQLite (Fernet).

Ключ берётся из SECRET_KEY. Без ключа пароли хранятся как есть — бот
работает, но при первом запуске в лог пишется предупреждение.

Сгенерировать ключ:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
from cryptography.fernet import Fernet, InvalidToken

import config

_fernet = Fernet(config.SECRET_KEY.encode()) if config.SECRET_KEY else None

if _fernet is None:
    print("WARNING: SECRET_KEY не задан — пароли приложений хранятся в SQLite без шифрования.")


def encrypt(plain: str) -> str:
    if not plain:
        return plain
    if _fernet is None:
        return plain
    return _fernet.encrypt(plain.encode()).decode()


def decrypt(stored: str) -> str:
    if not stored:
        return stored
    if _fernet is None:
        return stored
    try:
        return _fernet.decrypt(stored.encode()).decode()
    except InvalidToken:
        # запись сделана до включения SECRET_KEY — считаем её открытым текстом
        return stored
