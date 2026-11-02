"""Администрирование: сотрудники, подразделения, шаблоны служебных записок."""

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, SessionDep
from app.core.exceptions import http_409
from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.tables import Department, MemoTemplate, User
from app.schemas import (
    DepartmentIn,
    DepartmentOut,
    TemplateIn,
    TemplateOut,
    UserIn,
    UserOut,
)

router = APIRouter(tags=["Администрирование"])


# ─── шаблоны ─────────────────────────────────────────────────────────────────
@router.get("/templates", response_model=list[TemplateOut])
async def list_templates(session: SessionDep, user: CurrentUser) -> list[MemoTemplate]:
    """Доступно всем: автор выбирает шаблон при создании записки."""
    rows = await session.execute(
        select(MemoTemplate).where(MemoTemplate.is_active.is_(True)).order_by(MemoTemplate.id)
    )
    return list(rows.scalars().all())


@router.post("/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(
    data: TemplateIn, session: SessionDep, admin: AdminUser
) -> MemoTemplate:
    exists = (
        await session.execute(select(MemoTemplate).where(MemoTemplate.code == data.code))
    ).scalar_one_or_none()
    if exists is not None:
        raise http_409(f"Шаблон с кодом «{data.code}» уже существует")

    template = MemoTemplate(**data.model_dump())
    session.add(template)
    await session.flush()
    return template


# ─── подразделения ───────────────────────────────────────────────────────────
@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(session: SessionDep, user: CurrentUser) -> list[Department]:
    rows = await session.execute(select(Department).order_by(Department.name))
    return list(rows.scalars().all())


@router.post("/departments", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    data: DepartmentIn, session: SessionDep, admin: AdminUser
) -> Department:
    exists = (
        await session.execute(select(Department).where(Department.name == data.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise http_409(f"Подразделение «{data.name}» уже существует")

    department = Department(**data.model_dump())
    session.add(department)
    await session.flush()
    return department


# ─── сотрудники ──────────────────────────────────────────────────────────────
@router.get("/users", response_model=list[UserOut])
async def list_users(session: SessionDep, user: CurrentUser) -> list[User]:
    """
    Доступно всем: справочник нужен автору, чтобы выбрать согласующих при
    построении маршрута. Пароли и признак активности в схему ответа не входят.
    """
    rows = await session.execute(
        select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    )
    return list(rows.scalars().all())


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(data: UserIn, session: SessionDep, admin: AdminUser) -> User:
    if data.role not in UserRole.ALL:
        raise http_409(f"Неизвестная роль: {data.role}")

    email = data.email.lower()
    exists = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if exists is not None:
        raise http_409(f"Сотрудник с адресом {email} уже заведён")

    user = User(
        email=email,
        full_name=data.full_name,
        position=data.position,
        department_id=data.department_id,
        role=data.role,
        # Пароль необязателен: сотруднику, который только подписывает по
        # ссылке, учётная запись для входа не нужна.
        password_hash=hash_password(data.password) if data.password else None,
    )
    session.add(user)
    await session.flush()
    return user
