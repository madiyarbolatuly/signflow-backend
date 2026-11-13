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

    if field_type == "money":
        amount = _as_amount(value)
        return f"{_as_number(amount)} тенге" if amount is not None else str(value)

    if field_type == "month":
        return month_text(value) or str(value)

    if field_type in (None, "date"):
        as_date = _as_date(value)
        if as_date:
            return as_date

    if field_type is None and isinstance(value, (int, float)):
        return _as_number(value) or str(value)

    return str(value)


#: Месяц в родительном падеже — «04 мая 2026», как в приказах компании.
_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_MONTHS_NOMINATIVE = (
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
)

_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


def month_text(value) -> str | None:
    """`2026-11` → «ноябрь 2026»; не похоже на месяц — None."""
    match = _ISO_MONTH.match(str(value).strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        return None
    return f"{_MONTHS_NOMINATIVE[int(match.group(2)) - 1]} {match.group(1)}"


#: Пропуск в тексте документа, пока поле не заполнено. Виден на листе
#: предпросмотра так же, как пустая строка на бумажном бланке.
GAP = "________"

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)(?::([^{}]+))?\}")


def long_date(value: date) -> str:
    """«07» декабря 2026 — как дата вписывается в бланк: «___» ______ 202__."""
    return f"«{value.day:02d}» {_MONTHS_GENITIVE[value.month - 1]} {value.year}"


def _plural(n: int, forms: list[str]) -> str:
    """Форма слова для числа: 1 день, 2 дня, 5 дней, 11 дней, 21 день."""
    if len(forms) != 3:
        return forms[-1]
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _is_empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


_UNITS = (
    ("", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"),
    ("", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"),
)
_TEENS = (
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
)
_TENS = (
    "", "", "двадцать", "тридцать", "сорок", "пятьдесят",
    "шестьдесят", "семьдесят", "восемьдесят", "девяносто",
)
_HUNDREDS = (
    "", "сто", "двести", "триста", "четыреста", "пятьсот",
    "шестьсот", "семьсот", "восемьсот", "девятьсот",
)
#: Разряды: формы слова и род (тысяча — женского рода: «одна тысяча»).
_SCALES = (
    (("", "", ""), 0),
    (("тысяча", "тысячи", "тысяч"), 1),
    (("миллион", "миллиона", "миллионов"), 0),
    (("миллиард", "миллиарда", "миллиардов"), 0),
)


def number_in_words(n: int) -> str:
    """150000 → «сто пятьдесят тысяч». Целые от нуля до триллиона."""
    if n == 0:
        return "ноль"
    words: list[str] = []
    for power in range(len(_SCALES) - 1, -1, -1):
        chunk = n // 1000**power % 1000
        if not chunk:
            continue
        forms, gender = _SCALES[power]
        hundreds, rest = divmod(chunk, 100)
        tens, units = divmod(rest, 10)
        part = [_HUNDREDS[hundreds]]
        if tens == 1:
            part.append(_TEENS[units])
        else:
            part += [_TENS[tens], _UNITS[gender][units]]
        if power:
            part.append(_plural(chunk, list(forms)))
        words += [w for w in part if w]
    return " ".join(words)


def _as_amount(value) -> int | None:
    """Сумма в тенге целым числом; «150 000» и «150000» — одно и то же."""
    text = str(value).replace(" ", "").replace(_NBSP, "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0 or number != int(number) or number >= 10**12:
        return None
    return int(number)


def amount_text(value) -> str:
    """«150 000 (сто пятьдесят тысяч) тенге» — как сумма пишется в заявлении."""
    if _is_empty(value):
        return f"{GAP} ({GAP}) тенге"
    amount = _as_amount(value)
    if amount is None:
        return f"{value} тенге"
    return f"{_as_number(amount)} ({number_in_words(amount)}) тенге"


def _text_value(value, field_type: str | None, forms: str | None) -> str:
    """Значение поля внутри связного текста документа."""
    if forms == "words":
        return amount_text(value)

    if _is_empty(value):
        # Слово при числе остаётся и у пропуска: «сроком на ________
        # календарных дней», как на бумажном бланке, а не «сроком на ________.»
        return f"{GAP} {forms.split('|')[-1]}" if forms else GAP

    if field_type in (None, "date"):
        match = _ISO_DATE.match(str(value).strip())
        if match:
            try:
                return long_date(date(*(int(p) for p in match.groups())))
            except ValueError:
                pass

    if field_type == "month":
        # «за ноябрь 2026 года» — как на бланке «за ______ месяц 2026 года».
        month = month_text(value)
        return f"{month} года" if month else str(value)

    if forms:
        # «{days:календарный день|календарных дня|календарных дней}» → «21 календарный день».
        try:
            n = int(str(value).strip())
        except ValueError:
            return f"{value} {forms.split('|')[-1]}"
        return f"{_as_number(n)} {_plural(n, forms.split('|'))}"

    return display_value(value, field_type)


def render_text(text_template: str, body: dict | None, schema: dict | None) -> list[str]:
    """
    Связный текст документа по шаблону, разбитый на абзацы.

    Подстановка `{имя_поля}`; для числа можно указать формы слова —
    `{days:календарный день|календарных дня|календарных дней}`, иначе в
    документе выходит «21 календарных дней». `{amount:words}` — сумма в
    тенге цифрами и прописью. Незаполненное поле, как и поле,
    которого нет в схеме, становится пропуском — текст не ломается, а пробел
    виден на листе.
    """
    types = {
        f["name"]: f.get("type")
        for f in (schema or {}).get("fields", [])
        if f.get("name")
    }
    body = body or {}

    def substitute(match: re.Match) -> str:
        name, forms = match.group(1), match.group(2)
        return _text_value(body.get(name), types.get(name), forms)

    filled = _PLACEHOLDER.sub(substitute, text_template)
    # Абзац — каждая строка: в свободном тексте служебной записки автор
    # начинает новый абзац одним Enter, как в любом редакторе.
    return [p.strip() for p in filled.splitlines() if p.strip()]


def placeholders(text_template: str) -> set[str]:
    """Имена полей, на которые ссылается текст шаблона."""
    return {m.group(1) for m in _PLACEHOLDER.finditer(text_template)}


def short_name(full_name: str | None) -> str:
    """
    «Садуакас Мадияр Болатулы» → «Садуакас М. Б.» — как подписывают в
    документах компании («М. Озарчук», «А. Жусупова»). Одно слово остаётся
    как есть.
    """
    parts = (full_name or "").split()
    if len(parts) < 2:
        return (full_name or "").strip()
    return f"{parts[0]} " + " ".join(f"{p[0]}." for p in parts[1:3])


def compose_document(
    *,
    schema: dict | None,
    text_template: str | None,
    body: dict | None,
    addressee: str | None,
    author_name: str | None,
    author_position: str | None,
    dated: datetime | date | None,
    submitted_at: datetime | None = None,
    company: dict | None = None,
) -> dict | None:
    """
    Документ в виде, повторяющем бумажный бланк компании.

    Справа — кому и от кого (с подписями «(должность)», «(ФИО)» под
    значениями, как на бланке), затем название документа, связный текст и
    дата. Одна функция для PDF, карточки, страницы подписания и
    предпросмотра: документ не может выглядеть по-разному в разных местах.

    Возвращает None, если у шаблона нет текста — тогда документ печатается
    таблицей полей.
    """
    if not text_template:
        return None

    schema = schema or {}
    body = body or {}

    # «Кому»: что вписал автор, иначе директор выбранной компании, иначе
    # адресат из шаблона.
    if addressee and addressee.strip():
        lines = addressee.splitlines()
    else:
        lines = (company or {}).get("addressee") or schema.get("addressee") or []
    addressee_lines = [line.strip() for line in lines if line.strip()]

    # Фирменная шапка компании — над адресатом, как в колонтитуле бланка.
    letterhead = None
    if company and (company.get("letterhead_ru") or company.get("letterhead_en")):
        letterhead = {
            "company_id": company.get("id"),
            "en": list(company.get("letterhead_en") or []),
            "ru": list(company.get("letterhead_ru") or []),
            "has_logo": bool(company.get("logo")),
            # Имя файла логотипа — для PDF; в ответ API не попадает.
            "logo": company.get("logo"),
        }

    sender = []
    if author_position and author_position.strip():
        sender.append({"value": author_position.strip(), "caption": "должность"})
    sender.append({"value": (author_name or "").strip() or GAP, "caption": "ФИО"})

    # Поля шапки — то, что на бланке стоит под ФИО: адреса, телефон.
    header_fields = [
        {
            "label": f.get("label") or f["name"],
            "display": GAP if _is_empty(body.get(f["name"])) else display_value(body.get(f["name"]), f.get("type")),
        }
        for f in schema.get("fields", [])
        if f.get("name") and f.get("placement") == "header"
    ]

    if isinstance(dated, datetime):
        dated = to_local(dated).date()
    date_text = (
        f"«{dated.day:02d}» {_MONTHS_GENITIVE[dated.month - 1]} {dated.year} г."
        if dated
        else ""
    )

    # Штамп электронной подписи вместо строки «______ (подпись)» бланка.
    # Есть только у отправленного документа: подписью автора служит сама
    # отправка, и её время — момент подписания.
    stamp = None
    if submitted_at is not None:
        local = to_local(submitted_at)
        stamp = {
            "name": short_name(author_name),
            "at": local.strftime("%d.%m.%Y %H:%M"),
            "zone": timezone_label(),
        }

    return {
        "kind": "letter",
        "letterhead": letterhead,
        "addressee": addressee_lines,
        "sender": sender,
        "header_fields": header_fields,
        "doc_title": schema.get("doc_title") or "Заявление",
        # {company} в тексте — наименование выбранной компании
        # («вычет применяется только в ТОО «AI System Group»»).
        # Неразрывные пробелы: «ТОО «AI System Group»» не рвётся между строк.
        "paragraphs": render_text(
            text_template,
            {"company": ((company or {}).get("name") or "").replace(" ", _NBSP) or None, **body},
            schema,
        ),
        "date": date_text,
        "signer": (author_name or "").strip(),
        "stamp": stamp,
    }


def field_error(field: dict, value) -> str | None:
    """
    Что не так со значением поля, или None.

    Пустое значение здесь не ошибка — обязательность проверяется отдельно.
    Проверяется формат: `pattern` из схемы (например, IBAN) и сумма у поля
    типа `money`.
    """
    if _is_empty(value):
        return None
    text = str(value).strip()
    label = field.get("label") or field.get("name")
    pattern = field.get("pattern")
    if pattern:
        # Пробелы внутри номера счёта ставят для удобства чтения — не ошибка.
        compact = re.sub(r"\s+", "", text).upper()
        if not re.fullmatch(pattern, compact):
            hint = field.get("pattern_hint")
            return f"«{label}»: неверный формат" + (f" — {hint}" if hint else "")
    if field.get("type") == "money" and _as_amount(text) is None:
        return f"«{label}»: укажите сумму в тенге целым числом"
    return None


def body_errors(body: dict | None, schema: dict | None) -> list[str]:
    """Ошибки формата во всех полях записки."""
    body = body or {}
    errors = []
    for f in (schema or {}).get("fields", []):
        if f.get("name"):
            error = field_error(f, body.get(f["name"]))
            if error:
                errors.append(error)
    return errors


def preview_fields(body: dict | None, schema: dict | None) -> list[dict]:
    """
    Поля для предпросмотра ещё не отправленной записки.

    В отличие от `label_fields`, идёт по схеме, а не по присланным данным:
    обязательное поле показывается и пустым — с отметкой `missing`, чтобы
    его было видно на листе. Пустые необязательные поля в документ не идут.
    """
    body = body or {}
    result = []
    for f in (schema or {}).get("fields", []):
        name = f.get("name")
        if not name:
            continue
        value = body.get(name)
        empty = _is_empty(value)
        if empty and not f.get("required"):
            continue
        result.append(
            {
                "name": name,
                "label": f.get("label") or name,
                "type": f.get("type"),
                "value": None if empty else value,
                "display": "" if empty else display_value(value, f.get("type")),
                "missing": empty,
                "error": field_error(f, value),
            }
        )
    return result


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
