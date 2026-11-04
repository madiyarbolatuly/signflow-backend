"""
Сборка файла служебной записки: когда формировать PDF и что в него попадает.

Вынесено из обработчиков, потому что вызывается из трёх мест: при запуске
маршрута и при завершении решения — изнутри системы и по ссылке.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import file_digest
from app.db.enums import MemoStatus
from app.db.tables import ApprovalGroup, ApprovalRoute, ApprovalStep, Memo, MemoTemplate, User
from app.services import pdf, storage


async def _template_schema(session: AsyncSession, memo: Memo) -> dict | None:
    if memo.template_id is None:
        return None
    template = (
        await session.execute(
            select(MemoTemplate).where(MemoTemplate.id == memo.template_id)
        )
    ).scalar_one_or_none()
    return template.body_schema if template else None


async def _sheet_rows(session: AsyncSession, memo: Memo) -> list[dict]:
    rows = await session.execute(
        select(ApprovalGroup.order_no, ApprovalStep)
        .join(ApprovalStep, ApprovalStep.group_id == ApprovalGroup.id)
        .join(ApprovalRoute, ApprovalGroup.route_id == ApprovalRoute.id)
        .where(ApprovalRoute.memo_id == memo.id)
        .order_by(ApprovalGroup.order_no, ApprovalStep.id)
    )
    return [
        {
            "order_no": order_no,
            "signer_name": step.signer_name,
            "signer_email": step.signer_email,
            "signer_position": step.signer_position,
            "status": step.status,
            "decided_at": step.decided_at,
            "comment": step.comment,
        }
        for order_no, step in rows.all()
    ]


async def labeled_body(session: AsyncSession, memo: Memo) -> list[dict]:
    """
    Поля записки с человеческими подписями из схемы шаблона.

    Одна точка правды для PDF, карточки автора и страницы подписания: иначе
    в документе поле называется «Программа обучения», а на экране — `program`.
    Поля, которых нет в схеме (записка без шаблона или шаблон изменили после
    отправки), показываются под своим ключом, а не скрываются.
    """
    schema = await _template_schema(session, memo)
    labels = {
        f.get("name"): f.get("label", f.get("name", ""))
        for f in (schema or {}).get("fields", [])
        if f.get("name")
    }
    order = list(labels.keys())

    def rank(name: str) -> tuple[int, int]:
        # Порядок полей задаёт шаблон; «лишние» уходят в конец.
        return (0, order.index(name)) if name in order else (1, 0)

    return [
        {"name": name, "label": labels.get(name, name), "value": value}
        for name, value in sorted((memo.body or {}).items(), key=lambda kv: rank(kv[0]))
    ]


async def build_memo_pdf(session: AsyncSession, memo: Memo) -> str:
    """
    Формирует PDF служебной записки и запоминает его хеш.

    Хеш фиксируется здесь, при запуске маршрута, и именно его сохраняет
    каждая подпись: так расхождение с текущим файлом доказуемо (ТЗ, п. 5.1.6).
    Лист согласования при этом не добавляется — подписей ещё нет.
    """
    author = (
        await session.execute(select(User).where(User.id == memo.author_id))
    ).scalar_one()

    data = pdf.render(
        memo=memo,
        author=author,
        template_schema=await _template_schema(session, memo),
    )

    relative = f"memos/{memo.id}/memo.pdf"
    storage.save_bytes(relative, data)
    memo.pdf_path = relative
    memo.file_hash = file_digest(data)
    await session.flush()
    return relative


async def finalize_memo_pdf(session: AsyncSession, memo_id: int) -> str | None:
    """
    Дописывает к документу лист согласования после завершения маршрута.

    Вызывается при любом исходе, кроме отзыва: отклонённая записка тоже
    получает лист — на нём видно, кто и почему остановил согласование.

    `file_hash` здесь сознательно **не** пересчитывается: он относится к
    подписанному содержимому, а лист согласования добавляется уже после
    подписания. Перезапись хеша сделала бы все сохранённые подписи
    «несовпадающими».
    """
    memo = (
        await session.execute(select(Memo).where(Memo.id == memo_id))
    ).scalar_one_or_none()
    if memo is None or memo.status not in (MemoStatus.approved, MemoStatus.rejected):
        return None

    author = (
        await session.execute(select(User).where(User.id == memo.author_id))
    ).scalar_one()

    data = pdf.render(
        memo=memo,
        author=author,
        template_schema=await _template_schema(session, memo),
        sheet_rows=await _sheet_rows(session, memo),
    )

    relative = f"memos/{memo.id}/memo_signed.pdf"
    storage.save_bytes(relative, data)
    memo.pdf_path = relative
    await session.flush()
    return relative
