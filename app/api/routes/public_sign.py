"""
Подписание по одноразовой ссылке — без авторизации.

Ключевое требование заказчика: согласующий не проходит регистрацию. Порядок
проверок и устройство модуля повторяют `public_share.py` из EDMS, включая
правило «держатель ссылки видит один документ и ничего кроме него».

Безопасность здесь опирается на три свойства токена: он выдан
криптографически стойким генератором, он одноразовый, у него ограничен срок.
Это сознательно слабее входа по паролю — риск Р5 в ТЗ, закрываемый
организационно, внутренним приказом о применении простой электронной подписи.
"""

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import ClientInfo, SessionDep
from app.core.exceptions import http_404
from app.db.enums import MemoStatus, SignMethod
from app.db.tables import ApprovalGroup, ApprovalRoute, Attachment, Memo, User
from app.schemas import PublicAttachmentOut, PublicDecisionRequest, PublicMemoOut
from app.services import approval, documents, storage
from app.services.mail import send_outbox

router = APIRouter(tags=["Подписание по ссылке"])


async def _memo_for_step(session, step) -> tuple[Memo, User]:
    group = (
        await session.execute(
            select(ApprovalGroup).where(ApprovalGroup.id == step.group_id)
        )
    ).scalar_one()
    route = (
        await session.execute(
            select(ApprovalRoute).where(ApprovalRoute.id == group.route_id)
        )
    ).scalar_one()
    memo = (
        await session.execute(select(Memo).where(Memo.id == route.memo_id))
    ).scalar_one()
    author = (
        await session.execute(select(User).where(User.id == memo.author_id))
    ).scalar_one()
    return memo, author


@router.get("/sign/{token}", response_model=PublicMemoOut)
async def open_for_signing(token: str, session: SessionDep) -> PublicMemoOut:
    step = await approval.resolve_sign_token(session, token)
    memo, author = await _memo_for_step(session, step)

    attachments = (
        await session.execute(
            select(Attachment).where(Attachment.memo_id == memo.id).order_by(Attachment.id)
        )
    ).scalars().all()

    return PublicMemoOut(
        number=memo.number,
        title=memo.title,
        body=memo.body,
        author_name=author.full_name,
        author_position=author.position,
        created_at=memo.created_at,
        submitted_at=memo.submitted_at,
        signer_name=step.signer_name,
        signer_position=step.signer_position,
        expires_at=step.token_expires_at,
        attachments=[
            PublicAttachmentOut(id=a.id, file_name=a.file_name, size=a.size)
            for a in attachments
        ],
        fields=await documents.labeled_body(session, memo),
    )


@router.get("/sign/{token}/pdf")
async def memo_pdf_by_link(token: str, session: SessionDep) -> FileResponse:
    """
    Сам документ по ссылке подписания.

    Подписант должен видеть ровно тот файл, хеш которого сохранит его
    подпись, а не пересказ полей на странице. После решения ссылка гаснет
    вместе с этим адресом: `resolve_sign_token` проверяется на каждый запрос.
    """
    step = await approval.resolve_sign_token(session, token)
    memo, _author = await _memo_for_step(session, step)

    if not memo.pdf_path:
        raise http_404("Файл документа не сформирован")
    path = storage.resolve_within_storage(memo.pdf_path)
    if not path.is_file():
        raise http_404("Файл отсутствует в хранилище")

    # inline: браузер открывает документ, а не сохраняет — на телефоне это
    # разница между «посмотрел и подписал» и «ищи файл в загрузках».
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/sign/{token}/attachments/{attachment_id}")
async def download_attachment_by_link(
    token: str, attachment_id: int, session: SessionDep
) -> FileResponse:
    """
    Вложение по той же ссылке подписания.

    Токен проверяется заново, и вложение обязано принадлежать именно этой
    записке: без второй проверки подмена идентификатора в адресе открыла бы
    доступ к файлам чужих документов — та же ошибка, от которой защищается
    `public_share.py` в EDMS.
    """
    step = await approval.resolve_sign_token(session, token)
    memo, _author = await _memo_for_step(session, step)

    attachment = (
        await session.execute(
            select(Attachment)
            .where(Attachment.id == attachment_id)
            .where(Attachment.memo_id == memo.id)
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise http_404("Вложение не найдено")

    path = storage.resolve_within_storage(attachment.file_path)
    if not path.is_file():
        raise http_404("Файл отсутствует в хранилище")

    return FileResponse(path, filename=attachment.file_name)


@router.post("/sign/{token}/decide")
async def decide_by_link(
    token: str,
    data: PublicDecisionRequest,
    session: SessionDep,
    client: ClientInfo,
    background: BackgroundTasks,
) -> dict:
    step = await approval.resolve_sign_token(session, token)
    memo, author = await _memo_for_step(session, step)

    result = await approval.decide(
        session,
        step=step,
        approve=data.approve,
        comment=data.comment,
        # В журнале аудита подписант по ссылке опознаётся адресом почты:
        # учётной записи у него может не быть вовсе.
        actor=step.signer_email,
        method=SignMethod.link,
        signer_name=data.signer_name,
        ip=client["ip"],
        user_agent=client["user_agent"],
    )

    if result.memo_status in (MemoStatus.approved, MemoStatus.rejected):
        await documents.finalize_memo_pdf(session, result.memo_id)

    background.add_task(send_outbox, result.outbox, author_email=author.email)

    return {
        "decision": "approved" if data.approve else "rejected",
        "memo_status": result.memo_status,
        "message": (
            "Решение принято. Благодарим, больше действий не требуется."
            if data.approve
            else "Служебная записка отклонена, автор уведомлён."
        ),
    }
