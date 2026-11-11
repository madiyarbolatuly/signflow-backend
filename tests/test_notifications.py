"""Уведомления в интерфейсе: кто их получает и что видит через API."""

from sqlalchemy import select

from app.db.enums import GroupPolicy
from app.db.tables import ApprovalGroup, ApprovalStep, Notification
from app.services import approval
from tests.conftest import make_user


async def _first_step(session, route_id: int) -> ApprovalStep:
    return (
        await session.execute(
            select(ApprovalStep)
            .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
            .where(ApprovalGroup.route_id == route_id)
            .order_by(ApprovalStep.id)
        )
    ).scalars().first()


async def _messages(session, user_id: str) -> list[str]:
    rows = await session.execute(
        select(Notification.message).where(Notification.user_id == user_id)
    )
    return list(rows.scalars().all())


async def test_signer_from_directory_is_notified_on_activation(session, memo):
    boss = await make_user(session, "chief@example.com", "Иван Петров")
    await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"user_id": boss.id}]}],
    )
    await session.flush()
    assert any("ожидает вашего решения" in m for m in await _messages(session, boss.id))


async def test_author_is_notified_of_approval(session, memo, author):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"email": "boss@example.com"}]}],
    )
    step = await _first_step(session, route.id)
    await approval.decide(session, step=step, approve=True, actor="boss")
    await session.flush()

    assert any("согласована" in m for m in await _messages(session, author.id))


async def test_author_is_notified_of_rejection_with_the_reason(session, memo, author):
    route = await approval.build_route(
        session,
        memo=memo,
        spec=[{"policy": GroupPolicy.all_of, "signers": [{"email": "boss@example.com"}]}],
    )
    step = await _first_step(session, route.id)
    await approval.decide(
        session, step=step, approve=False, comment="Нет бюджета", actor="boss"
    )
    await session.flush()

    messages = await _messages(session, author.id)
    assert any("отклонена" in m and "Нет бюджета" in m for m in messages)
