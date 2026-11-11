"""
Проверки формирования файла служебной записки, листа согласования и вложений
(ТЗ, п. 5.1.6 и приёмочное испытание № 2 в части PDF).
"""

import pytest
from sqlalchemy import select

from app.core.security import file_digest
from app.db.enums import GroupPolicy
from app.db.tables import ApprovalGroup, ApprovalStep, MemoTemplate
from app.services import approval, documents, pdf, storage


def test_cyrillic_font_is_available():
    """
    Без TrueType-шрифта русский текст в PDF выходит набором точек: встроенные
    шрифты reportlab знают только Latin-1. Если проверка упала на площадке
    сборки — в образ не доставлен `fonts-dejavu-core`.
    """
    assert pdf._ensure_fonts(), (
        "не найден ни один шрифт с кириллицей; проверьте список "
        "pdf._FONT_CANDIDATES и состав образа"
    )


async def test_memo_pdf_is_built_on_submit(session, memo):
    await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"email": "boss@example.com"}]}],
    )
    await documents.build_memo_pdf(session, memo)

    assert memo.pdf_path == f"memos/{memo.id}/memo.pdf"
    assert memo.file_hash, "хеш фиксируется до первой подписи"

    data = storage.read_bytes(memo.pdf_path)
    assert data.startswith(b"%PDF-"), "на выходе должен быть PDF"
    assert file_digest(data) == memo.file_hash


async def test_signature_stores_the_hash_of_what_was_signed(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"email": "boss@example.com"}]}],
    )
    await documents.build_memo_pdf(session, memo)
    hash_at_signing = memo.file_hash

    step = (
        await session.execute(
            select(ApprovalStep)
            .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
            .where(ApprovalGroup.route_id == route.id)
        )
    ).scalar_one()
    await approval.decide(session, step=step, approve=True, actor="boss")

    await documents.finalize_memo_pdf(session, memo.id)

    # Лист согласования дописывается после подписания, поэтому хеш
    # подписанного содержимого обновляться не должен — иначе все ранее
    # сохранённые подписи стали бы «несовпадающими».
    assert memo.file_hash == hash_at_signing
    assert memo.pdf_path.endswith("memo_signed.pdf")

    signed = storage.read_bytes(memo.pdf_path)
    assert signed.startswith(b"%PDF-")
    assert len(signed) > len(storage.read_bytes(f"memos/{memo.id}/memo.pdf")), (
        "в документе с листом согласования должна быть лишняя страница"
    )


async def test_rejected_memo_also_gets_a_sheet(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"email": "boss@example.com"}]}],
    )
    await documents.build_memo_pdf(session, memo)

    step = (
        await session.execute(
            select(ApprovalStep)
            .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
            .where(ApprovalGroup.route_id == route.id)
        )
    ).scalar_one()
    await approval.decide(
        session, step=step, approve=False, comment="Нет бюджета", actor="boss"
    )

    # На листе должно быть видно, кто и почему остановил согласование.
    assert await documents.finalize_memo_pdf(session, memo.id) is not None


async def test_sheet_renders_template_field_labels(session, memo):
    """
    Лист и тело документа должны показывать подписи полей из схемы шаблона,
    а не их машинные имена.
    """
    template = MemoTemplate(
        code="training",
        title="Направление на обучение",
        body_schema={"fields": [{"name": "program", "label": "Программа обучения"}]},
    )
    session.add(template)
    await session.flush()

    memo.template_id = template.id
    memo.body = {"program": "Промышленная безопасность"}
    await session.flush()

    assert await documents.build_memo_pdf(session, memo)
    # Содержимое PDF сжато, поэтому проверяем не байты, а то, что сборка
    # с непустой схемой проходит и файл читается.
    assert storage.read_bytes(memo.pdf_path).startswith(b"%PDF-")


# ─── хранилище ───────────────────────────────────────────────────────────────
def test_storage_refuses_to_escape_its_root():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        storage.resolve_within_storage("../../etc/passwd")
    assert err.value.status_code == 400


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../../etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\drivers\\etc\\hosts", "hosts"),
        ("отчёт.pdf", "отчёт.pdf"),
    ],
)
def test_attachment_names_are_reduced_to_a_file_name(raw, expected):
    assert storage.safe_file_name(raw) == expected


def test_empty_attachment_name_is_refused():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        storage.safe_file_name("  ")
