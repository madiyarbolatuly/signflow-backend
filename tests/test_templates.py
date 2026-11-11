"""
Шаблоны заявлений по бланкам компании: текст, подстановки, документ, сид.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.db.tables import MemoTemplate
from app.scripts.seed import ADDRESSEE, RETIRED_CODES, TEMPLATES, apply_templates
from app.services import pdf
from app.services.formatting import (
    GAP,
    compose_document,
    placeholders,
    preview_fields,
    render_text,
)

#: Значение для каждого поля шаблонов — чтобы проверить, что заполненный
#: документ не содержит ни пропусков, ни необработанных подстановок.
SAMPLE = {
    "position": "менеджер склада",
    "start_date": "2026-12-01",
    "registration_address": "г. Астана, ул. Достык, 4",
    "residence_address": "г. Астана, ул. Кенесары, 10",
    "phone": "+7 701 000 00 00",
    "date_from": "2026-12-01",
    "days": "14",
    "dismissal_date": "2026-12-15",
}


def _by_code(code: str) -> dict:
    return next(t for t in TEMPLATES if t["code"] == code)


# ─── сами шаблоны ────────────────────────────────────────────────────────────
def test_addressee_matches_company_requisites():
    """Реквизиты ТОО «AI System Group»: генеральный директор Кашабаева А. Т."""
    assert ADDRESSEE == [
        "Генеральному директору",
        "ТОО «AI System Group»",
        "г-же Кашабаевой А. Т.",
    ]
    assert all(t["body_schema"]["addressee"] == ADDRESSEE for t in TEMPLATES)


def test_exactly_the_three_requested_templates():
    assert [t["code"] for t in TEMPLATES] == ["hiring", "annual_leave", "dismissal"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["code"])
def test_text_refers_only_to_declared_fields(template):
    declared = {f["name"] for f in template["body_schema"]["fields"]}
    used = placeholders(template["text_template"])
    assert used <= declared, f"в тексте поля без объявления: {used - declared}"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["code"])
def test_filled_template_has_no_gaps_or_raw_placeholders(template):
    schema = template["body_schema"]
    body = {f["name"]: SAMPLE[f["name"]] for f in schema["fields"]}
    text = " ".join(render_text(template["text_template"], body, schema))
    assert GAP not in text
    assert "{" not in text and "}" not in text


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["code"])
def test_every_field_is_required(template):
    """На бланке каждое поле обязательно: заявление без даты не примут."""
    assert all(f.get("required") for f in template["body_schema"]["fields"])


def test_rendered_texts_read_like_the_paper_forms():
    def text(code: str) -> str:
        t = _by_code(code)
        return " ".join(render_text(t["text_template"], SAMPLE, t["body_schema"]))

    assert text("hiring") == (
        "Прошу Вас принять меня на работу на должность менеджер склада "
        "с 01 декабря 2026 года."
    )
    assert text("annual_leave") == (
        "Прошу Вас предоставить мне очередной трудовой отпуск с 01 декабря 2026 года "
        "сроком на 14 календарных дней."
    )
    assert text("dismissal") == (
        "Прошу Вас расторгнуть со мной трудовой договор по собственному желанию "
        "15 декабря 2026 года."
    )


# ─── подстановки ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "days,expected",
    [
        ("1", "1 календарный день"),
        ("2", "2 календарных дня"),
        ("4", "4 календарных дня"),
        ("5", "5 календарных дней"),
        ("11", "11 календарных дней"),
        ("14", "14 календарных дней"),
        ("21", "21 календарный день"),
        ("22", "22 календарных дня"),
        ("112", "112 календарных дней"),
    ],
)
def test_days_agree_with_the_number(days, expected):
    """На бумаге «сроком на ___ календарных дней» дописывают от руки; в
    электронном документе «21 календарных дней» — ошибка, которую заметят."""
    t = _by_code("annual_leave")
    text = " ".join(render_text(t["text_template"], {"date_from": "2026-12-01", "days": days},
                                t["body_schema"]))
    assert text.endswith(f"сроком на {expected}.")


def test_unfilled_field_leaves_a_visible_gap():
    t = _by_code("dismissal")
    text = " ".join(render_text(t["text_template"], {}, t["body_schema"]))
    assert GAP in text


def test_gap_keeps_the_word_after_a_number():
    """Пустое «сколько дней» не должно съедать слово: на бланке «сроком на
    ___ календарных дней», а не «сроком на ___.»."""
    t = _by_code("annual_leave")
    text = " ".join(render_text(t["text_template"], {"date_from": "2026-12-01"},
                                t["body_schema"]))
    assert text.endswith(f"сроком на {GAP} календарных дней.")


def test_unknown_placeholder_becomes_a_gap_not_a_crash():
    assert render_text("Срок {nonexistent}.", {}, None) == [f"Срок {GAP}."]


def test_paragraphs_split_on_blank_lines():
    assert render_text("Первый.\n\nВторой.", {}, None) == ["Первый.", "Второй."]


# ─── документ ────────────────────────────────────────────────────────────────
def test_document_follows_the_paper_layout():
    t = _by_code("hiring")
    doc = compose_document(
        schema=t["body_schema"],
        text_template=t["text_template"],
        body=SAMPLE,
        addressee=None,
        author_name="Иванов Иван Иванович",
        author_position="менеджер склада",
        dated=datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc),
    )
    assert doc["kind"] == "letter"
    assert doc["addressee"] == ADDRESSEE
    assert doc["sender"] == [
        {"value": "менеджер склада", "caption": "должность"},
        {"value": "Иванов Иван Иванович", "caption": "ФИО"},
    ]
    assert [f["label"] for f in doc["header_fields"]] == [
        "Адрес регистрации",
        "Адрес проживания",
        "Конт. тел",
    ]
    assert doc["doc_title"] == "Заявление"
    # 21:00 UTC — это уже 11 ноября в Астане.
    assert doc["date"] == "«11» ноября 2026 г."
    # Поля шапки в текст заявления не попадают.
    assert "Достык" not in " ".join(doc["paragraphs"])


def test_addressee_can_be_overridden_per_memo():
    t = _by_code("dismissal")
    doc = compose_document(
        schema=t["body_schema"],
        text_template=t["text_template"],
        body=SAMPLE,
        addressee="Директору\nТОО «GQ-Group Invest»\n\n",
        author_name="Иванов И. И.",
        author_position=None,
        dated=date(2026, 12, 1),
    )
    assert doc["addressee"] == ["Директору", "ТОО «GQ-Group Invest»"]
    # Должности нет — строки «(должность)» на листе тоже нет.
    assert [s["caption"] for s in doc["sender"]] == ["ФИО"]


def test_template_without_text_has_no_letter_document():
    assert compose_document(
        schema={"fields": []}, text_template=None, body={}, addressee=None,
        author_name="x", author_position=None, dated=None,
    ) is None


def test_preview_marks_missing_required_fields():
    schema = _by_code("annual_leave")["body_schema"]
    fields = preview_fields({"date_from": "2026-12-01", "days": ""}, schema)
    assert [(f["name"], f["missing"], f["display"]) for f in fields] == [
        ("date_from", False, "01.12.2026"),
        ("days", True, ""),
    ]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["code"])
def test_letter_pdf_builds(template):
    from types import SimpleNamespace

    memo = SimpleNamespace(
        number="СЗ-2026-0001", title=template["title"], body=SAMPLE,
        created_at=datetime(2026, 11, 11, 9, 0), submitted_at=None, file_hash=None,
    )
    doc = compose_document(
        schema=template["body_schema"], text_template=template["text_template"],
        body=SAMPLE, addressee=None, author_name="Иванов <И.> & Ко",
        author_position="менеджер", dated=date(2026, 11, 11),
    )
    data = pdf.render(memo=memo, author=None, document=doc)
    assert data.startswith(b"%PDF-")


# ─── сид ─────────────────────────────────────────────────────────────────────
async def test_seed_creates_updates_and_retires(session):
    # Учебный шаблон первой версии, на который могут ссылаться записки.
    session.add(MemoTemplate(code="training", title="Направление на обучение",
                             body_schema={"fields": []}, is_active=True))
    await session.flush()

    first = await apply_templates(session)
    assert sum("создан" in line for line in first) == 3
    assert any("выключен" in line for line in first)

    active = (
        await session.execute(
            select(MemoTemplate.code).where(MemoTemplate.is_active.is_(True))
        )
    ).scalars().all()
    assert sorted(active) == ["annual_leave", "dismissal", "hiring"]

    # Выведенный шаблон не удалён: на него ссылаются старые записки.
    retired = (
        await session.execute(select(MemoTemplate).where(MemoTemplate.code.in_(RETIRED_CODES)))
    ).scalars().all()
    assert retired and not any(t.is_active for t in retired)

    assert await apply_templates(session) == [], "повторный запуск ничего не меняет"

    # Правка шаблона в коде доходит до уже существующей строки.
    hiring = (
        await session.execute(select(MemoTemplate).where(MemoTemplate.code == "hiring"))
    ).scalar_one()
    hiring.text_template = "устаревший текст"
    await session.flush()
    assert await apply_templates(session) == ["обновлён шаблон «Заявление на приём на работу»"]
    assert hiring.text_template == _by_code("hiring")["text_template"]


# ─── штамп электронной подписи ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "full,short",
    [
        ("Садуакас Мадияр Болатулы", "Садуакас М. Б."),
        ("Қуанышқызы Назерке", "Қуанышқызы Н."),
        ("Администратор", "Администратор"),
        ("", ""),
    ],
)
def test_short_name_like_company_documents(full, short):
    from app.services.formatting import short_name

    assert short_name(full) == short


def test_stamp_appears_only_once_submitted():
    t = _by_code("dismissal")
    common = dict(
        schema=t["body_schema"], text_template=t["text_template"], body=SAMPLE,
        addressee=None, author_name="Садуакас Мадияр Болатулы", author_position="Инженер",
        dated=date(2026, 11, 11),
    )
    assert compose_document(**common)["stamp"] is None, "у черновика подписи ещё нет"

    stamp = compose_document(
        **common, submitted_at=datetime(2026, 11, 11, 7, 15, tzinfo=timezone.utc)
    )["stamp"]
    assert stamp == {"name": "Садуакас М. Б.", "at": "11.11.2026 12:15", "zone": "UTC+05:00"}


def test_signed_letter_pdf_with_sheet_builds():
    from types import SimpleNamespace

    t = _by_code("annual_leave")
    memo = SimpleNamespace(
        number="СЗ-2026-0001", title=t["title"], body=SAMPLE, status="approved",
        created_at=datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc),
        submitted_at=datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc),
        file_hash="ab" * 32,
    )
    author = SimpleNamespace(full_name="Садуакас Мадияр Болатулы", position="Инженер")
    doc = compose_document(
        schema=t["body_schema"], text_template=t["text_template"], body=SAMPLE,
        addressee=None, author_name=author.full_name, author_position=author.position,
        dated=memo.submitted_at, submitted_at=memo.submitted_at,
    )
    rows = [
        {"order_no": 1, "signer_name": "Сапарбаева А. С.", "signer_position": "Руководитель",
         "status": "approved", "decided_at": memo.completed_at, "comment": None},
        {"order_no": 2, "signer_name": "Капанова И. М.", "signer_position": "Зам. ген. директора",
         "status": "not_required", "decided_at": None, "comment": None},
    ]
    data = pdf.render(memo=memo, author=author, document=doc, sheet_rows=rows)
    assert data.startswith(b"%PDF-")

