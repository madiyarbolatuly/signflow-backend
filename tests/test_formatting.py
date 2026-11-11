"""
Проверки представления полей: подписи, порядок, вид значений — то, что
одинаково попадает в PDF, карточку автора и страницу подписания.
"""

from types import SimpleNamespace

import pytest

from app.services import pdf
from app.services.formatting import display_value, label_fields

NBSP = "\u00a0"


@pytest.mark.parametrize(
    "value,field_type,expected",
    [
        ("2026-12-01", "date", "01.12.2026"),
        # Записка без шаблона: схемы нет, но дату всё равно узнаём.
        ("2026-12-01", None, "01.12.2026"),
        # Похоже на дату, но такой даты не бывает — показываем как ввели.
        ("2026-02-31", "date", "2026-02-31"),
        # Форма присылает число строкой.
        ("185000", "number", f"185{NBSP}000"),
        (185000, "number", f"185{NBSP}000"),
        ("1250000.5", "number", f"1{NBSP}250{NBSP}000,50"),
        ("не число", "number", "не число"),
        # Текстовое поле с цифрами — это не число: номер приказа, телефон.
        ("185000", "text", "185000"),
        ("2026-12-01", "text", "2026-12-01"),
        ("", "text", "—"),
        (None, None, "—"),
        (True, None, "да"),
        (["а", "б"], None, "а, б"),
    ],
)
def test_display_value(value, field_type, expected):
    assert display_value(value, field_type) == expected


SCHEMA = {
    "fields": [
        {"name": "program", "label": "Программа обучения", "type": "text"},
        {"name": "date_from", "label": "Дата начала", "type": "date"},
        {"name": "cost", "label": "Стоимость, тенге", "type": "number"},
    ]
}


def test_fields_follow_template_order_not_input_order():
    fields = label_fields(
        {"cost": "185000", "date_from": "2026-12-01", "program": "ПБ"}, SCHEMA
    )
    assert [f["name"] for f in fields] == ["program", "date_from", "cost"]
    assert [f["label"] for f in fields] == [
        "Программа обучения",
        "Дата начала",
        "Стоимость, тенге",
    ]
    assert [f["display"] for f in fields] == ["ПБ", "01.12.2026", f"185{NBSP}000"]


def test_unknown_fields_are_kept_under_their_key():
    # Шаблон изменили после отправки: данные не должны молча исчезнуть.
    fields = label_fields({"legacy": "значение", "program": "ПБ"}, SCHEMA)
    assert [f["name"] for f in fields] == ["program", "legacy"]
    assert fields[1]["label"] == "legacy"


def test_memo_without_template_still_renders():
    fields = label_fields({"note": "текст"}, None)
    assert fields == [
        {"name": "note", "label": "note", "type": None, "value": "текст", "display": "текст"}
    ]


def test_pdf_survives_markup_characters_in_user_text():
    """
    Paragraph разбирает текст как разметку. До экранирования тема вида
    «Закупка <срочно>» или цель «R&D» роняли сборку PDF — а вместе с ней и
    отправку записки на согласование.
    """
    from datetime import datetime

    memo = SimpleNamespace(
        number="01ABC",
        title="Закупка <срочно> & согласование </para>",
        created_at=datetime(2026, 11, 11, 9, 0),
        body={"purpose": "R&D <b>не закрыт", "note": "a < b > c"},
        file_hash=None,
    )
    author = SimpleNamespace(full_name="Иванов <И.>", position="Инженер & механик")

    data = pdf.render(
        memo=memo,
        author=author,
        template_schema=None,
        sheet_rows=[
            {
                "order_no": 1,
                "signer_name": "Петров <П.>",
                "signer_position": "Рук. & зам.",
                "status": "rejected",
                "decided_at": datetime(2026, 11, 11, 10, 0),
                "comment": "Нет бюджета <совсем> & точка",
            }
        ],
    )
    assert data.startswith(b"%PDF-")


# ─── время в документах ──────────────────────────────────────────────────────
def test_document_time_is_local_not_utc():
    """
    В базе время в UTC. Записка, созданная в два часа ночи по времени Алматы,
    в UTC ещё «вчера» — и до пересчёта получала в документе вчерашнюю дату.
    """
    from datetime import datetime, timezone

    from app.services.formatting import to_local

    night_in_almaty = datetime(2026, 11, 10, 21, 0, tzinfo=timezone.utc)
    local = to_local(night_in_almaty)
    assert local.strftime("%d.%m.%Y %H:%M") == "11.11.2026 02:00"

    # SQLite отдаёт значение без пояса — оно тоже UTC.
    naive = datetime(2026, 11, 10, 21, 0)
    assert to_local(naive).strftime("%d.%m.%Y %H:%M") == "11.11.2026 02:00"

    assert to_local(None) is None


def test_timezone_label_names_the_offset():
    from app.services.formatting import timezone_label

    assert timezone_label() == "UTC+05:00"


def test_api_moments_always_carry_a_timezone():
    """
    Без пояса браузер принимает время за местное и сдвигает дату. На
    PostgreSQL пояс есть всегда, на SQLite его не было — разработка и
    эксплуатация показывали разное.
    """
    from datetime import datetime

    from app.schemas import MemoOut

    memo = MemoOut(
        id=1,
        title="t",
        status="draft",
        author_id="a",
        body={},
        created_at=datetime(2026, 11, 10, 21, 0),
    )
    assert memo.created_at.tzinfo is not None
    assert memo.model_dump(mode="json")["created_at"].endswith(("Z", "+00:00"))
