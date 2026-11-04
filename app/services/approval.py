"""
Машина состояний маршрута согласования.

Правила, которые здесь поддерживаются (ТЗ, п. 5.1.3):

  * уровни выполняются строго последовательно — следующий активируется только
    после закрытия предыдущего;
  * шаги внутри уровня активируются одновременно (параллельное согласование);
  * политика уровня `all` требует решения всех участников, `any` — одного;
  * отклонение на любом уровне останавливает маршрут целиком;
  * автор вправе отозвать записку до окончательного решения;
  * повторное решение по одному шагу невозможно, в том числе при одновременном
    обращении нескольких участников одного уровня (риск Р6).

Весь модуль работает внутри транзакции вызывающей стороны и сам `commit()`
не делает — транзакцией владеет зависимость `get_async_session`.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from ulid import ULID

from app.core.config import settings
from app.core.exceptions import http_400, http_403, http_409, http_410
from app.core.security import new_sign_token
from app.db.enums import (
    GroupPolicy,
    GroupStatus,
    MemoStatus,
    RouteStatus,
    SignMethod,
    StepStatus,
)
from app.db.tables import (
    ApprovalGroup,
    ApprovalRoute,
    ApprovalStep,
    AuditLog,
    Memo,
    Notification,
    Signature,
    User,
)


# ─── исходящие сообщения ─────────────────────────────────────────────────────
@dataclass
class OutboxItem:
    """
    Письмо, которое нужно отправить после фиксации транзакции.

    Сервис не отправляет почту сам: отправка внутри транзакции означала бы
    письмо о событии, которое затем откатилось. Обработчик складывает эти
    элементы в `BackgroundTasks` уже после коммита.
    """

    to: str
    subject: str
    body: str


@dataclass
class DecisionResult:
    step: ApprovalStep
    route_status: str
    memo_status: str
    #: Нужен вызывающей стороне, чтобы дописать лист согласования, не
    #: восстанавливая путь шаг → уровень → маршрут → записка заново.
    memo_id: int = 0
    outbox: list[OutboxItem] = field(default_factory=list)
    #: Уровень, активированный этим решением. None — маршрут не продвинулся.
    activated_group_no: int | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: datetime | None) -> datetime | None:
    """
    Приводит значение из базы к осведомлённому о часовом поясе виду.

    SQLite хранит `DateTime(timezone=True)` без смещения и возвращает наивный
    `datetime`, сравнение которого с `_now()` падает с TypeError. В Postgres
    смещение сохраняется, и преобразование ничего не меняет.
    """
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _supports_row_locks(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind.dialect.name not in {"sqlite"}


async def _lock_route(session: AsyncSession, route_id: int) -> ApprovalRoute:
    """
    Читает маршрут под блокировкой строки.

    Это защита от состязания на параллельном уровне: двое участников жмут
    «Согласовать» одновременно, оба видят, что их решение закрывает уровень,
    и оба активируют следующий — следующий уровень получает по два задания и
    два письма. Блокировка строки маршрута упорядочивает решения (риск Р6).

    SQLite не поддерживает SELECT ... FOR UPDATE, поэтому там блокировка не
    запрашивается: в тестах конкуренции нет, а запрос с FOR UPDATE просто не
    выполнился бы.
    """
    stmt = select(ApprovalRoute).where(ApprovalRoute.id == route_id)
    if _supports_row_locks(session):
        stmt = stmt.with_for_update()
    route = (await session.execute(stmt)).scalar_one_or_none()
    if route is None:
        raise http_409("Маршрут согласования не найден")
    return route


async def _load_groups(session: AsyncSession, route_id: int) -> list[ApprovalGroup]:
    stmt = (
        select(ApprovalGroup)
        .where(ApprovalGroup.route_id == route_id)
        .options(selectinload(ApprovalGroup.steps))
        .order_by(ApprovalGroup.order_no)
    )
    return list((await session.execute(stmt)).scalars().all())


# ─── построение маршрута ─────────────────────────────────────────────────────
async def build_route(
    session: AsyncSession,
    *,
    memo: Memo,
    spec: list[dict],
) -> ApprovalRoute:
    """
    Создаёт маршрут по описанию `spec` и активирует первый уровень.

    `spec` — список уровней в порядке прохождения::

        [{"policy": "all", "signers": [{"email": ..., "user_id": ...}, ...]},
         {"policy": "any", "signers": [...]}]

    Проверки здесь, а не в схеме запроса, потому что часть из них требует
    обращения к базе (существование пользователя) либо затрагивает маршрут
    целиком (непустой состав уровня).
    """
    if memo.status != MemoStatus.draft:
        raise http_409("Маршрут можно запустить только для черновика")
    if not spec:
        raise http_400("Маршрут должен содержать хотя бы один уровень")

    route = ApprovalRoute(memo_id=memo.id, status=RouteStatus.active)
    session.add(route)
    await session.flush()

    for order_no, level in enumerate(spec, start=1):
        policy = level.get("policy", GroupPolicy.all_of)
        if policy not in GroupPolicy.ALL:
            raise http_400(f"Неизвестная политика уровня: {policy}")

        signers = level.get("signers") or []
        if not signers:
            raise http_400(f"Уровень {order_no} не содержит согласующих")

        # Сотрудника достаточно указать идентификатором: адрес и должность
        # берутся из справочника. Требовать почту ещё и во входных данных
        # значило бы дать автору возможность разойтись со справочником.
        resolved: list[tuple[dict, User | None, str]] = []
        for signer in signers:
            user: User | None = None
            if signer.get("user_id"):
                user = (
                    await session.execute(
                        select(User).where(User.id == signer["user_id"])
                    )
                ).scalar_one_or_none()
                if user is None:
                    raise http_400(f"Сотрудник {signer['user_id']} не найден")

            email = (user.email if user else str(signer.get("email", ""))).strip().lower()
            if not email:
                raise http_400(
                    f"Уровень {order_no}: у согласующего не указан ни сотрудник, "
                    f"ни адрес почты"
                )
            resolved.append((signer, user, email))

        emails = [email for _, _, email in resolved]
        if len(set(emails)) != len(emails):
            raise http_400(f"Уровень {order_no}: один согласующий указан дважды")

        group = ApprovalGroup(
            route_id=route.id,
            order_no=order_no,
            policy=policy,
            status=GroupStatus.pending,
        )
        session.add(group)
        await session.flush()

        for signer, user, email in resolved:
            session.add(
                ApprovalStep(
                    group_id=group.id,
                    signer_user_id=user.id if user else None,
                    signer_email=user.email if user else email,
                    signer_name=signer.get("name") or (user.full_name if user else None),
                    signer_position=signer.get("position") or (user.position if user else None),
                    status=StepStatus.pending,
                )
            )

    await session.flush()

    memo.status = MemoStatus.submitted
    memo.submitted_at = _now()
    if memo.number is None:
        memo.number = str(ULID())

    outbox: list[OutboxItem] = []
    groups = await _load_groups(session, route.id)
    await _activate_group(session, memo=memo, group=groups[0], outbox=outbox)
    route.current_group_no = groups[0].order_no
    memo.status = MemoStatus.in_review

    session.add(
        AuditLog(
            entity_type="memo",
            entity_id=str(memo.id),
            action="route_started",
            actor=memo.author_id,
            payload={"levels": len(spec), "number": memo.number},
        )
    )

    # Маршрут отдаётся вызывающей стороне вместе с письмами: обработчик обязан
    # забрать `outbox` через `collect_outbox`, иначе уведомления не уйдут.
    route._outbox = outbox  # type: ignore[attr-defined]
    return route


def collect_outbox(route: ApprovalRoute) -> list[OutboxItem]:
    return getattr(route, "_outbox", [])


async def _activate_group(
    session: AsyncSession,
    *,
    memo: Memo,
    group: ApprovalGroup,
    outbox: list[OutboxItem],
) -> None:
    """Активирует уровень: выдаёт токены, ставит задания, готовит письма."""
    group.status = GroupStatus.active
    expires = _now() + timedelta(days=settings.sign_link_ttl_days)

    for step in group.steps:
        if step.status != StepStatus.pending:
            continue
        step.status = StepStatus.active
        step.action_token = new_sign_token()
        step.token_expires_at = expires

        link = f"{settings.frontend_url.rstrip('/')}/sign/{step.action_token}"
        outbox.append(
            OutboxItem(
                to=step.signer_email,
                subject=f"Служебная записка {memo.number or ''} на согласование".strip(),
                body=(
                    f"Вам направлена служебная записка на согласование.\n\n"
                    f"Тема: {memo.title}\n"
                    f"Автор: {memo.author_id}\n\n"
                    f"Открыть и принять решение: {link}\n\n"
                    f"Ссылка одноразовая и действует до "
                    f"{expires.strftime('%d.%m.%Y')}."
                ),
            )
        )

        if step.signer_user_id:
            session.add(
                Notification(
                    user_id=step.signer_user_id,
                    message=f"Служебная записка «{memo.title}» ожидает вашего решения",
                    link=f"/memos/{memo.id}",
                )
            )

        session.add(
            AuditLog(
                entity_type="step",
                entity_id=str(step.id),
                action="step_activated",
                actor="system",
                payload={"email": step.signer_email, "group_no": group.order_no},
            )
        )


# ─── принятие решения ────────────────────────────────────────────────────────
async def decide(
    session: AsyncSession,
    *,
    step: ApprovalStep,
    approve: bool,
    actor: str,
    comment: str | None = None,
    method: str = SignMethod.link,
    signer_name: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> DecisionResult:
    """
    Фиксирует решение по шагу и продвигает маршрут.

    `actor` — кто принял решение в терминах журнала аудита: идентификатор
    пользователя либо адрес почты подписанта по ссылке.
    """
    if not approve and not (comment or "").strip():
        # Отклонение без причины бесполезно автору: он не знает, что править.
        raise http_400("При отклонении необходимо указать причину")

    group = (
        await session.execute(
            select(ApprovalGroup).where(ApprovalGroup.id == step.group_id)
        )
    ).scalar_one()

    route = await _lock_route(session, group.route_id)

    # Состояние перечитывается уже под блокировкой: до её получения другой
    # участник мог закрыть уровень или отклонить маршрут.
    await session.refresh(step)

    # Порядок проверок важен: состояние шага конкретнее состояния маршрута.
    # Отозванный маршрут гасит свои задания, поэтому «задание больше не
    # требуется» участник увидит и в этом случае.
    if step.status != StepStatus.active:
        raise http_409(_step_refusal(step))
    if route.status != RouteStatus.active:
        raise http_409(f"Маршрут уже завершён со статусом «{route.status}»")

    expires = _as_aware(step.token_expires_at)
    if method == SignMethod.link and expires is not None and expires < _now():
        raise http_410("Срок действия ссылки истёк")

    memo = (
        await session.execute(select(Memo).where(Memo.id == route.memo_id))
    ).scalar_one()

    # ─── решение по шагу ─────────────────────────────────────────────────────
    step.status = StepStatus.approved if approve else StepStatus.rejected
    step.comment = (comment or "").strip() or None
    step.decided_at = _now()
    if signer_name and not step.signer_name:
        step.signer_name = signer_name
    if method == SignMethod.link:
        step.token_used_at = step.decided_at

    session.add(
        Signature(
            step_id=step.id,
            file_hash=memo.file_hash,
            method=method,
            evidence={
                "actor": actor,
                "ip": ip,
                "user_agent": user_agent,
                "decision": "approved" if approve else "rejected",
            },
        )
    )
    session.add(
        AuditLog(
            entity_type="step",
            entity_id=str(step.id),
            action="approved" if approve else "rejected",
            actor=actor,
            ip=ip,
            user_agent=user_agent,
            payload={"memo_id": memo.id, "comment": step.comment},
        )
    )

    outbox: list[OutboxItem] = []
    activated: int | None = None
    groups = await _load_groups(session, route.id)
    current = next(g for g in groups if g.id == group.id)

    if not approve:
        _fail_route(session, route=route, memo=memo, failed_group=current, groups=groups)
        outbox.append(_author_letter(memo, approved=False, comment=step.comment))
    else:
        if _group_satisfied(current):
            current.status = GroupStatus.satisfied
            _release_siblings(current)

            nxt = next(
                (g for g in groups if g.order_no > current.order_no),
                None,
            )
            if nxt is not None:
                await _activate_group(session, memo=memo, group=nxt, outbox=outbox)
                route.current_group_no = nxt.order_no
                activated = nxt.order_no
            else:
                route.status = RouteStatus.approved
                route.completed_at = _now()
                route.current_group_no = None
                memo.status = MemoStatus.approved
                memo.completed_at = route.completed_at
                outbox.append(_author_letter(memo, approved=True))
                session.add(
                    AuditLog(
                        entity_type="memo",
                        entity_id=str(memo.id),
                        action="route_approved",
                        actor="system",
                        payload={"number": memo.number},
                    )
                )

    await session.flush()
    return DecisionResult(
        step=step,
        route_status=route.status,
        memo_status=memo.status,
        memo_id=memo.id,
        outbox=outbox,
        activated_group_no=activated,
    )


def _step_refusal(step: ApprovalStep) -> str:
    """Внятная причина отказа вместо общего «шаг недоступен»."""
    return {
        StepStatus.approved: "Решение по этому заданию уже принято",
        StepStatus.rejected: "Решение по этому заданию уже принято",
        StepStatus.not_required: "Задание больше не требуется: решение принято другим участником",
        StepStatus.expired: "Срок действия ссылки истёк",
        StepStatus.pending: "Очередь этого уровня ещё не наступила",
    }.get(step.status, "Задание недоступно")


def _group_satisfied(group: ApprovalGroup) -> bool:
    """
    Выполнено ли условие закрытия уровня.

    Политика `any`: достаточно одного согласования. Политика `all`: решение
    должны принять все участники, отсутствие хотя бы одного оставляет уровень
    активным.
    """
    approved = [s for s in group.steps if s.status == StepStatus.approved]
    if group.policy == GroupPolicy.any_of:
        return len(approved) >= 1
    undecided = [
        s for s in group.steps if s.status in (StepStatus.active, StepStatus.pending)
    ]
    return not undecided and len(approved) == len(group.steps)


def _release_siblings(group: ApprovalGroup) -> None:
    """
    Закрывает задания, которые больше не нужны.

    Срабатывает на политике `any`: уровень закрыт решением одного участника,
    остальным жать кнопку незачем, а их ссылки должны перестать работать.
    """
    for step in group.steps:
        if step.status == StepStatus.active:
            step.status = StepStatus.not_required


def _fail_route(
    session: AsyncSession,
    *,
    route: ApprovalRoute,
    memo: Memo,
    failed_group: ApprovalGroup,
    groups: list[ApprovalGroup],
) -> None:
    """
    Останавливает маршрут после отклонения.

    Гасятся все незакрытые задания во всех уровнях, а не только в текущем:
    иначе ссылка подписанта следующего уровня, выданная заранее, осталась бы
    рабочей. Токены при этом сохраняются — они часть доказательной базы;
    нерабочими их делает состояние шага, которое проверяет публичный
    обработчик.
    """
    failed_group.status = GroupStatus.failed
    route.status = RouteStatus.rejected
    route.completed_at = _now()
    route.current_group_no = None
    memo.status = MemoStatus.rejected
    memo.completed_at = route.completed_at

    for group in groups:
        for step in group.steps:
            if step.status in (StepStatus.active, StepStatus.pending):
                step.status = StepStatus.not_required
        if group.status in (GroupStatus.pending, GroupStatus.active) and group is not failed_group:
            group.status = GroupStatus.failed

    session.add(
        AuditLog(
            entity_type="memo",
            entity_id=str(memo.id),
            action="route_rejected",
            actor="system",
            payload={"number": memo.number, "group_no": failed_group.order_no},
        )
    )


def _author_letter(memo: Memo, *, approved: bool, comment: str | None = None) -> OutboxItem:
    if approved:
        body = (
            f"Служебная записка «{memo.title}» согласована полностью.\n"
            f"Регистрационный номер: {memo.number}."
        )
        subject = f"Согласовано: {memo.title}"
    else:
        body = (
            f"Служебная записка «{memo.title}» отклонена.\n"
            f"Причина: {comment or 'не указана'}."
        )
        subject = f"Отклонено: {memo.title}"
    return OutboxItem(to="", subject=subject, body=body)


# ─── отзыв ───────────────────────────────────────────────────────────────────
async def recall(
    session: AsyncSession,
    *,
    memo: Memo,
    actor: str,
) -> None:
    """Отзывает записку: маршрут останавливается, все ссылки гаснут."""
    if memo.author_id != actor:
        raise http_403("Отозвать служебную записку может только её автор")
    if memo.status not in MemoStatus.OPEN:
        raise http_409(f"Нельзя отозвать записку в состоянии «{memo.status}»")

    route = (
        await session.execute(
            select(ApprovalRoute).where(ApprovalRoute.memo_id == memo.id)
        )
    ).scalar_one_or_none()
    if route is None:
        raise http_409("У служебной записки нет маршрута согласования")

    route = await _lock_route(session, route.id)
    groups = await _load_groups(session, route.id)

    for group in groups:
        for step in group.steps:
            if step.status in (StepStatus.active, StepStatus.pending):
                step.status = StepStatus.not_required
        if group.status in (GroupStatus.pending, GroupStatus.active):
            group.status = GroupStatus.failed

    route.status = RouteStatus.recalled
    route.completed_at = _now()
    route.current_group_no = None
    memo.status = MemoStatus.recalled
    memo.completed_at = route.completed_at

    session.add(
        AuditLog(
            entity_type="memo",
            entity_id=str(memo.id),
            action="recalled",
            actor=actor,
            payload={"number": memo.number},
        )
    )
    await session.flush()


# ─── доступ по ссылке ────────────────────────────────────────────────────────
async def resolve_sign_token(session: AsyncSession, token: str) -> ApprovalStep:
    """
    Находит шаг по токену ссылки подписания.

    Порядок проверок повторяет `resolve_share` из EDMS: сначала существование,
    затем погашение, затем срок. Это даёт точный ответ вместо общего 404 —
    повторно открытая ссылка должна сообщать, что решение уже принято, а не
    выглядеть как битый адрес (ТЗ, п. 5.1.4).
    """
    step = (
        await session.execute(
            select(ApprovalStep).where(ApprovalStep.action_token == token)
        )
    ).scalar_one_or_none()

    if step is None:
        raise http_410("Ссылка не найдена")
    if step.token_used_at is not None:
        raise http_410("Ссылка уже использована: решение по ней принято")
    if step.status != StepStatus.active:
        raise http_410(_step_refusal(step))

    expires = _as_aware(step.token_expires_at)
    if expires is not None and expires < _now():
        # Состояние шага здесь сознательно не меняется. Исключение
        # откатывает транзакцию, поэтому отметка всё равно не сохранилась бы,
        # а код, который делает вид, что пишет в базу, хуже отсутствующего.
        # Просроченные задания переводит `expire_overdue` — её вызывает
        # регламентное задание.
        raise http_410("Срок действия ссылки истёк")

    return step


async def expire_overdue(session: AsyncSession) -> int:
    """
    Переводит задания с истёкшей ссылкой в состояние «просрочено».

    Вызывается регламентным заданием, а не обработчиком запроса: пометка
    должна сохраниться, а запрос, наткнувшийся на истёкшую ссылку,
    завершается исключением и откатом.
    """
    rows = await session.execute(
        select(ApprovalStep)
        .where(ApprovalStep.status == StepStatus.active)
        .where(ApprovalStep.token_expires_at.is_not(None))
    )
    now = _now()
    count = 0
    for step in rows.scalars().all():
        expires = _as_aware(step.token_expires_at)
        if expires is not None and expires < now:
            step.status = StepStatus.expired
            session.add(
                AuditLog(
                    entity_type="step",
                    entity_id=str(step.id),
                    action="expired",
                    actor="system",
                    payload={"expired_at": expires.isoformat()},
                )
            )
            count += 1

    await session.flush()
    return count
