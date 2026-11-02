"""
Проверки машины состояний согласования.

Покрывают приёмочные испытания из раздела 7 ТЗ, кроме № 1 (миграции,
проверяются командами Alembic) и № 7 (состязание на параллельном уровне —
требует блокировки строки, которой в SQLite нет; см. отметку в конце файла).
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.enums import GroupPolicy, GroupStatus, MemoStatus, RouteStatus, StepStatus
from app.db.tables import ApprovalGroup, ApprovalStep, AuditLog, Signature
from app.services import approval
from tests.conftest import make_user


async def _steps(session, route_id: int) -> list[ApprovalStep]:
    rows = await session.execute(
        select(ApprovalStep)
        .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
        .where(ApprovalGroup.route_id == route_id)
        .order_by(ApprovalGroup.order_no, ApprovalStep.id)
    )
    return list(rows.scalars().all())


def _level(policy: str, *emails: str) -> dict:
    return {"policy": policy, "signers": [{"email": e} for e in emails]}


# ─── испытание № 2: последовательный маршрут ─────────────────────────────────
async def test_sequential_route_approves_memo(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[_level(GroupPolicy.all_of, "boss@example.com")],
    )

    assert memo.status == MemoStatus.in_review
    assert memo.number, "при запуске маршрута записке присваивается номер"

    outbox = approval.collect_outbox(route)
    assert len(outbox) == 1
    assert outbox[0].to == "boss@example.com"
    assert "/sign/" in outbox[0].body, "в письме должна быть ссылка подписания"

    step = (await _steps(session, route.id))[0]
    assert step.status == StepStatus.active
    assert step.action_token

    result = await approval.decide(
        session, step=step, approve=True, actor="boss@example.com"
    )

    assert result.memo_status == MemoStatus.approved
    assert result.route_status == RouteStatus.approved
    assert memo.completed_at is not None

    signature = (
        await session.execute(select(Signature).where(Signature.step_id == step.id))
    ).scalar_one()
    assert signature.evidence["decision"] == "approved"


async def test_two_levels_advance_one_at_a_time(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[
            _level(GroupPolicy.all_of, "boss@example.com"),
            _level(GroupPolicy.all_of, "hr@example.com"),
        ],
    )

    first, second = await _steps(session, route.id)
    # Второй уровень не должен быть разослан заранее: иначе кадры получат
    # документ, который руководитель ещё не видел.
    assert second.status == StepStatus.pending
    assert second.action_token is None

    result = await approval.decide(session, step=first, approve=True, actor="boss")
    assert result.activated_group_no == 2
    assert result.memo_status == MemoStatus.in_review

    await session.refresh(second)
    assert second.status == StepStatus.active
    assert second.action_token

    result = await approval.decide(session, step=second, approve=True, actor="hr")
    assert result.memo_status == MemoStatus.approved


# ─── испытание № 3: параллельный уровень, политика «все» ─────────────────────
async def test_parallel_all_waits_for_every_signer(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[
            _level(GroupPolicy.all_of, "a@example.com", "b@example.com"),
            _level(GroupPolicy.all_of, "hr@example.com"),
        ],
    )

    a, b, hr = await _steps(session, route.id)
    assert a.status == b.status == StepStatus.active, "уровень рассылается целиком"

    result = await approval.decide(session, step=a, approve=True, actor="a")
    assert result.activated_group_no is None, "одного согласования недостаточно"
    assert result.memo_status == MemoStatus.in_review
    await session.refresh(hr)
    assert hr.status == StepStatus.pending

    result = await approval.decide(session, step=b, approve=True, actor="b")
    assert result.activated_group_no == 2
    await session.refresh(hr)
    assert hr.status == StepStatus.active


# ─── испытание № 4: параллельный уровень, политика «любой» ───────────────────
async def test_parallel_any_closes_on_first_decision(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[_level(GroupPolicy.any_of, "x@example.com", "y@example.com", "z@example.com")],
    )

    x, y, z = await _steps(session, route.id)
    token_y = y.action_token

    result = await approval.decide(session, step=x, approve=True, actor="x")
    assert result.memo_status == MemoStatus.approved

    await session.refresh(y)
    await session.refresh(z)
    assert y.status == StepStatus.not_required
    assert z.status == StepStatus.not_required

    # Ссылка погашена не удалением токена — он часть доказательной базы, —
    # а состоянием шага, которое проверяет публичный обработчик.
    with pytest.raises(HTTPException) as err:
        await approval.resolve_sign_token(session, token_y)
    assert err.value.status_code == 410
    assert "другим участником" in err.value.detail


# ─── испытание № 5: отклонение останавливает маршрут ─────────────────────────
async def test_rejection_stops_whole_route(session, memo):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[
            _level(GroupPolicy.all_of, "boss@example.com"),
            _level(GroupPolicy.all_of, "hr@example.com"),
        ],
    )

    boss, hr = await _steps(session, route.id)
    result = await approval.decide(
        session, step=boss, approve=False, comment="Нет бюджета", actor="boss"
    )

    assert result.memo_status == MemoStatus.rejected
    assert result.route_status == RouteStatus.rejected

    await session.refresh(hr)
    assert hr.status == StepStatus.not_required, "задания следующих уровней гаснут"

    groups = await session.execute(
        select(ApprovalGroup).where(ApprovalGroup.route_id == route.id)
    )
    assert all(g.status == GroupStatus.failed for g in groups.scalars().all())

    assert result.outbox and "отклонена" in result.outbox[0].body


async def test_rejection_requires_a_reason(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]

    with pytest.raises(HTTPException) as err:
        await approval.decide(session, step=step, approve=False, comment="  ", actor="boss")
    assert err.value.status_code == 400

    await session.refresh(step)
    assert step.status == StepStatus.active, "неудачная попытка не меняет состояние"


# ─── испытание № 6: одноразовость ссылки ─────────────────────────────────────
async def test_sign_link_is_single_use(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]
    token = step.action_token

    resolved = await approval.resolve_sign_token(session, token)
    assert resolved.id == step.id

    await approval.decide(session, step=step, approve=True, actor="boss")

    with pytest.raises(HTTPException) as err:
        await approval.resolve_sign_token(session, token)
    assert err.value.status_code == 410
    assert "уже использована" in err.value.detail


async def test_unknown_token_is_gone_not_found(session):
    # Для держателя ссылки 410 информативнее 404: ссылка существовала, но
    # больше не работает.
    with pytest.raises(HTTPException) as err:
        await approval.resolve_sign_token(session, "no-such-token")
    assert err.value.status_code == 410


async def test_expired_link_is_refused(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]
    step.token_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.flush()

    with pytest.raises(HTTPException) as err:
        await approval.resolve_sign_token(session, step.action_token)
    assert err.value.status_code == 410
    assert "истёк" in err.value.detail

    # Читающий обработчик состояние не меняет: исключение откатило бы запись.
    await session.refresh(step)
    assert step.status == StepStatus.active


async def test_expire_overdue_marks_stale_steps(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]
    step.token_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await session.flush()

    assert await approval.expire_overdue(session) == 1
    await session.refresh(step)
    assert step.status == StepStatus.expired

    # Повторный проход не должен трогать уже помеченные задания.
    assert await approval.expire_overdue(session) == 0


async def test_second_decision_on_same_step_is_refused(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.any_of, "a@example.com", "b@example.com")]
    )
    a, _b = await _steps(session, route.id)

    await approval.decide(session, step=a, approve=True, actor="a")

    with pytest.raises(HTTPException) as err:
        await approval.decide(session, step=a, approve=True, actor="a")
    assert err.value.status_code == 409
    assert "уже принято" in err.value.detail, (
        "сообщение должно говорить о состоянии задания, а не маршрута"
    )


# ─── отзыв автором ───────────────────────────────────────────────────────────
async def test_author_can_recall_and_links_die(session, memo, author):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[_level(GroupPolicy.all_of, "boss@example.com")],
    )
    step = (await _steps(session, route.id))[0]
    token = step.action_token

    await approval.recall(session, memo=memo, actor=author.id)

    assert memo.status == MemoStatus.recalled
    with pytest.raises(HTTPException) as err:
        await approval.resolve_sign_token(session, token)
    assert err.value.status_code == 410


async def test_only_author_can_recall(session, memo):
    await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    with pytest.raises(HTTPException) as err:
        await approval.recall(session, memo=memo, actor="somebody-else")
    assert err.value.status_code == 403


async def test_decision_after_recall_is_refused(session, memo, author):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]
    await approval.recall(session, memo=memo, actor=author.id)

    with pytest.raises(HTTPException) as err:
        await approval.decide(session, step=step, approve=True, actor="boss")
    assert err.value.status_code == 409


# ─── построение маршрута: проверки входных данных ────────────────────────────
async def test_empty_route_is_refused(session, memo):
    with pytest.raises(HTTPException) as err:
        await approval.build_route(session, memo=memo, spec=[])
    assert err.value.status_code == 400


async def test_level_without_signers_is_refused(session, memo):
    with pytest.raises(HTTPException) as err:
        await approval.build_route(
            session, memo=memo, spec=[{"policy": GroupPolicy.all_of, "signers": []}]
        )
    assert err.value.status_code == 400


async def test_duplicate_signer_in_level_is_refused(session, memo):
    with pytest.raises(HTTPException) as err:
        await approval.build_route(
            session,
            memo=memo,
            spec=[_level(GroupPolicy.all_of, "a@example.com", "A@example.com")],
        )
    assert err.value.status_code == 400
    assert "дважды" in err.value.detail


async def test_route_cannot_start_twice(session, memo):
    await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    with pytest.raises(HTTPException) as err:
        await approval.build_route(
            session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
        )
    assert err.value.status_code == 409


async def test_known_signer_details_come_from_the_directory(session, memo):
    boss = await make_user(session, "chief@example.com", "Иван Петров")
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"user_id": boss.id}]}],
    )
    step = (await _steps(session, route.id))[0]

    assert step.signer_email == "chief@example.com"
    assert step.signer_name == "Иван Петров"
    assert step.signer_user_id == boss.id


# ─── испытание № 8: журнал аудита ────────────────────────────────────────────
async def test_audit_trail_records_every_decision(session, memo):
    route = await approval.build_route(
        session, memo=memo, spec=[_level(GroupPolicy.all_of, "boss@example.com")]
    )
    step = (await _steps(session, route.id))[0]

    await approval.decide(
        session,
        step=step,
        approve=True,
        actor="boss@example.com",
        ip="10.1.2.3",
        user_agent="Mozilla/5.0",
    )

    rows = (await session.execute(select(AuditLog))).scalars().all()
    actions = {r.action for r in rows}
    assert {"route_started", "step_activated", "approved", "route_approved"} <= actions

    decision = next(r for r in rows if r.action == "approved")
    assert decision.ip == "10.1.2.3"
    assert decision.user_agent == "Mozilla/5.0"
    assert decision.actor == "boss@example.com"


@pytest.mark.skip(
    reason="Испытание № 7 ТЗ: состязание на параллельном уровне проверяется "
    "только на PostgreSQL — SQLite не поддерживает SELECT ... FOR UPDATE"
)
async def test_concurrent_decisions_activate_next_level_once():
    ...
