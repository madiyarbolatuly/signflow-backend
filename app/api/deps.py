"""Зависимости API: текущий пользователь, разграничение по ролям, клиент."""

from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import http_401, http_403
from app.core.security import decode_access_token
from app.db.enums import UserRole
from app.db.session import get_async_session
from app.db.tables import User

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_prefix}/auth/login", scheme_name="JWT"
)

SessionDep = Annotated[AsyncSession, Depends(get_async_session)]


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: SessionDep,
) -> User:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise http_401("Токен недействителен или истёк") from exc

    user_id = payload.get("sub")
    if not user_id:
        raise http_401("Токен не содержит идентификатора пользователя")

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise http_401("Пользователь не найден")
    if not user.is_active:
        # Уволенный сотрудник с ещё живым токеном не должен подписывать.
        raise http_403("Учётная запись отключена")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def require_admin(user: CurrentUser) -> User:
    if user.role != UserRole.admin:
        raise http_403("Требуются права администратора")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def client_info(request: Request) -> dict:
    """
    Сведения о клиенте для журнала аудита.

    За обратным прокси адрес берётся из `X-Forwarded-For`: иначе в журнал
    попадёт адрес nginx, одинаковый для всех решений, и доказательная
    ценность записи теряется.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )
    return {"ip": ip, "user_agent": request.headers.get("user-agent")}


ClientInfo = Annotated[dict, Depends(client_info)]
