"""
Компании группы: на чьём бланке оформляется документ.

Шапка бланка и адресат «Кому» берутся отсюда. Компания по умолчанию для
сотрудника — та, что записана в названии его отдела «Отдел (Компания)»:
так справочник сотрудников импортирован из выгрузки контактов.
"""

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.tables import Company, Department, User

#: Логотипы компаний лежат в репозитории рядом с кодом.
LOGO_DIR = Path(__file__).resolve().parent.parent / "assets" / "letterheads"

_DEPARTMENT_COMPANY = re.compile(r"\(([^()]+)\)\s*$")


def as_dict(company: Company | None) -> dict | None:
    """Компания в виде, который принимает `formatting.compose_document`."""
    if company is None:
        return None
    return {
        "id": company.id,
        "name": company.name,
        "addressee": list(company.addressee or []),
        "letterhead_en": list(company.letterhead_en or []),
        "letterhead_ru": list(company.letterhead_ru or []),
        "logo": company.logo,
    }


async def get(session: AsyncSession, company_id: int | None) -> Company | None:
    if company_id is None:
        return None
    return (
        await session.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()


async def active(session: AsyncSession) -> list[Company]:
    rows = await session.execute(
        select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
    )
    return list(rows.scalars().all())


async def default_for(session: AsyncSession, user: User, companies: list[Company]) -> Company | None:
    """
    Компания сотрудника по названию его отдела; не нашлась — первая из
    списка. Пустой список — None: документ оформляется без фирменной шапки.
    """
    if not companies:
        return None
    if user.department_id is not None:
        name = (
            await session.execute(
                select(Department.name).where(Department.id == user.department_id)
            )
        ).scalar_one_or_none()
        match = _DEPARTMENT_COMPANY.search(name or "")
        if match:
            wanted = match.group(1).strip().lower()
            for company in companies:
                if company.short_name.strip().lower() == wanted:
                    return company
    return companies[0]


def logo_path(company: Company) -> Path | None:
    """Файл логотипа или None. Имя из базы не выводит за пределы каталога."""
    if not company.logo:
        return None
    path = (LOGO_DIR / company.logo).resolve()
    if path.parent != LOGO_DIR.resolve() or not path.is_file():
        return None
    return path
