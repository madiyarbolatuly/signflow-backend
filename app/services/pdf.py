"""
Формирование PDF служебной записки и листа согласования (ТЗ, п. 5.1.6).

Про шрифты. Встроенные в reportlab шрифты — Type 1 с кодировкой Latin-1,
кириллицы в них нет: русский текст выходит набором точек. Поэтому
регистрируется TrueType-шрифт из системы. В образе Docker он появляется
пакетом `fonts-dejavu-core`, локально на Windows берётся Arial.
"""

from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

FONT_NAME = "SignFlowBody"
FONT_BOLD = "SignFlowBold"

#: Пары «обычный, полужирный». Берётся первая пара, найденная целиком:
#: Половины набора мало — без полужирного заголовки неотличимы от текста.
_FONT_CANDIDATES: list[tuple[str, str]] = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf"),
]

_fonts_ready = False


def _ensure_fonts() -> bool:
    """
    Регистрирует шрифт с кириллицей. Возвращает признак успеха.

    При неудаче формирование PDF не прерывается: документ выйдет встроенным
    шрифтом, то есть с нечитаемым русским текстом, но согласование от этого
    не встанет. Отсутствие шрифта — дефект развёртывания, а не причина
    отказать пользователю в подписании.
    """
    global _fonts_ready
    if _fonts_ready:
        return True

    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont(FONT_NAME, regular))
            pdfmetrics.registerFont(TTFont(FONT_BOLD, bold))
            pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_BOLD)
            _fonts_ready = True
            return True

    return False


def _styles() -> dict:
    have_fonts = _ensure_fonts()
    body_font = FONT_NAME if have_fonts else "Helvetica"
    bold_font = FONT_BOLD if have_fonts else "Helvetica-Bold"

    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "SfTitle", parent=base["Title"], fontName=bold_font, fontSize=14,
            leading=18, alignment=TA_CENTER, spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "SfH2", parent=base["Heading2"], fontName=bold_font, fontSize=11,
            leading=15, spaceBefore=10, spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "SfBody", parent=base["BodyText"], fontName=body_font, fontSize=10,
            leading=14, alignment=TA_JUSTIFY,
        ),
        "small": ParagraphStyle(
            "SfSmall", parent=base["BodyText"], fontName=body_font, fontSize=8.5,
            leading=11,
        ),
        "cell": ParagraphStyle(
            "SfCell", parent=base["BodyText"], fontName=body_font, fontSize=8.5,
            leading=11,
        ),
        "cell_bold": ParagraphStyle(
            "SfCellBold", parent=base["BodyText"], fontName=bold_font, fontSize=8.5,
            leading=11,
        ),
        "_fonts_ok": have_fonts,
    }


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _field_labels(body_schema: dict | None) -> dict[str, str]:
    """Сопоставление «имя поля → подпись» из схемы шаблона."""
    if not body_schema:
        return {}
    return {
        f.get("name"): f.get("label", f.get("name", ""))
        for f in body_schema.get("fields", [])
        if f.get("name")
    }


DECISION_LABEL = {
    "approved": "Согласовано",
    "rejected": "Отклонено",
    "not_required": "Не требовалось",
    "expired": "Просрочено",
    "active": "Ожидает решения",
    "pending": "Ожидает очереди",
}


def _memo_flowables(*, memo, author, template_schema: dict | None, st: dict) -> list:
    created = memo.created_at.strftime("%d.%m.%Y") if memo.created_at else ""
    head = Table(
        [[
            Paragraph("СЛУЖЕБНАЯ ЗАПИСКА", st["title"]),
        ], [
            Paragraph(
                f"№ {memo.number or 'без номера'} от {created}", st["small"]
            ),
        ]],
        colWidths=[170 * mm],
    )
    head.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))

    flow = [head, Spacer(1, 6 * mm), Paragraph(memo.title, st["h2"])]

    labels = _field_labels(template_schema)
    body = memo.body or {}
    if body:
        rows = [[
            Paragraph("Показатель", st["cell_bold"]),
            Paragraph("Значение", st["cell_bold"]),
        ]]
        for key, value in body.items():
            rows.append([
                Paragraph(labels.get(key, key), st["cell"]),
                Paragraph(_fmt(value), st["cell"]),
            ])

        table = Table(rows, colWidths=[60 * mm, 110 * mm])
        table.setStyle(
            TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7E6E6")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )
        flow += [Spacer(1, 3 * mm), table]

    flow += [
        Spacer(1, 8 * mm),
        Paragraph(
            f"Автор: {author.full_name}"
            + (f", {author.position}" if author.position else ""),
            st["body"],
        ),
    ]
    return flow


def _sheet_flowables(*, memo, rows: list[dict], st: dict) -> list:
    """
    Лист согласования.

    По каждому участнику: ФИО, должность, решение, дата и время, комментарий
    (ТЗ, п. 5.1.6).
    """
    table_rows = [[
        Paragraph("Уровень", st["cell_bold"]),
        Paragraph("Фамилия, инициалы", st["cell_bold"]),
        Paragraph("Должность", st["cell_bold"]),
        Paragraph("Решение", st["cell_bold"]),
        Paragraph("Дата и время", st["cell_bold"]),
        Paragraph("Комментарий", st["cell_bold"]),
    ]]

    for row in rows:
        decided = row.get("decided_at")
        table_rows.append([
            Paragraph(str(row.get("order_no", "")), st["cell"]),
            Paragraph(_fmt(row.get("signer_name") or row.get("signer_email")), st["cell"]),
            Paragraph(_fmt(row.get("signer_position")), st["cell"]),
            Paragraph(DECISION_LABEL.get(row.get("status"), _fmt(row.get("status"))), st["cell"]),
            Paragraph(
                decided.strftime("%d.%m.%Y %H:%M") if isinstance(decided, datetime) else "—",
                st["cell"],
            ),
            Paragraph(_fmt(row.get("comment")), st["cell"]),
        ])

    table = Table(
        table_rows,
        colWidths=[14 * mm, 36 * mm, 32 * mm, 26 * mm, 26 * mm, 36 * mm],
        repeatRows=1,
    )
    table.setStyle(
        TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7E6E6")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ])
    )

    note = Paragraph(
        "Документ согласован в информационной системе SignFlow простой "
        "электронной подписью. Решения зафиксированы в журнале аудита "
        "с указанием времени и сетевого адреса. Хеш подписанного файла "
        f"(SHA-256): {memo.file_hash or '—'}",
        st["small"],
    )

    return [
        PageBreak(),
        Paragraph("ЛИСТ СОГЛАСОВАНИЯ", st["title"]),
        Paragraph(
            f"к служебной записке № {memo.number or 'без номера'} — {memo.title}",
            st["small"],
        ),
        Spacer(1, 5 * mm),
        table,
        Spacer(1, 6 * mm),
        KeepTogether(note),
    ]


def render(
    *,
    memo,
    author,
    template_schema: dict | None = None,
    sheet_rows: list[dict] | None = None,
) -> bytes:
    """
    Собирает PDF. Передан `sheet_rows` — к документу добавляется лист
    согласования.
    """
    from io import BytesIO

    st = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=25 * mm,
        rightMargin=15 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f"Служебная записка {memo.number or ''}".strip(),
    )

    flow = _memo_flowables(
        memo=memo, author=author, template_schema=template_schema, st=st
    )
    if sheet_rows:
        flow += _sheet_flowables(memo=memo, rows=sheet_rows, st=st)

    doc.build(flow)
    return buffer.getvalue()
