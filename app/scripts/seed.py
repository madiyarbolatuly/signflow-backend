"""
Первичное наполнение: администратор, подразделение, компании группы и
шаблоны заявлений.

Шаблоны повторяют бумажные бланки компании — заявления на приём, отпуск,
увольнение, аванс, удержание и другие: текст и состав полей взяты с них.
Шапка бланка и адресат берутся из выбранной компании.

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
from app.db.tables import Company, Department, MemoTemplate, User

#: Юрлица группы. Отсюда берутся фирменная шапка бланка и адресат «Кому»:
#: автор выбирает компанию в редакторе. Адресат — как на бланках компании,
#: без обращения: инициал перед фамилией в дательном падеже («Ж. Құсайын»,
#: «А. Жусуповой»).
#:
#: Новая компания — ещё одна запись здесь и логотип в
#: `app/assets/letterheads/`. `short_name` должен совпадать с тем, как
#: компания записана в названии отдела «Отдел (Компания)» (без учёта
#: пробелов и дефисов): по нему сотруднику предлагается его компания.
#:
#: Шапки — из колонтитулов присланных бланков, строка в строку: первая
#: строка (наименование) печатается полужирным, остальные — обычным.
COMPANIES = [
    {
        "code": "aisg",
        "name": "ТОО «AI System Group»",
        "short_name": "AI System Group",
        "bin": "190740026451",
        "legal_address": (
            "РК, город Астана, район Нұра, Жилой массив Комсомольский, "
            "улица Айша Бибі, дом 48"
        ),
        "actual_address": "РК, город Астана, район Нұра, улица Жылыой, дом 24",
        "iban": "KZ4496503F0009018647",
        "bank": "Филиал АО «ForteBank» в г. Астана",
        "bik": "IRTYKZKA",
        "kbe": "17",
        "director_position": "Генеральный директор",
        "director_name": "Кашабаева Анаргуль Тулешовна",
        "addressee": [
            "Генеральному директору",
            "ТОО «AI System Group»",
            "А. Кашабаевой",
        ],
        # Колонтитул — из «Уведомление о смене наименования.docx».
        "letterhead_en": [
            "«AI System Group» LLP",
            "BIN 190740026451",
            "010000, Republic of Kazakhstan",
            "Astana, Nura District, Komsomolsky Residential Area, 48 Aisha Bibi Street",
        ],
        "letterhead_ru": [
            "ТОО «AI System Group»",
            "БИН 190740026451",
            "010000, Республика Казахстан",
            "г. Астана, район Нура,",
            "Жилой массив Комсомольский,",
            "улица Айша Биби, дом 48",
        ],
        "letterhead_font": "serif",
        "logo": "aisg.png",
    },
    {
        "code": "ist",
        "name": "ТОО «Industrial SmartTech»",
        "short_name": "Industrial SmartTech",
        "bin": "060240026358",
        "legal_address": "РК, г. Астана, ул. Жылыой, 24",
        "actual_address": None,
        "iban": None,
        "bank": None,
        "bik": None,
        "kbe": None,
        "director_position": "Генеральный директор",
        # Полное имя в присланных документах не встречается — только
        # «Усенов М.К.» в подписи письма.
        "director_name": "Усенов М. К.",
        "addressee": [
            "Генеральному директору",
            "ТОО «Industrial SmartTech»",
            "М. Усенову",
        ],
        # Колонтитул — из «Письмо по потопу Industrial_SmartTech.docx».
        "letterhead_en": [
            "Industrial SmartTech LLP",
            "BIN 060240026358",
            "060000, Republic of Kazakhstan",
            "Astana,",
            "Zhylyoy 24",
        ],
        "letterhead_ru": [
            "ТОО «Industrial SmartTech»",
            "БИН 060240026358",
            "060000, Республика Казахстан",
            "г. Астана,",
            "ул. Жылыой 24",
        ],
        "letterhead_font": "narrow",
        "logo": "ist.png",
    },
    {
        "code": "gqs",
        "name": "ТОО «GQ-System»",
        "short_name": "GQ-System",
        "bin": "180940031651",
        "legal_address": "РК, г. Нур-Султан, пр. Момышулы, зд. 16, в.п. 12",
        "actual_address": None,
        "iban": None,
        "bank": None,
        "bik": None,
        "kbe": None,
        "director_position": "Генеральный директор",
        # Руководитель — по сведениям электронного правительства РК на
        # 16.11.2026 (в разных источниках сведения расходятся).
        "director_name": "Абдуалиев Бауыржан Нурланулы",
        "addressee": [
            "Генеральному директору",
            "ТОО «GQ-System»",
            "Б. Абдуалиеву",
        ],
        # Колонтитул — из «Бланк GQ System.docx».
        "letterhead_en": [
            "«GQ-System» LLP",
            "BIN 180940031651",
            "010000, Republic of Kazakhstan",
            "Nur-Sultan, 16 Momyshuly ave., IP 12",
            "tel: +7 777 211 30 85",
        ],
        "letterhead_ru": [
            "ТОО «GQ-System»",
            "БИН 180940031651",
            "010000, Республика Казахстан",
            "г. Нур-Султан, пр. Момышулы,",
            "зд. 16, в.п. 12",
            "тел.: +7 777 211 30 85",
        ],
        "letterhead_font": "narrow",
        "logo": "gqs.png",
    },
]

#: Месяц для полей «за какой месяц» — значение `2026-11`, в тексте
#: «ноябрь 2026 года».
_MONTH_FIELD = {"type": "month", "required": True}

#: Адресат трёх первых шаблонов — из времени до выбора компании. Новым
#: документам адресата даёт компания; этот остаётся для уже созданных
#: документов без компании и без своего «Кому»: иначе в их карточках
#: адресат пропал бы.
LEGACY_ADDRESSEE = [
    "Генеральному директору",
    "ТОО «AI System Group»",
    "А. Кашабаевой",
]

TEMPLATES = [
    {
        "code": "hiring",
        "title": "Заявление на приём на работу",
        "body_schema": {
            "doc_title": "Заявление",
            "addressee": LEGACY_ADDRESSEE,
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
            "addressee": LEGACY_ADDRESSEE,
            "fields": [
                {"name": "date_from", "label": "Дата начала отпуска", "type": "date",
                 "required": True},
                {"name": "days", "label": "Количество календарных дней", "type": "number",
                 "required": True},
            ],
        },
        "text_template": (
            # Запятая перед «сроком» — как на бланке.
            "Прошу Вас предоставить мне очередной трудовой отпуск с {date_from} года, "
            "сроком на {days:календарный день|календарных дня|календарных дней}."
        ),
    },
    {
        "code": "dismissal",
        "title": "Заявление на увольнение",
        "body_schema": {
            "doc_title": "Заявление",
            "addressee": LEGACY_ADDRESSEE,
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
    {
        "code": "advance",
        "title": "Заявление на аванс",
        "body_schema": {
            "doc_title": "Заявление",
            "fields": [
                {"name": "amount", "label": "Сумма аванса, тенге", "type": "money",
                 "required": True},
                {"name": "month", "label": "За какой месяц", **_MONTH_FIELD},
            ],
        },
        "text_template": (
            "Прошу Вас выдать аванс в размере {amount:words} в счет заработной платы "
            "за {month}."
        ),
    },
    {
        "code": "salary_account",
        "title": "Заявление о перечислении зарплаты на другой счёт",
        "body_schema": {
            "doc_title": "Заявление",
            "fields": [
                {"name": "account_holder", "label": "На чьё имя счёт (ФИО)", "type": "text",
                 "required": True},
                {"name": "iban", "label": "Номер счёта (IBAN)", "type": "text",
                 "required": True, "pattern": r"KZ\d{2}[A-Z0-9]{16}",
                 "pattern_hint": "KZ и 18 знаков, например KZ4496503F0009018647"},
                {"name": "until", "label": "На срок до", "type": "date", "required": True},
                {"name": "reason", "label": "Причина (в связи с …)", "type": "text",
                 "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас перечислять мою заработную плату на счет {account_holder}, "
            "счет № {iban}, на срок до {until} года в связи с {reason}."
        ),
    },
    {
        "code": "deduction_card",
        "title": "Заявление на удержание (корпоративная карта)",
        "body_schema": {
            "doc_title": "Заявление",
            "fields": [
                {"name": "period_from", "label": "Период использования: с", "type": "date",
                 "required": True},
                {"name": "period_to", "label": "Период использования: по", "type": "date",
                 "required": True},
                {"name": "amount", "label": "Сумма удержания, тенге", "type": "money",
                 "required": True},
                {"name": "month", "label": "Удержать из зарплаты за", **_MONTH_FIELD},
            ],
        },
        "text_template": (
            "Мной были использованы денежные средства с корпоративной карты «Яндекс» "
            "в период с {period_from} г. по {period_to} г. для личного пользования.\n\n"
            "Прошу Вас удержать сумму в размере {amount:words} с моей заработной платы "
            "за {month}. С удержанием согласен(-на)."
        ),
    },
    {
        "code": "tax_deduction",
        "title": "Заявление на налоговый вычет",
        "body_schema": {
            "doc_title": "Заявление",
            "fields": [
                {"name": "date_from", "label": "Применять вычет с", "type": "date",
                 "required": True},
            ],
        },
        # {company} — наименование выбранной компании, подставляется само.
        "text_template": (
            "В соответствии со статьей 403 Налогового кодекса РК в целях уменьшения "
            "налогооблагаемого дохода при исчислении индивидуального подоходного налога "
            "прошу производить расчет с вычетом в сумме 30-кратного месячного расчетного "
            "показателя из моего дохода за каждый календарный месяц с {date_from} года.\n\n"
            "Указанный налоговый вычет по моим доходам применяется только в {company}.\n\n"
            "В случае изменения обстоятельств, дающих право на использование данных "
            "налоговых вычетов, обязуюсь немедленно сообщить об этом."
        ),
    },
    {
        "code": "childcare_leave",
        "title": "Заявление на отпуск по уходу за ребёнком",
        "body_schema": {
            "doc_title": "Заявление",
            # Без копии свидетельства документ не отправить: на бланке
            # «Копию свидетельства о рождении прилагаю».
            "required_attachment": "копия свидетельства о рождении ребёнка",
            "fields": [
                {"name": "child_name", "label": "ФИО ребёнка", "type": "text",
                 "required": True},
                {"name": "date_from", "label": "Дата начала отпуска", "type": "date",
                 "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас предоставить мне отпуск по уходу за ребенком {child_name} "
            "до достижения его (её) 3-х лет с {date_from} года.\n\n"
            "Копию свидетельства о рождении прилагаю."
        ),
    },
    {
        "code": "childcare_return",
        "title": "Заявление о досрочном выходе из отпуска по уходу за ребёнком",
        "body_schema": {
            "doc_title": "Заявление",
            "fields": [
                {"name": "return_date", "label": "Дата выхода на работу", "type": "date",
                 "required": True},
                {"name": "day_mode", "label": "Продолжительность рабочего дня",
                 "type": "select", "options": ["нормальной", "неполной"], "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас прервать отпуск без сохранения заработной платы по уходу за "
            "ребенком до достижения им возраста трех лет и разрешить приступить к "
            "исполнению обязанностей с {return_date} года с {day_mode} "
            "продолжительностью рабочего дня."
        ),
    },
    {
        "code": "memo_free",
        "title": "Служебная записка",
        "body_schema": {
            "doc_title": "Служебная записка",
            "fields": [
                {"name": "text", "label": "Текст служебной записки", "type": "textarea",
                 "required": True},
            ],
        },
        "text_template": "{text}",
    },
    {
        "code": "memo_deduction",
        "title": "Служебная записка на удержание (ОС, ТМЦ)",
        "body_schema": {
            "doc_title": "Служебная записка",
            "fields": [
                {"name": "amount", "label": "Сумма удержания, тенге", "type": "money",
                 "required": True},
                {"name": "items", "label": "За что (выданные ОС, ТМЦ, спецодежда и т. д.)",
                 "type": "textarea", "required": True},
            ],
        },
        "text_template": (
            "Прошу Вас удержать с меня сумму в размере {amount:words} за {items}.\n\n"
            "С удержанием согласен(-на)."
        ),
    },
]

_COMPANY_KEYS = tuple(k for k in COMPANIES[0] if k != "code")


async def apply_companies(session: AsyncSession) -> list[str]:
    """Создаёт компании и обновляет реквизиты существующих. Идемпотентно."""
    log: list[str] = []
    for spec in COMPANIES:
        company = (
            await session.execute(select(Company).where(Company.code == spec["code"]))
        ).scalar_one_or_none()
        if company is None:
            session.add(Company(**spec, is_active=True))
            log.append(f"создана компания {spec['name']}")
            continue
        if any(getattr(company, key) != spec.get(key) for key in _COMPANY_KEYS):
            for key in _COMPANY_KEYS:
                setattr(company, key, spec.get(key))
            log.append(f"обновлены реквизиты {spec['name']}")
    await session.flush()
    return log

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

        for line in await apply_companies(session):
            print(line)
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
