"""
Загрузка справочника сотрудников из выгрузки «Контакты».

Формат — таблица с табуляцией, как её копируют со страницы контактов:

    N  Компания  Ф.И.О.  Отдел  Должность  Телефон  Email

    python -m app.scripts.import_employees data/employees.tsv           # загрузить
    python -m app.scripts.import_employees data/employees.tsv --dry-run # только отчёт

Сотрудник ищется по адресу почты, поэтому повторная загрузка обновлённой
выгрузки ничего не дублирует: ФИО, должность и подразделение обновляются,
новые сотрудники добавляются. Роль, пароль и признак активности уже
заведённых сотрудников не трогаются — их ведёт администратор.

Пропускаются строки, которые сотрудниками не являются или не могут
получить задание: группы рассылок и сотрудники без почты. Сама выгрузка
содержит персональные данные и хранится в `data/`, вне репозитория.
"""

import argparse
import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import UserRole
from app.db.tables import Department, User

#: Строки этой «компании» — общие ящики, а не люди.
MAILING_GROUPS = "Группы рассылок"

#: Домены почты группы. Адрес на другом домене загружается, но попадает в
#: отчёт: опечатка в домене означает, что письма со ссылкой не дойдут.
KNOWN_DOMAINS = {"gqgroup.kz", "gqe.kz"}

_SPACES = re.compile(r"\s+")


def _clean(value: str) -> str:
    # В выгрузке у почты хвост «📄» — значок карточки контакта со страницы.
    return _SPACES.sub(" ", value.replace("📄", "")).strip()


def department_key(company: str, name: str) -> str:
    """
    Ключ подразделения для сравнения.

    В выгрузке одно подразделение встречается в разном написании —
    «Отдел расчетов» и «Отдел рассчетов», «Слаботочных Систем» и
    «слаботочных систем». Без нормализации из одного отдела получилось бы два.
    """
    text = f"{company}|{name}".lower().replace("ё", "е").replace("рассчет", "расчет")
    return _SPACES.sub(" ", text)


@dataclass
class Row:
    line: int
    company: str
    full_name: str
    department: str
    position: str
    email: str


@dataclass
class Report:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    departments_created: list[str] = field(default_factory=list)
    skipped_groups: int = 0
    skipped_no_email: list[str] = field(default_factory=list)
    invalid_email: list[str] = field(default_factory=list)
    unusual_domain: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


def parse(text: str, report: Report) -> list[Row]:
    rows: list[Row] = []
    seen: set[str] = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        parts = [_clean(p) for p in raw.split("\t")]
        if len(parts) < 7 or not parts[0].isdigit():
            continue  # заголовок, пустые и служебные строки выгрузки

        _, company, full_name, department, position, _phone, email = parts[:7]

        if company == MAILING_GROUPS:
            report.skipped_groups += 1
            continue
        if not email or email == "-":
            report.skipped_no_email.append(full_name)
            continue

        try:
            email = validate_email(email, check_deliverability=False).normalized.lower()
        except EmailNotValidError:
            report.invalid_email.append(f"{full_name}: {email}")
            continue

        if email in seen:
            report.duplicates.append(f"{full_name}: {email}")
            continue
        seen.add(email)

        if email.rsplit("@", 1)[1] not in KNOWN_DOMAINS:
            report.unusual_domain.append(f"{full_name}: {email}")

        rows.append(Row(number, company, full_name, department, position, email))
    return rows


async def _departments(session: AsyncSession, rows: list[Row], report: Report) -> dict[str, int]:
    """
    Подразделения по ключу. Имя — «Отдел (Компания)»: в разных компаниях
    группы есть одноимённые отделы, «Администрация» — в трёх.
    """
    existing = {
        d.name: d for d in (await session.execute(select(Department))).scalars().all()
    }
    by_key: dict[str, int] = {}
    for row in rows:
        key = department_key(row.company, row.department)
        if key in by_key:
            continue
        name = f"{row.department} ({row.company})"
        found = next(
            (d for n, d in existing.items() if department_key(*_split_name(n)) == key),
            None,
        )
        if found is None:
            found = Department(name=name)
            session.add(found)
            await session.flush()
            existing[name] = found
            report.departments_created.append(name)
        by_key[key] = found.id
    return by_key


def _split_name(name: str) -> tuple[str, str]:
    """«Отдел (Компания)» → (компания, отдел) для сравнения по ключу."""
    match = re.fullmatch(r"(.*) \(([^()]*)\)", name)
    return (match.group(2), match.group(1)) if match else ("", name)


async def import_rows(session: AsyncSession, rows: list[Row], report: Report) -> None:
    departments = await _departments(session, rows, report)
    users = {
        u.email.lower(): u for u in (await session.execute(select(User))).scalars().all()
    }

    for row in rows:
        department_id = departments[department_key(row.company, row.department)]
        user = users.get(row.email)

        if user is None:
            session.add(
                User(
                    email=row.email,
                    full_name=row.full_name,
                    position=row.position or None,
                    department_id=department_id,
                    role=UserRole.author,
                )
            )
            report.created.append(row.full_name)
            continue

        changes = {
            "full_name": row.full_name,
            "position": row.position or None,
            "department_id": department_id,
        }
        changed = [k for k, v in changes.items() if getattr(user, k) != v]
        if changed:
            for key in changed:
                setattr(user, key, changes[key])
            report.updated.append(f"{row.full_name} ({', '.join(changed)})")
        else:
            report.unchanged += 1

    await session.flush()


def print_report(report: Report, *, dry_run: bool) -> None:
    head = "ПРОВЕРКА (в базу ничего не записано)" if dry_run else "ЗАГРУЖЕНО"
    print(f"=== {head} ===")
    print(f"новых сотрудников: {len(report.created)}")
    print(f"обновлено: {len(report.updated)}")
    print(f"без изменений: {report.unchanged}")
    print(f"новых подразделений: {len(report.departments_created)}")
    print(f"пропущено групп рассылок: {report.skipped_groups}")

    sections = [
        ("Без почты — не загружены, задание им отправить некуда", report.skipped_no_email),
        ("Некорректная почта — не загружены", report.invalid_email),
        ("Повторяющаяся почта — загружена первая строка", report.duplicates),
        ("Почта не на домене группы — проверьте, нет ли опечатки", report.unusual_domain),
        ("Обновлены", report.updated),
    ]
    for title, items in sections:
        if items:
            print(f"\n{title}:")
            for item in items:
                print(f"  - {item}")


async def run(path: Path, dry_run: bool) -> Report:
    from app.db.session import async_session

    report = Report()
    rows = parse(path.read_text(encoding="utf-8"), report)
    async with async_session() as session:
        await import_rows(session, rows, report)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Загрузка справочника сотрудников")
    parser.add_argument("path", type=Path, help="выгрузка «Контакты», таблица с табуляцией")
    parser.add_argument("--dry-run", action="store_true", help="только отчёт, без записи")
    args = parser.parse_args()
    print_report(asyncio.run(run(args.path, args.dry_run)), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
