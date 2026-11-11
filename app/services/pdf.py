"""
Формирование PDF документа и листа согласования (ТЗ, п. 5.1.6).

Вёрстка повторяет бумажные бланки компании: Times New Roman 12 pt, подписи
под строками — мельче, поля 30/15/20/20 мм, текст по ширине с абзацным
отступом. Так распечатанный документ неотличим от привычного.

Про шрифты. Встроенные в reportlab шрифты — Type 1 с кодировкой Latin-1,
кириллицы в них нет: русский текст выходит набором точек. Поэтому
регистрируется TrueType-шрифт из системы: на Windows — Times New Roman, в
образе Docker — Liberation Serif (метрически совместим с Times, пакет
`fonts-liberation`), в крайнем случае — DejaVu Serif.
"""

from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from app.services.formatting import label_fields, short_name, timezone_label, to_local

FONT_NAME = "SignFlowBody"
FONT_BOLD = "SignFlowBold"

#: Пары «обычный, полужирный», в порядке предпочтения. Берётся первая пара,
#: найденная целиком: без полужирного заголовки неотличимы от текста.
_FONT_CANDIDATES: list[tuple[str, str]] = [
    ("C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/timesbd.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
    # Без засечек — только если серифного шрифта в системе нет вовсе.
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]

_fonts_ready = False

#: Поля страницы, как в бланках компании.
MARGIN_LEFT = 30 * mm
MARGIN_RIGHT = 15 * mm
MARGIN_TOP = 20 * mm
MARGIN_BOTTOM = 20 * mm
CONTENT_WIDTH = A4[0] - MARGIN_LEFT - MARGIN_RIGHT  # 165 мм

INK = colors.HexColor("#000000")
MUTED = colors.HexColor("#555555")
RULE = colors.HexColor("#808080")
HEAD_FILL = colors.HexColor("#F0F0F0")
#: Цвет штампа электронной подписи — как у печатей и штампов ЭП: синий.
STAMP = colors.HexColor("#1F3F8C")


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
    body = FONT_NAME if have_fonts else "Times-Roman"
    bold = FONT_BOLD if have_fonts else "Times-Bold"

    def style(name, **kw):
        base = {"fontName": body, "fontSize": 12, "leading": 15, "textColor": INK}
        base.update(kw)
        return ParagraphStyle(name, **base)

    return {
        "text": style("SfText"),
        "right": style("SfRight", alignment=TA_LEFT),
        "title": style("SfTitle", fontName=bold, fontSize=14, leading=18, alignment=TA_CENTER),
        "subtitle": style("SfSubtitle", fontSize=10, leading=13, alignment=TA_CENTER,
                          textColor=MUTED),
        "paragraph": style("SfParagraph", leading=18, alignment=TA_JUSTIFY,
                           firstLineIndent=12.5 * mm),
        "caption": style("SfCaption", fontSize=8.5, leading=10, alignment=TA_CENTER,
                         textColor=MUTED),
        "h2": style("SfH2", fontName=bold, fontSize=12, leading=16),
        "cell": style("SfCell", fontSize=10, leading=12.5),
        "cell_center": style("SfCellC", fontSize=10, leading=12.5, alignment=TA_CENTER),
        "cell_bold": style("SfCellB", fontName=bold, fontSize=10, leading=12.5,
                           alignment=TA_CENTER),
        "cell_muted": style("SfCellM", fontSize=10, leading=12.5, textColor=MUTED),
        "small": style("SfSmall", fontSize=9, leading=11.5),
        "small_bold": style("SfSmallB", fontName=bold, fontSize=9, leading=11.5),
        "stamp_head": style("SfStampH", fontName=bold, fontSize=7.5, leading=9,
                            textColor=STAMP, alignment=TA_CENTER),
        "stamp": style("SfStamp", fontSize=8.5, leading=10.5, textColor=STAMP),
        "_bold": bold,
        "_body": body,
    }


def _fmt(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def _text(value) -> str:
    """
    Экранирует пользовательский текст перед вставкой в Paragraph.

    Paragraph разбирает содержимое как разметку: тема «Закупка <срочно>»
    или цель «R&D» без экранирования либо роняют сборку PDF, либо молча
    теряют часть текста.
    """
    return escape(_fmt(value))


DECISION_LABEL = {
    "approved": "Согласовано",
    "rejected": "Отклонено",
    "not_required": "Не требовалось",
    "expired": "Просрочено",
    "active": "Ожидает решения",
    "pending": "Ожидает очереди",
}

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _doc_date(memo) -> str:
    """Дата документа — день отправки, у неотправленного — день создания."""
    dated = to_local(getattr(memo, "submitted_at", None) or memo.created_at)
    return dated.strftime("%d.%m.%Y") if dated else ""


def _plain_table(rows, widths, extra=()) -> Table:
    """Таблица без рамок — для раскладки, а не для данных."""
    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        *extra,
    ]))
    return table


def _stamp(stamp: dict, st: dict) -> Table:
    """
    Штамп электронной подписи — так подписанный электронно документ
    выглядит в печатном виде вместо росчерка на строке «(подпись)».
    """
    box = Table(
        [
            [Paragraph("ДОКУМЕНТ ПОДПИСАН<br/>ПРОСТОЙ ЭЛЕКТРОННОЙ ПОДПИСЬЮ", st["stamp_head"])],
            [Paragraph(f"Владелец: {_text(stamp['name'])}", st["stamp"])],
            [Paragraph(f"Подписан: {_text(stamp['at'])} ({_text(stamp['zone'])})", st["stamp"])],
            [Paragraph("Система: УчетЭДО", st["stamp"])],
        ],
        colWidths=[72 * mm],
    )
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, STAMP),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, STAMP),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("TOPPADDING", (0, 1), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 4),
    ]))
    return box


# ─── заявление по образцу бланка ─────────────────────────────────────────────
def _letter_flowables(*, memo, document: dict, st: dict) -> list:
    """
    Документ по образцу бумажного бланка компании.

    Справа — адресат и «от ___ (должность) ___ (ФИО)»: значения на линии,
    мелкие подписи под ней по центру, как на бланке. По центру — название,
    ниже текст по ширине с абзацным отступом, внизу дата и штамп
    электронной подписи. Содержимое целиком приходит из `compose_document`:
    эта функция только раскладывает его по листу.
    """
    block_width = 88 * mm
    prefix_width = 7 * mm
    line_width = block_width - prefix_width

    rows: list = []
    style: list = []

    for line in document["addressee"]:
        rows.append([Paragraph(_text(line), st["right"]), ""])
        style.append(("SPAN", (0, len(rows) - 1), (1, len(rows) - 1)))

    rows.append(["", ""])  # отступ перед «от»
    style.append(("TOPPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 3))

    for index, item in enumerate(document["sender"]):
        value_row = len(rows)
        rows.append([
            Paragraph("от" if index == 0 else "", st["right"]),
            Paragraph(_text(item["value"]), st["right"]),
        ])
        # Значение стоит на линии — как вписанное в бланк от руки.
        style.append(("LINEBELOW", (1, value_row), (1, value_row), 0.6, INK))
        style.append(("BOTTOMPADDING", (0, value_row), (-1, value_row), 1))
        rows.append(["", Paragraph(f"({_text(item['caption'])})", st["caption"])])
        style.append(("BOTTOMPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 4))

    for field in document["header_fields"]:
        value_row = len(rows)
        rows.append([
            Paragraph(f"{_text(field['label'])}:", st["right"]),
            "",
        ])
        style.append(("SPAN", (0, value_row), (1, value_row)))
        rows.append(["", Paragraph(_text(field["display"]), st["right"])])
        style.append(("LINEBELOW", (1, len(rows) - 1), (1, len(rows) - 1), 0.6, INK))
        style.append(("BOTTOMPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 3))

    header = _plain_table(rows, [prefix_width, line_width], style)
    head = _plain_table([["", header]], [CONTENT_WIDTH - block_width, block_width])

    flow: list = [
        head,
        Spacer(1, 16 * mm),
        Paragraph(_text(document["doc_title"]), st["title"]),
    ]
    if memo.number:
        flow.append(Paragraph(
            f"рег. № {_text(memo.number)} от {_doc_date(memo)}", st["subtitle"]
        ))
    flow.append(Spacer(1, 8 * mm))
    for text in document["paragraphs"]:
        flow += [Paragraph(_text(text), st["paragraph"]), Spacer(1, 1.5 * mm)]

    # Низ бланка: дата слева, подпись справа. Вместо строки «______
    # (подпись)» у отправленного документа — штамп электронной подписи.
    if document.get("stamp"):
        signature = _stamp(document["stamp"], st)
    else:
        signature = _plain_table(
            [
                [Paragraph(_text(short_name(document["signer"])), st["right"])],
                [Paragraph("(подпись)", st["caption"])],
            ],
            [60 * mm],
            [("LINEBELOW", (0, 0), (0, 0), 0.6, INK),
             ("TOPPADDING", (0, 1), (0, 1), 1)],
        )
    footer = _plain_table(
        [[Paragraph(_text(document["date"]), st["text"]), signature]],
        [CONTENT_WIDTH - 72 * mm, 72 * mm],
    )
    flow += [Spacer(1, 16 * mm), KeepTogether(footer)]
    return flow


# ─── записка без текста шаблона: таблица полей ──────────────────────────────
def _memo_flowables(*, memo, author, template_schema: dict | None, st: dict) -> list:
    flow = [
        Paragraph("СЛУЖЕБНАЯ ЗАПИСКА", st["title"]),
        Paragraph(
            f"рег. № {_text(memo.number or 'не присвоен')} от {_doc_date(memo)}",
            st["subtitle"],
        ),
        Spacer(1, 8 * mm),
        Paragraph(_text(memo.title), st["h2"]),
        Spacer(1, 3 * mm),
    ]

    # Те же подписи, порядок и вид значений, что на экране: их отдаёт одна
    # функция, поэтому документ и предпросмотр не расходятся.
    fields = label_fields(memo.body, template_schema)
    if fields:
        rows = [[Paragraph("Показатель", st["cell_bold"]),
                 Paragraph("Значение", st["cell_bold"])]]
        for field in fields:
            rows.append([Paragraph(_text(field["label"]), st["cell"]),
                         Paragraph(_text(field["display"]), st["cell"])])
        table = Table(rows, colWidths=[60 * mm, CONTENT_WIDTH - 60 * mm], repeatRows=1)
        table.setStyle(_grid_style())
        flow.append(table)

    if author is not None:
        flow += [
            Spacer(1, 10 * mm),
            Paragraph(
                f"Автор: {_text(author.full_name)}"
                + (f", {_text(author.position)}" if author.position else ""),
                st["text"],
            ),
        ]
    return flow


def _grid_style(extra=()) -> TableStyle:
    return TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_FILL),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        *extra,
    ])


# ─── лист согласования ───────────────────────────────────────────────────────
def _sheet_flowables(*, memo, author, rows: list[dict], st: dict) -> list:
    """
    Лист согласования: по каждому участнику этап, должность, ФИО, решение,
    отметка о подписи с датой и замечания (ТЗ, п. 5.1.6). Ниже — сведения
    об электронной подписи и контрольная сумма подписанного файла.
    """
    head = ["Этап", "Должность", "Ф.И.О.", "Решение", "Подпись", "Замечания"]
    widths = [11 * mm, 34 * mm, 38 * mm, 24 * mm, 29 * mm, 29 * mm]  # = 165 мм

    table_rows = [[Paragraph(h, st["cell_bold"]) for h in head]]
    extra = []
    for index, row in enumerate(rows, start=1):
        status = row.get("status")
        decided = to_local(row.get("decided_at"))
        name = row.get("signer_name") or row.get("signer_email")

        if status in ("approved", "rejected") and isinstance(decided, datetime):
            signature = Paragraph(
                f"<font color='#1F3F8C'>подписано ПЭП</font><br/>"
                f"{decided.strftime('%d.%m.%Y')}<br/>{decided.strftime('%H:%M')}",
                st["cell_center"],
            )
        else:
            signature = Paragraph("—", st["cell_center"])

        decision_style = st["cell_muted"] if status in ("not_required", "expired", "pending") else st["cell"]
        decision = DECISION_LABEL.get(status, _fmt(status))
        if status == "rejected":
            decision = f"<b>{decision}</b>"

        table_rows.append([
            Paragraph(str(row.get("order_no", "")), st["cell_center"]),
            Paragraph(_text(row.get("signer_position")), st["cell"]),
            Paragraph(_text(name), st["cell"]),
            Paragraph(decision, decision_style),
            signature,
            Paragraph(_text(row.get("comment")), st["cell"]),
        ])
        # Участники одного этапа выделяются фоном через этап, чтобы
        # параллельные подписи читались одной группой.
        if int(row.get("order_no") or 0) % 2 == 0:
            extra.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#FAFAFA")))

    table = Table(table_rows, colWidths=widths, repeatRows=1)
    table.setStyle(_grid_style(extra))

    outcome = {
        "approved": "Документ согласован",
        "rejected": "Документ отклонён",
        "recalled": "Документ отозван автором",
    }.get(getattr(memo, "status", None))
    completed = to_local(getattr(memo, "completed_at", None))

    meta: list = [
        Paragraph(
            f"к документу «{_text(memo.title)}», рег. № {_text(memo.number or '—')} "
            f"от {_doc_date(memo)}",
            st["subtitle"],
        ),
        Spacer(1, 6 * mm),
    ]
    if author is not None:
        meta.append(Paragraph(
            f"<b>Автор:</b> {_text(author.full_name)}"
            + (f", {_text(author.position)}" if author.position else ""),
            st["small"],
        ))
    if outcome:
        meta.append(Paragraph(
            f"<b>Итог:</b> {outcome}"
            + (f" {completed.strftime('%d.%m.%Y в %H:%M')}" if completed else ""),
            st["small"],
        ))

    # Хеш группами по 8 символов: так его можно сверить глазами, и он
    # переносится по группам, а не рвётся посреди строки.
    digest = memo.file_hash or ""
    grouped = " ".join(digest[i:i + 8] for i in range(0, len(digest), 8)) or "—"

    info = Table(
        [
            [Paragraph("Сведения об электронной подписи", st["small_bold"])],
            [Paragraph(
                "Документ подписан простой электронной подписью в системе "
                "электронного документооборота УчетЭДО. Каждое решение зафиксировано "
                "в журнале аудита с указанием времени и сетевого адреса подписанта. "
                f"Время указано по часовому поясу {timezone_label()}.",
                st["small"],
            )],
            [Paragraph(
                "Контрольная сумма подписанного документа (SHA-256):<br/>"
                f"<font name='Courier' size='8.5'>{grouped}</font>",
                st["small"],
            )],
        ],
        colWidths=[CONTENT_WIDTH],
    )
    info.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFAFA")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 6),
    ]))

    return [
        PageBreak(),
        Paragraph("ЛИСТ СОГЛАСОВАНИЯ", st["title"]),
        *meta,
        Spacer(1, 4 * mm),
        table,
        Spacer(1, 8 * mm),
        KeepTogether(info),
    ]


# ─── нумерация страниц ───────────────────────────────────────────────────────
def _numbered_canvas(footer_left: str):
    """
    Холст, который знает общее число страниц.

    «Стр. 1 из 2» требует второго прохода: число страниц известно только
    после вёрстки, поэтому страницы копятся и подписываются при сохранении.
    """

    class NumberedCanvas(pdf_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):  # noqa: N802 — имя задано reportlab
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._footer(total)
                super().showPage()
            super().save()

        def _footer(self, total: int):
            font = FONT_NAME if _fonts_ready else "Times-Roman"
            self.saveState()
            self.setFont(font, 8)
            self.setFillColor(MUTED)
            y = MARGIN_BOTTOM - 9 * mm
            self.drawString(MARGIN_LEFT, y, footer_left)
            self.drawRightString(A4[0] - MARGIN_RIGHT, y, f"Стр. {self._pageNumber} из {total}")
            self.restoreState()

    return NumberedCanvas


def render(
    *,
    memo,
    author,
    template_schema: dict | None = None,
    sheet_rows: list[dict] | None = None,
    document: dict | None = None,
) -> bytes:
    """
    Собирает PDF. Передан `document` — документ печатается по образцу бланка,
    иначе таблицей полей. Передан `sheet_rows` — в конце добавляется лист
    согласования.
    """
    st = _styles()
    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_LEFT,
        rightMargin=MARGIN_RIGHT,
        topMargin=MARGIN_TOP,
        bottomMargin=MARGIN_BOTTOM,
        title=f"{memo.title} {memo.number or ''}".strip(),
        author=getattr(author, "full_name", "") or "",
        creator="УчетЭДО",
    )
    # Рамка без внутренних отступов. У SimpleDocTemplate они по 6 pt с
    # каждой стороны: текст вставал на 2 мм правее поля, а таблицы шириной
    # в полную полосу вылезали за рамку и стояли по полю — края документа
    # расходились.
    doc.addPageTemplates([PageTemplate(id="page", frames=[Frame(
        MARGIN_LEFT, MARGIN_BOTTOM, CONTENT_WIDTH, A4[1] - MARGIN_TOP - MARGIN_BOTTOM,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body",
    )])])

    if document and document.get("kind") == "letter":
        flow = _letter_flowables(memo=memo, document=document, st=st)
    else:
        flow = _memo_flowables(memo=memo, author=author, template_schema=template_schema, st=st)
    if sheet_rows:
        flow += _sheet_flowables(memo=memo, author=author, rows=sheet_rows, st=st)

    footer = f"УчетЭДО · рег. № {memo.number}" if memo.number else "УчетЭДО"
    doc.build(flow, canvasmaker=_numbered_canvas(footer))
    return buffer.getvalue()
