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
from sqlalchemy import select

from app.api.deps import ClientInfo, SessionDep
from app.db.enums import SignMethod
from app.db.tables import ApprovalGroup, ApprovalRoute, Memo, User
from app.schemas import PublicDecisionRequest, PublicMemoOut
from app.services import approval
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

    return PublicMemoOut(
        number=memo.number,
        title=memo.title,
        body=memo.body,
        author_name=author.full_name,
        created_at=memo.created_at,
        signer_name=step.signer_name,
        signer_position=step.signer_position,
        expires_at=step.token_expires_at,
    )


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
