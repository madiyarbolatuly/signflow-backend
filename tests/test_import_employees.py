"""Загрузка справочника сотрудников из выгрузки «Контакты»."""

from sqlalchemy import select

from app.db.enums import UserRole
from app.db.tables import Department, User
from app.scripts.import_employees import Report, import_rows, parse

HEADER = "N\tКомпания\tФ.И.О.\tОтдел\tДолжность\tТелефон\tEmail"


def tsv(*rows: str) -> str:
    return "\n".join([HEADER, *rows])


SAMPLE = tsv(
    "1\tAI System Group\tИванов Иван Иванович\tОтдел расчетов и проектных продаж\tРуководитель\t870\ti.ivanov@gqgroup.kz 📄",
    # Тот же отдел, но с опечаткой «рассчетов» и лишними пробелами.
    "2\tAI System Group\tПетров  Пётр\tОтдел  рассчетов и проектных продаж\tМенеджер\t870\tP.Petrov@GQGROUP.kz 📄",
    # Одноимённый отдел другой компании — отдельное подразделение.
    "3\tIndustrial Smart Tech\tСидоров Сидор\tАдминистрация\tГенеральный директор\t870\ts.sidorov@gqgroup.kz 📄",
    "4\tAI System Group\tКузнецов Кузьма\tАдминистрация\tДиректор\t870\tk.kuz@gqqroup.kz 📄",
    "5\tГруппы рассылок\tОбщая почта\tВсе отделы\tДля рассылки\t-\tall@gqgroup.kz 📄",
    "6\tAI System Group\tБезпочтов Батыр\tСклад\tВодитель\t870\t- 📄",
    "7\tAI System Group\tДубль Иванова\tСклад\tКладовщик\t870\ti.ivanov@gqgroup.kz 📄",
    "8\tAI System Group\tКривой Адрес\tСклад\tКладовщик\t870\tне-почта 📄",
)


def test_parse_skips_what_cannot_receive_a_task():
    report = Report()
    rows = parse(SAMPLE, report)

    assert [r.email for r in rows] == [
        "i.ivanov@gqgroup.kz",
        "p.petrov@gqgroup.kz",
        "s.sidorov@gqgroup.kz",
        "k.kuz@gqqroup.kz",
    ]
    assert rows[1].full_name == "Петров Пётр", "лишние пробелы схлопываются"
    assert report.skipped_groups == 1
    assert report.skipped_no_email == ["Безпочтов Батыр"]
    assert report.duplicates == ["Дубль Иванова: i.ivanov@gqgroup.kz"]
    assert report.invalid_email == ["Кривой Адрес: не-почта"]
    assert report.unusual_domain == ["Кузнецов Кузьма: k.kuz@gqqroup.kz"], (
        "опечатку в домене нужно показать: письмо со ссылкой туда не дойдёт"
    )


async def _import(session, text: str) -> Report:
    report = Report()
    await import_rows(session, parse(text, report), report)
    return report


async def test_import_creates_users_and_merges_department_spellings(session):
    report = await _import(session, SAMPLE)
    assert len(report.created) == 4

    departments = sorted(
        (await session.execute(select(Department.name))).scalars().all()
    )
    assert departments == [
        "Администрация (AI System Group)",
        "Администрация (Industrial Smart Tech)",
        "Отдел расчетов и проектных продаж (AI System Group)",
    ], "«расчетов» и «рассчетов» — один отдел, одноимённые отделы разных компаний — разные"

    ivanov, petrov = (
        await session.execute(
            select(User).where(User.email.in_(["i.ivanov@gqgroup.kz", "p.petrov@gqgroup.kz"]))
            .order_by(User.email)
        )
    ).scalars().all()
    assert ivanov.department_id == petrov.department_id
    assert ivanov.role == UserRole.author
    assert ivanov.password_hash is None


async def test_reimport_updates_without_duplicates_or_touching_access(session):
    await _import(session, SAMPLE)

    ivanov = (
        await session.execute(select(User).where(User.email == "i.ivanov@gqgroup.kz"))
    ).scalar_one()
    # Администратор сделал сотрудника администратором и задал пароль.
    ivanov.role = UserRole.admin
    ivanov.password_hash = "hash"
    await session.flush()

    changed = SAMPLE.replace("\tРуководитель\t", "\tНачальник отдела\t")
    report = await _import(session, changed)

    assert report.created == []
    assert report.updated == ["Иванов Иван Иванович (position)"]
    assert report.unchanged == 3

    await session.refresh(ivanov)
    assert ivanov.position == "Начальник отдела"
    assert ivanov.role == UserRole.admin, "роль ведёт администратор, загрузка её не трогает"
    assert ivanov.password_hash == "hash"

    users = (await session.execute(select(User))).scalars().all()
    assert len(users) == 4, "повторная загрузка не дублирует сотрудников"
