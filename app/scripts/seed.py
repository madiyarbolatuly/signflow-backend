"""
Первичное наполнение: администратор, подразделение и три шаблона служебных
записок, требуемых ТЗ (п. 5.1.2).

Идемпотентно: повторный запуск ничего не дублирует, поэтому сценарий
безопасно вызывать при каждом развёртывании.

    python -m app.scripts.seed --admin-email admin@example.com --admin-password ...

Пароль не печатается и не записывается в журнал. Если он не передан, будет
сгенерирован и показан один раз.
"""

import argparse
import asyncio
import secrets

from sqlalchemy import select

from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.session import async_session
from app.db.tables import Department, MemoTemplate, User

TEMPLATES = [
    {
        "code": "vacation",
        "title": "Заявление на отпуск",
        "body_schema": {
            "fields": [
                {"name": "date_from", "label": "Дата начала", "type": "date", "required": True},
                {"name": "date_to", "label": "Дата окончания", "type": "date", "required": True},
                {"name": "kind", "label": "Вид отпуска", "type": "select",
                 "options": ["Ежегодный оплачиваемый", "Без сохранения оплаты"], "required": True},
                {"name": "reason", "label": "Основание", "type": "textarea", "required": False},
            ]
        },
    },
    {
        "code": "training",
        "title": "Направление на обучение",
        "body_schema": {
            "fields": [
                {"name": "program", "label": "Программа обучения", "type": "text", "required": True},
                {"name": "provider", "label": "Организатор", "type": "text", "required": True},
                {"name": "date_from", "label": "Дата начала", "type": "date", "required": True},
                {"name": "date_to", "label": "Дата окончания", "type": "date", "required": True},
                {"name": "cost", "label": "Стоимость, тенге", "type": "number", "required": False},
            ]
        },
    },
    {
        "code": "business_trip",
        "title": "Направление в командировку",
        "body_schema": {
            "fields": [
                {"name": "destination", "label": "Пункт назначения", "type": "text", "required": True},
                {"name": "purpose", "label": "Цель поездки", "type": "textarea", "required": True},
                {"name": "date_from", "label": "Дата выезда", "type": "date", "required": True},
                {"name": "date_to", "label": "Дата возвращения", "type": "date", "required": True},
            ]
        },
    },
]


async def seed(admin_email: str, admin_password: str | None) -> None:
    generated: str | None = None

    async with async_session() as session:
        department = (
            await session.execute(
                select(Department).where(Department.name == "Администрация")
            )
        ).scalar_one_or_none()
        if department is None:
            department = Department(name="Администрация")
            session.add(department)
            await session.flush()
            print("создано подразделение «Администрация»")

        admin = (
            await session.execute(select(User).where(User.email == admin_email))
        ).scalar_one_or_none()
        if admin is None:
            if not admin_password:
                generated = secrets.token_urlsafe(12)
                admin_password = generated
            session.add(
                User(
                    email=admin_email,
                    full_name="Администратор",
                    position="Администратор системы",
                    department_id=department.id,
                    role=UserRole.admin,
                    password_hash=hash_password(admin_password),
                )
            )
            print(f"создан администратор {admin_email}")
        else:
            print(f"администратор {admin_email} уже существует, пропущен")

        for spec in TEMPLATES:
            exists = (
                await session.execute(
                    select(MemoTemplate).where(MemoTemplate.code == spec["code"])
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(MemoTemplate(**spec))
                print(f"создан шаблон «{spec['title']}»")

        await session.commit()

    if generated:
        # Единственный раз, когда пароль выводится: дальше его знает только
        # администратор. Хранить его в файле сценарий не станет.
        print(f"\nПароль администратора: {generated}")
        print("Сохраните его и смените при первом входе.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Первичное наполнение SignFlow")
    parser.add_argument("--admin-email", default="admin@example.com")
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Если не указан, будет сгенерирован и показан один раз",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.admin_email, args.admin_password))


if __name__ == "__main__":
    main()
