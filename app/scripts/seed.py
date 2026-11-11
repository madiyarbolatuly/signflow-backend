"""
Первичное наполнение: администратор, подразделение и шаблоны заявлений.

Шаблоны повторяют бумажные бланки компании — заявления на приём, на
трудовой отпуск и на увольнение: адресат, текст и состав полей взяты с них.

Идемпотентно: повторный запуск ничего не дублирует, а изменения шаблонов
дописывает в уже существующие, поэтому сценарий безопасно вызывать при
каждом развёртывании.

    python -m app.scripts.seed --admin-email admin@example.com --admin-password ...

Пароль не печатается и не записывается в журнал. Если он не передан, будет
сгенерирован и показан один раз.
"""

import argparse
import asyncio
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.session import async_session
from app.db.tables import Department, MemoTemplate, User

#: Адресат по реквизитам компании: ТОО «AI System Group», БИН 190740026451,
#: генеральный директор Кашабаева Анаргуль Тулешовна (действует на основании
#: Устава). Обращение — как на прежних бланках, «г-ну/г-же Фамилия И. О.».
#: В каждой записке адресата можно поправить — например, для заявления в
#: другое юрлицо группы.
ADDRESSEE = [
    "Генеральному директору",
    "ТОО «AI System Group»",
    "г-же Кашабаевой А. Т.",
]

TEMPLATES = [
    {
        "code": "hiring",
        "title": "Заявление на приём на работу",
        "body_schema": {
            "doc_title": "Заявление",
            "addressee": ADDRESSEE,
            "fields": [
                {"name": "position", "label": "Должность", "type": "text", "required": True},
                {"name": "start_date", "label": "Дата начала работы", "type": "date",
                 "required": True},
                # Поля шапки стоят на бланке под ФИО, а не в тексте заявления.
                {"name": "registration_address", "label": "Адрес регистрации", "type": "text",
                 "required": True, "placement": "header"},
                {"name": "residence_address", "label": "Адрес проживания", "type": "text",
                 "required": True, "placement": "header"},
                {"name": "phone", "label": "Конт. тел", "type": "text",
                 "required": True, "placement": "header"},
            ],
        },
        "text_template": (
            "Прошу Вас принять меня на работу на должность {position} "
            "с {start_date} года."
        ),
    },
    {
        "code": "annual_leave",
        "title": "Заявление на трудовой отпуск",
        "body_schema": {
            "doc_title": "Заявление",
            "addressee": ADDRESSEE,
            "fields": [
                {"name": "date_from", "label": "Дата начала отпуска", "type": "date",
                 "required": True},
                {"name": "days", "label": "Количество календарных дней", "type": "number",
                 "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас предоставить мне очередной трудовой отпуск с {date_from} года "
            "сроком на {days:календарный день|календарных дня|календарных дней}."
        ),
    },
    {
        "code": "dismissal",
        "title": "Заявление на увольнение",
        "body_schema": {
            "doc_title": "Заявление",
            "addressee": ADDRESSEE,
            "fields": [
                {"name": "dismissal_date", "label": "Дата увольнения", "type": "date",
                 "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас расторгнуть со мной трудовой договор по собственному желанию "
            "{dismissal_date} года."
        ),
    },
]

#: Учебные шаблоны первой версии. Не удаляются — на них ссылаются уже
#: созданные записки, — а выключаются: из списка при создании они пропадают.
RETIRED_CODES = ("vacation", "training", "business_trip")


async def apply_templates(session: AsyncSession) -> list[str]:
    """
    Создаёт шаблоны, обновляет существующие и выключает выведенные.

    Возвращает перечень сделанного — для вывода в консоль и для тестов.
    """
    log: list[str] = []

    for spec in TEMPLATES:
        template = (
            await session.execute(
                select(MemoTemplate).where(MemoTemplate.code == spec["code"])
            )
        ).scalar_one_or_none()

        if template is None:
            session.add(MemoTemplate(**spec, is_active=True))
            log.append(f"создан шаблон «{spec['title']}»")
            continue

        changed = [
            key for key in ("title", "body_schema", "text_template")
            if getattr(template, key) != spec[key]
        ]
        if changed or not template.is_active:
            for key in ("title", "body_schema", "text_template"):
                setattr(template, key, spec[key])
            template.is_active = True
            log.append(f"обновлён шаблон «{spec['title']}»")

    retired = (
        await session.execute(
            select(MemoTemplate)
            .where(MemoTemplate.code.in_(RETIRED_CODES))
            .where(MemoTemplate.is_active.is_(True))
        )
    ).scalars().all()
    for template in retired:
        template.is_active = False
        log.append(f"выключен шаблон «{template.title}»")

    await session.flush()
    return log


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

        for line in await apply_templates(session):
            print(line)

        await session.commit()

    if generated:
        # Единственный раз, когда пароль выводится: дальше его знает только
        # администратор. Хранить его в файле сценарий не станет.
        print(f"\nПароль администратора: {generated}")
        print("Сохраните его и смените при первом входе.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Первичное наполнение УчетЭДО")
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
