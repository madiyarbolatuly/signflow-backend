"""
Служебные записки: черновики, запуск маршрута, перечни заданий, решения
изнутри системы.
"""

from fastapi import APIRouter, BackgroundTasks, status
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import ClientInfo, CurrentUser, SessionDep
from app.core.exceptions import http_403, http_404, http_409
from app.db.enums import MemoStatus, SignMethod, StepStatus
from app.db.tables import ApprovalGroup, ApprovalRoute, ApprovalStep, Memo, User
from app.schemas import (
    DecisionRequest,
    MemoCreate,
    MemoDetailOut,
    MemoOut,
    MemoUpdate,
    SubmitRequest,
)
from app.services import approval
from app.services.mail import send_outbox

router = APIRouter(tags=["Служебные записки"])


async def _get_own_memo(session, memo_id: int, user: User) -> Memo:
    memo = (
        await session.execute(select(Memo).where(Memo.id == memo_id))
    ).scalar_one_or_none()
    if memo is None:
        raise http_404("Служебная записка не найдена")
    if memo.author_id != user.id:
        raise http_403("Доступ только к собственным служебным запискам")
    return memo


@router.post("", response_model=MemoOut, status_code=status.HTTP_201_CREATED)
async def create_memo(data: MemoCreate, session: SessionDep, user: CurrentUser) -> Memo:
    memo = Memo(
        author_id=user.id,
        template_id=data.template_id,
        title=data.title,
        body=data.body,
        status=MemoStatus.draft,
    )
    session.add(memo)
    await session.flush()
    return memo


@router.put("/{memo_id}", response_model=MemoOut)
async def update_memo(
    memo_id: int, data: MemoUpdate, session: SessionDep, user: CurrentUser
) -> Memo:
    memo = await _get_own_memo(session, memo_id, user)
    if memo.status != MemoStatus.draft:
        raise http_409("Редактировать можно только черновик")

    if data.title is not None:
        memo.title = data.title
    if data.body is not None:
        memo.body = data.body
    await session.flush()
    return memo


@router.post("/{memo_id}/submit", response_model=MemoDetailOut)
async def submit_memo(
    memo_id: int,
    data: SubmitRequest,
    session: SessionDep,
    user: CurrentUser,
    background: BackgroundTasks,
) -> Memo:
    """Запускает маршрут согласования и рассылает задания первого уровня."""
    memo = await _get_own_memo(session, memo_id, user)

    spec = [
        {
            "policy": level.policy,
            "signers": [s.model_dump(exclude_none=True) for s in level.signers],
        }
        for level in data.route
    ]
    route = await approval.build_route(session, memo=memo, spec=spec)
    outbox = approval.collect_outbox(route)

    # Отправка после ответа и, что важнее, после фиксации транзакции: письмо о
    # событии, которое затем откатилось, хуже отсутствия письма.
    background.add_task(send_outbox, outbox, author_email=user.email)

    return await _memo_detail(session, memo.id)


@router.post("/{memo_id}/recall", response_model=MemoOut)
async def recall_memo(memo_id: int, session: SessionDep, user: CurrentUser) -> Memo:
    memo = await _get_own_memo(session, memo_id, user)
    await approval.recall(session, memo=memo, actor=user.id)
    return memo


@router.get("/outgoing", response_model=list[MemoOut])
async def outgoing(session: SessionDep, user: CurrentUser) -> list[Memo]:
    """«Мои служебные записки»."""
    rows = await session.execute(
        select(Memo)
        .where(Memo.author_id == user.id)
        .order_by(Memo.created_at.desc())
    )
    return list(rows.scalars().all())


@router.get("/incoming", response_model=list[MemoOut])
async def incoming(session: SessionDep, user: CurrentUser) -> list[Memo]:
    """
    «Мне на подпись».

    Задание ищется и по идентификатору сотрудника, и по адресу почты: автор
    мог указать согласующего адресом, не выбрав его из справочника, и такое
    задание всё равно должно появиться в перечне, если человек заходит в
    систему под своей учётной записью.
    """
    rows = await session.execute(
        select(Memo)
        .join(ApprovalRoute, ApprovalRoute.memo_id == Memo.id)
        .join(ApprovalGroup, ApprovalGroup.route_id == ApprovalRoute.id)
        .join(ApprovalStep, ApprovalStep.group_id == ApprovalGroup.id)
        .where(ApprovalStep.status == StepStatus.active)
        .where(
            or_(
                ApprovalStep.signer_user_id == user.id,
                ApprovalStep.signer_email == user.email,
            )
        )
        .order_by(Memo.submitted_at.desc())
    )
    return list(rows.scalars().unique().all())


async def _memo_detail(session, memo_id: int) -> Memo:
    memo = (
        await session.execute(
            select(Memo)
            .where(Memo.id == memo_id)
            .options(
                selectinload(Memo.route)
                .selectinload(ApprovalRoute.groups)
                .selectinload(ApprovalGroup.steps)
            )
        )
    ).scalar_one_or_none()
    if memo is None:
        raise http_404("Служебная записка не найдена")
    return memo


@router.get("/{memo_id}", response_model=MemoDetailOut)
async def memo_detail(memo_id: int, session: SessionDep, user: CurrentUser) -> Memo:
    memo = await _memo_detail(session, memo_id)

    # Видеть карточку вправе автор, администратор и любой участник маршрута.
    if memo.author_id != user.id and user.role != "admin":
        participates = (
            await session.execute(
                select(ApprovalStep.id)
                .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
                .join(ApprovalRoute, ApprovalGroup.route_id == ApprovalRoute.id)
                .where(ApprovalRoute.memo_id == memo_id)
                .where(
                    or_(
                        ApprovalStep.signer_user_id == user.id,
                        ApprovalStep.signer_email == user.email,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if participates is None:
            raise http_403("Нет доступа к этой служебной записке")

    return memo


@router.post("/steps/{step_id}/decide", response_model=MemoDetailOut)
async def decide_step(
    step_id: int,
    data: DecisionRequest,
    session: SessionDep,
    user: CurrentUser,
    client: ClientInfo,
    background: BackgroundTasks,
) -> Memo:
    """Решение изнутри системы, под своей учётной записью."""
    step = (
        await session.execute(select(ApprovalStep).where(ApprovalStep.id == step_id))
    ).scalar_one_or_none()
    if step is None:
        raise http_404("Задание не найдено")

    if step.signer_user_id != user.id and step.signer_email != user.email:
        raise http_403("Это задание адресовано другому сотруднику")

    result = await approval.decide(
        session,
        step=step,
        approve=data.approve,
        comment=data.comment,
        actor=user.id,
        method=SignMethod.internal,
        ip=client["ip"],
        user_agent=client["user_agent"],
    )

    author_email = (
        await session.execute(
            select(User.email)
            .join(Memo, Memo.author_id == User.id)
            .join(ApprovalRoute, ApprovalRoute.memo_id == Memo.id)
            .join(ApprovalGroup, ApprovalGroup.route_id == ApprovalRoute.id)
            .where(ApprovalGroup.id == step.group_id)
        )
    ).scalar_one_or_none()

    background.add_task(send_outbox, result.outbox, author_email=author_email)

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
    return await _memo_detail(session, route.memo_id)
