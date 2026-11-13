"""Справочник компаний, сумма прописью, формат полей."""

import pytest
from sqlalchemy import select

from app.db.tables import Company, Department, User
from app.scripts.seed import COMPANIES, apply_companies
from app.services import companies
from app.services.formatting import amount_text, body_errors, display_value, number_in_words


# ─── сумма прописью ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "n,words",
    [
        (0, "ноль"),
        (1, "один"),
        (2, "два"),
        (11, "одиннадцать"),
        (21, "двадцать один"),
        (100, "сто"),
        (1000, "одна тысяча"),
        (2000, "две тысячи"),
        (5000, "пять тысяч"),
        (21000, "двадцать одна тысяча"),
        (150000, "сто пятьдесят тысяч"),
        (312412, "триста двенадцать тысяч четыреста двенадцать"),
        (1000000, "один миллион"),
        (2500000, "два миллиона пятьсот тысяч"),
        (1_000_000_000, "один миллиард"),
    ],
)
def test_number_in_words(n, words):
    assert number_in_words(n) == words


def test_amount_text_like_the_form():
    assert amount_text("150000") == "150 000 (сто пятьдесят тысяч) тенге"
    assert amount_text("150 000") == "150 000 (сто пятьдесят тысяч) тенге"
    assert amount_text("") == "________ (________) тенге", "пустое — пропуск, как на бланке"


def test_money_and_month_display():
    assert display_value("150000", "money") == "150 000 тенге"
    assert display_value("2026-11", "month") == "ноябрь 2026"


def test_body_errors():
    schema = {"fields": [
        {"name": "iban", "label": "IBAN", "pattern": r"KZ\d{2}[A-Z0-9]{16}"},
        {"name": "amount", "label": "Сумма", "type": "money"},
    ]}
    assert body_errors({"iban": "KZ4496503F0009018647", "amount": "1000"}, schema) == []
    assert body_errors({}, schema) == [], "пустое — забота проверки обязательности"
    assert len(body_errors({"iban": "KZ1", "amount": "-5"}, schema)) == 2


# ─── справочник ──────────────────────────────────────────────────────────────
async def test_seed_companies_is_idempotent(session):
    assert await apply_companies(session) == ["создана компания ТОО «AI System Group»"]
    assert await apply_companies(session) == []

    company = (await session.execute(select(Company))).scalar_one()
    company.director_name = "кто-то другой"
    await session.flush()
    assert await apply_companies(session) == ["обновлены реквизиты ТОО «AI System Group»"]
    assert company.director_name == COMPANIES[0]["director_name"]


async def test_default_company_follows_the_department(session):
    await apply_companies(session)
    session.add(Company(code="ist", name="ТОО «Industrial Smart Tech»",
                        short_name="Industrial Smart Tech", addressee=[], letterhead_en=[],
                        letterhead_ru=[], is_active=True))
    dep = Department(name="Склад (Industrial Smart Tech)")
    session.add(dep)
    await session.flush()

    items = await companies.active(session)
    worker = User(email="w@example.com", full_name="W", department_id=dep.id)
    nobody = User(email="n@example.com", full_name="N")
    assert (await companies.default_for(session, worker, items)).code == "ist"
    assert (await companies.default_for(session, nobody, items)).code == "aisg", "иначе первая"
    assert await companies.default_for(session, nobody, []) is None


def test_logo_cannot_escape_the_assets_folder():
    evil = Company(code="x", name="x", short_name="x", logo="../../core/config.py")
    assert companies.logo_path(evil) is None
    good = Company(code="aisg", name="x", short_name="x", logo="aisg.png")
    assert companies.logo_path(good) is not None
