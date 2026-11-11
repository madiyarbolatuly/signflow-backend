"""Уведомления в интерфейсе: перечень, счётчик непрочитанных, отметка о прочтении."""

from fastapi import APIRouter
from sqlalchemy import func, select, update

from app.api.deps import CurrentUser, SessionDep
from app.core.exceptions import http_404
from app.db.enums import NotifyStatus
from app.db.tables import Notification
from app.schemas import NotificationListOut

router = APIRouter(tags=["Уведомления"])

#: Сколько последних уведомлений отдаётся. Колокольчику больше не нужно, а
#: без предела перечень активного согласующего рос бы бесконечно.
PAGE = 30


@router.get("", response_model=NotificationListOut)
async def list_notifications(session: SessionDep, user: CurrentUser) -> dict:
    items = (
        await session.execute(
            select(Notification)
            .where(Notification.user_id == user.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(PAGE)
        )
    ).scalars().all()

    # Счётчик считается отдельно и по всем уведомлениям, а не по странице:
    # иначе при 40 непрочитанных значок показывал бы 30.
    unread = (
        await session.execute(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == user.id)
            .where(Notification.status == NotifyStatus.unread)
        )
    ).scalar_one()

    return {"unread": unread, "items": list(items)}


@router.post("/read-all")
async def read_all(session: SessionDep, user: CurrentUser) -> dict:
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == user.id)
        .where(Notification.status == NotifyStatus.unread)
        .values(status=NotifyStatus.read)
    )
    return {"updated": result.rowcount}


@router.post("/{notification_id}/read")
async def read_one(notification_id: int, session: SessionDep, user: CurrentUser) -> dict:
    # Чужое уведомление неотличимо от несуществующего: условие по владельцу
    # стоит в самом запросе, а не проверяется после чтения.
    result = await session.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .where(Notification.user_id == user.id)
        .values(status=NotifyStatus.read)
    )
    if result.rowcount == 0:
        raise http_404("Уведомление не найдено")
    return {"updated": 1}
