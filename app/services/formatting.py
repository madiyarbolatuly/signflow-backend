"""
Представление полей служебной записки для человека.

Одна точка правды для PDF, карточки автора и страницы подписания: подписи
берутся из схемы шаблона, значения приводятся к привычному виду (дата —
`01.12.2026`, а не `2026-12-01`; число — с разрядами). Без общего места
документ и экран расходились бы.
"""

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_EMPTY = "—"
#: Неразрывный пробел: разряды числа не должны разъезжаться по строкам.
_NBSP = "\u00a0"


def to_local(value: datetime | None) -> datetime | None:
    """
    Момент времени в часовом поясе документов (`settings.display_timezone`).

    Значение без пояса считается UTC: так его хранит база, а SQLite при
    чтении пояс теряет.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(settings.display_timezone))


def timezone_label() -> str:
    """Подпись пояса для документа: «UTC+05:00»."""
    offset = datetime.now(ZoneInfo(settings.display_timezone)).strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}"


def _as_date(value) -> str | None:
    match = _ISO_DATE.match(str(value))
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        # Похоже на дату, но такой даты нет — показываем как ввели.
        return None
    return f"{day:02d}.{month:02d}.{year}"


def _as_number(value) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    if number == int(number):
        return f"{int(number):,}".replace(",", _NBSP)
    return f"{number:,.2f}".replace(",", _NBSP).replace(".", ",")


def display_value(value, field_type: str | None = None) -> str:
    """
    Значение поля в том виде, в каком его читает человек.

    Тип берётся из схемы шаблона. Форма присылает и числа, и даты строками,
    поэтому тип значения в Python ни о чём не говорит. Дата распознаётся и
    без подсказки схемы — у записки без шаблона схемы нет вовсе.
    """
    if value is None or value == "":
        return _EMPTY
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(display_value(item) for item in value) or _EMPTY

    if field_type == "number":
        return _as_number(value) or str(value)

    if field_type in (None, "date"):
        as_date = _as_date(value)
        if as_date:
            return as_date

    if field_type is None and isinstance(value, (int, float)):
        return _as_number(value) or str(value)

    return str(value)


def label_fields(body: dict | None, schema: dict | None) -> list[dict]:
    """
    Поля записки с подписями, типами и готовым к показу значением.

    Порядок задаёт шаблон. Поля, которых в схеме нет (записка без шаблона или
    шаблон изменили после отправки), идут в конце под своим ключом, а не
    скрываются: данные не должны молча исчезать.
    """
    meta = {
        f["name"]: f
        for f in (schema or {}).get("fields", [])
        if f.get("name")
    }
    order = list(meta)

    def rank(name: str) -> tuple[int, int]:
        return (0, order.index(name)) if name in meta else (1, 0)

    result = []
    for name, value in sorted((body or {}).items(), key=lambda kv: rank(kv[0])):
        field = meta.get(name, {})
        field_type = field.get("type")
        result.append(
            {
                "name": name,
                "label": field.get("label") or name,
                "type": field_type,
                "value": value,
                "display": display_value(value, field_type),
            }
        )
    return result
