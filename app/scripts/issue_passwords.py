"""
Выдача паролей для входа и назначение администраторов.

    # пароли всем сотрудникам компании, у кого их ещё нет
    python -m app.scripts.issue_passwords --company "AI System Group"

    # сделать сотрудника администратором (и выдать пароль, если его нет)
    python -m app.scripts.issue_passwords --admin admin@company.kz

Пароли не печатаются в консоль: они записываются в CSV в `data/` (каталог
исключён из git), откуда их раздают сотрудникам. Файл открывается в Excel —
кодировка UTF-8 с BOM, иначе Excel показывает кириллицу кракозябрами.

Уже выданный пароль по умолчанию не меняется: повторный запуск выдаёт
пароли только новым сотрудникам, и никто не теряет работающий вход.
Перевыпустить всем — `--reset`.

Сотрудник компании определяется по подразделению: при загрузке справочника
подразделения называются «Отдел (Компания)».
"""

import argparse
import asyncio
import csv
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import http_404
from app.core.security import hash_password
from app.db.enums import UserRole
from app.db.tables import Department, User

#: Без похожих символов (0/O, 1/l/I): пароль переписывают с листа или
#: диктуют по телефону, и ошибка в одном символе — звонок в ИТ.
_ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
PASSWORD_LENGTH = 10


def new_password() -> str:
    while True:
        value = "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))
        # Хотя бы по одной строчной, заглавной и цифре — так пароль не
        # окажется случайно составленным из одних букв.
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
        ):
            return value


@dataclass
class Issued:
    full_name: str
    email: str
    position: str | None
    password: str


@dataclass
class Result:
    issued: list[Issued] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)


async def company_users(session: AsyncSession, company: str) -> list[User]:
    suffix = f"({company})"
    departments = [
        d.id
        for d in (await session.execute(select(Department))).scalars().all()
        if d.name.endswith(suffix)
    ]
    if not departments:
        return []
    return list(
        (
            await session.execute(
                select(User)
                .where(User.department_id.in_(departments))
                .where(User.is_active.is_(True))
                .order_by(User.full_name)
            )
        ).scalars().all()
    )


def _issue(user: User, result: Result, *, reset: bool) -> None:
    if user.password_hash and not reset:
        result.kept.append(user.full_name)
        return
    password = new_password()
    user.password_hash = hash_password(password)
    result.issued.append(Issued(user.full_name, user.email, user.position, password))


async def issue_for_company(
    session: AsyncSession, company: str, *, reset: bool = False
) -> Result:
    result = Result()
    for user in await company_users(session, company):
        _issue(user, result, reset=reset)
    await session.flush()
    return result


async def make_admin(session: AsyncSession, email: str, result: Result) -> None:
    """Назначает администратором; без пароля администратор войти не сможет."""
    user = (
        await session.execute(select(User).where(User.email == email.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        raise http_404(f"Сотрудник {email} не найден — сначала загрузите справочник")
    if user.role != UserRole.admin:
        user.role = UserRole.admin
        result.promoted.append(user.full_name)
    if not any(i.email == user.email for i in result.issued):
        _issue(user, result, reset=False)
    await session.flush()


def write_csv(issued: list[Issued], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ФИО", "Должность", "Логин (почта)", "Пароль"])
        for item in issued:
            writer.writerow([item.full_name, item.position or "", item.email, item.password])


async def run(company: str | None, admins: list[str], reset: bool, out_dir: Path) -> tuple[Result, Path | None]:
    from app.db.session import async_session

    result = Result()
    async with async_session() as session:
        if company:
            company_result = await issue_for_company(session, company, reset=reset)
            result.issued += company_result.issued
            result.kept += company_result.kept
        for email in admins:
            await make_admin(session, email, result)

        path = None
        if result.issued:
            # Файл пишется до фиксации: если запись не удалась, пароли в
            # базе не меняются и никто не остаётся с паролем, которого нет
            # ни у кого на руках.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = out_dir / f"passwords-{stamp}.csv"
            write_csv(result.issued, path)
        await session.commit()
    return result, path


def main() -> None:
    parser = argparse.ArgumentParser(description="Выдача паролей и назначение администраторов")
    parser.add_argument("--company", help="компания из справочника, например «AI System Group»")
    parser.add_argument("--admin", action="append", default=[], metavar="EMAIL",
                        help="назначить администратором (можно несколько раз)")
    parser.add_argument("--reset", action="store_true",
                        help="перевыпустить пароли и тем, у кого они уже есть")
    parser.add_argument("--out", type=Path, default=Path("data"), help="каталог для CSV")
    args = parser.parse_args()
    if not args.company and not args.admin:
        parser.error("укажите --company и/или --admin")

    result, path = asyncio.run(run(args.company, args.admin, args.reset, args.out))
    print(f"выдано паролей: {len(result.issued)}")
    print(f"пароль уже был, оставлен: {len(result.kept)}")
    for name in result.promoted:
        print(f"назначен администратором: {name}")
    if path:
        print(f"пароли записаны в {path} — раздайте сотрудникам и удалите файл")


if __name__ == "__main__":
    main()
