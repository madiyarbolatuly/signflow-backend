"""Выдача паролей сотрудникам компании и назначение администраторов."""

import csv

import pytest
from fastapi import HTTPException

from app.core.security import verify_password
from app.db.enums import UserRole
from app.db.tables import Department, User
from app.scripts.issue_passwords import (
    Result,
    issue_for_company,
    make_admin,
    new_password,
    write_csv,
)


async def _staff(session):
    ais = Department(name="IT развитие (AI System Group)")
    ist = Department(name="ИТР (Industrial Smart Tech)")
    session.add_all([ais, ist])
    await session.flush()
    users = {
        "a": User(email="a@gqgroup.kz", full_name="А Айсистемов", department_id=ais.id),
        "b": User(email="b@gqgroup.kz", full_name="Б Айсистемов", department_id=ais.id,
                  password_hash="existing-hash"),
        "c": User(email="c@gqgroup.kz", full_name="В Индастриал", department_id=ist.id),
        "d": User(email="d@gqgroup.kz", full_name="Г Уволенный", department_id=ais.id,
                  is_active=False),
    }
    session.add_all(users.values())
    await session.flush()
    return users


def test_password_is_readable_and_strong():
    for _ in range(200):
        p = new_password()
        assert len(p) == 10
        assert not set(p) & set("0O1lI"), "похожие символы путают при переписывании"
        assert any(c.isupper() for c in p) and any(c.islower() for c in p)
        assert any(c.isdigit() for c in p)


async def test_only_company_staff_without_password_get_one(session):
    users = await _staff(session)
    result = await issue_for_company(session, "AI System Group")

    assert [i.email for i in result.issued] == ["a@gqgroup.kz"]
    assert result.kept == ["Б Айсистемов"], "выданный пароль не перезаписывается"
    assert verify_password(result.issued[0].password, users["a"].password_hash)
    assert users["b"].password_hash == "existing-hash"
    assert users["c"].password_hash is None, "другая компания не затронута"
    assert users["d"].password_hash is None, "отключённым паролей не выдаём"


async def test_reset_reissues_everyone_in_company(session):
    users = await _staff(session)
    result = await issue_for_company(session, "AI System Group", reset=True)
    assert sorted(i.email for i in result.issued) == ["a@gqgroup.kz", "b@gqgroup.kz"]
    assert users["b"].password_hash != "existing-hash"


async def test_make_admin_promotes_and_gives_a_password(session):
    users = await _staff(session)
    result = Result()
    await make_admin(session, "C@GQGROUP.KZ", result)

    assert users["c"].role == UserRole.admin
    assert result.promoted == ["В Индастриал"]
    assert [i.email for i in result.issued] == ["c@gqgroup.kz"]

    again = Result()
    await make_admin(session, "c@gqgroup.kz", again)
    assert again.promoted == [] and again.issued == [], "повторный запуск ничего не меняет"


async def test_make_admin_refuses_unknown_email(session):
    with pytest.raises(HTTPException):
        await make_admin(session, "nobody@gqgroup.kz", Result())


def test_csv_opens_in_excel_with_cyrillic(tmp_path):
    from app.scripts.issue_passwords import Issued

    path = tmp_path / "p.csv"
    write_csv([Issued("Иванов И. И.", "i@gqgroup.kz", "Инженер", "Ab3defGh9k")], path)
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "без BOM Excel показывает кириллицу кракозябрами"
    rows = list(csv.reader(path.open(encoding="utf-8-sig"), delimiter=";"))
    assert rows == [
        ["ФИО", "Должность", "Логин (почта)", "Пароль"],
        ["Иванов И. И.", "Инженер", "i@gqgroup.kz", "Ab3defGh9k"],
    ]
