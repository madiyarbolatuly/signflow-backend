"""Вход сотрудника и сведения о текущем пользователе."""

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.core.exceptions import http_401
from app.core.security import create_access_token, verify_password
from app.db.tables import User
from app.schemas import LoginRequest, TokenResponse, UserOut

router = APIRouter(tags=["Авторизация"])


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, session: SessionDep) -> TokenResponse:
    user = (
        await session.execute(
            select(User).where(User.email == data.email.lower())
        )
    ).scalar_one_or_none()

    # Одно и то же сообщение для неизвестной почты и неверного пароля: иначе
    # форма входа превращается в средство проверки, кто работает в компании.
    if user is None or not user.password_hash:
        raise http_401("Неверный адрес почты или пароль")
    if not verify_password(data.password, user.password_hash):
        raise http_401("Неверный адрес почты или пароль")
    if not user.is_active:
        raise http_401("Учётная запись отключена")

    return TokenResponse(
        access_token=create_access_token(
            user_id=user.id, email=user.email, role=user.role
        )
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user
