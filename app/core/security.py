"""
Пароли, токены доступа и одноразовые токены подписания.

Хеширование вызывает bcrypt напрямую. В EDMS между кодом и bcrypt стоял
passlib, который определял версию bcrypt через приватный `bcrypt.__about__` —
атрибут, удалённый в bcrypt 4.1, из-за чего вход и регистрация падали с
«password cannot be longer than 72 bytes», а requirements приходилось держать
на `bcrypt<4.1`. Прямой вызов снимает и проблему, и пин.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings

#: bcrypt усекает вход до 72 байт и на более длинном пароле бросает ошибку.
#: Отрезаем сами и осознанно, а не ждём исключения из библиотеки.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    try:
        return bcrypt.checkpw(raw, hashed.encode("ascii"))
    except ValueError:
        # Хеш в базе повреждён или имеет чужой формат. Это отказ в доступе,
        # а не ошибка сервера.
        return False


def create_access_token(*, user_id: str, email: str, role: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_min
    )
    payload = {"sub": user_id, "email": email, "role": role, "exp": expires}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.algorithm)


def decode_access_token(token: str) -> dict:
    """Возвращает полезную нагрузку токена либо бросает `jwt.PyJWTError`."""
    return jwt.decode(
        token, settings.jwt_secret_key, algorithms=[settings.algorithm]
    )


def new_sign_token() -> str:
    """
    Одноразовый токен ссылки подписания.

    32 байта из `secrets` — криптографически стойкий генератор, как требует
    ТЗ, п. 5.3. Длина в символах фиксирована (43), что важно: колонка
    объявлена как String(64) с уникальным индексом.
    """
    return secrets.token_urlsafe(32)


def file_digest(data: bytes) -> str:
    """
    SHA-256 подписываемого файла.

    Фиксируется в момент каждого решения: именно он доказывает, что подписан
    был тот документ, который сейчас лежит в хранилище (ТЗ, п. 5.1.6).
    """
    return hashlib.sha256(data).hexdigest()
