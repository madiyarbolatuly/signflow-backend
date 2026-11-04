"""
Служебные записки: черновики, запуск маршрута, перечни заданий, решения
изнутри системы.
"""

from fastapi import APIRouter, BackgroundTasks, File, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import ClientInfo, CurrentUser, SessionDep
from app.core.exceptions import http_403, http_404, http_409
from app.core.security import file_digest
from app.db.enums import MemoStatus, SignMethod, StepStatus
from app.db.tables import (
    ApprovalGroup,
    ApprovalRoute,
    ApprovalStep,
    Attachment,
    Memo,
    User,
)
from app.schemas import (
    AttachmentOut,
    DecisionRequest,
    MemoCreate,
    MemoDetailOut,
    MemoOut,
    MemoUpdate,
    SubmitRequest,
)
from app.services import approval, documents, storage
from app.services.mail import send_outbox

#: Предел размера вложения. Без него один запрос способен занять диск.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

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

    # Файл и его хеш нужны до первой подписи: подпись сохраняет хеш того,
    # что подписывали.
    await documents.build_memo_pdf(session, memo)

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

    if result.memo_status in (MemoStatus.approved, MemoStatus.rejected):
        await documents.finalize_memo_pdf(session, result.memo_id)

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


@router.get("/{memo_id}/pdf")
async def memo_pdf(memo_id: int, session: SessionDep, user: CurrentUser) -> FileResponse:
    """Файл служебной записки. После завершения маршрута — с листом согласования."""
    memo = await memo_detail(memo_id, session, user)
    if not memo.pdf_path:
        raise http_404("Файл ещё не сформирован: маршрут не запускался")

    path = storage.resolve_within_storage(memo.pdf_path)
    if not path.is_file():
        raise http_404("Файл отсутствует в хранилище")

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"sluzhebnaya-zapiska-{memo.number or memo.id}.pdf",
    )


@router.post(
    "/{memo_id}/attachments",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_attachment(
    memo_id: int,
    session: SessionDep,
    user: CurrentUser,
    file: UploadFile = File(...),
) -> Attachment:
    memo = await _get_own_memo(session, memo_id, user)
    if memo.status != MemoStatus.draft:
        # После запуска маршрута состав вложений меняться не должен: иначе
        # подписант согласует один комплект, а в деле окажется другой.
        raise http_409("Прикреплять файлы можно только к черновику")

    data = await file.read()
    if not data:
        raise http_409("Файл пуст")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise http_409(
            f"Файл больше допустимых {MAX_ATTACHMENT_BYTES // (1024 * 1024)} МБ"
        )

    name = storage.safe_file_name(file.filename or "file")
    attachment = Attachment(
        memo_id=memo.id,
        file_name=name,
        file_path=f"memos/{memo.id}/attachments/{name}",
        size=len(data),
        file_hash=file_digest(data),
    )
    storage.save_bytes(attachment.file_path, data)
    session.add(attachment)
    await session.flush()
    return attachment


@router.get("/{memo_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    memo_id: int, session: SessionDep, user: CurrentUser
) -> list[Attachment]:
    await memo_detail(memo_id, session, user)
    rows = await session.execute(
        select(Attachment).where(Attachment.memo_id == memo_id).order_by(Attachment.id)
    )
    return list(rows.scalars().all())


@router.get("/{memo_id}/attachments/{attachment_id}")
async def download_attachment(
    memo_id: int, attachment_id: int, session: SessionDep, user: CurrentUser
) -> FileResponse:
    await memo_detail(memo_id, session, user)
    attachment = (
        await session.execute(
            select(Attachment)
            .where(Attachment.id == attachment_id)
            .where(Attachment.memo_id == memo_id)
        )
    ).scalar_one_or_none()
    if attachment is None:
        raise http_404("Вложение не найдено")

    path = storage.resolve_within_storage(attachment.file_path)
    if not path.is_file():
        raise http_404("Файл отсутствует в хранилище")

    return FileResponse(path, filename=attachment.file_name)
