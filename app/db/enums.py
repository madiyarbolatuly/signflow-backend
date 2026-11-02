"""
Наборы состояний.

Сознательно обычные строковые константы, а не `Enum` из PostgreSQL:
`ALTER TYPE ... ADD VALUE` не откатывается внутри транзакции, поэтому любое
расширение перечня состояний ломает откат миграции. В EDMS этим уже больно —
`DocStatus` там объявлен как SQLEnum. Здесь допустимый набор задаётся
CHECK-ограничением, которое меняется обычным ALTER TABLE (ТЗ, риск Р4).
"""

from typing import Final


class MemoStatus:
    draft: Final = "draft"
    submitted: Final = "submitted"
    in_review: Final = "in_review"
    approved: Final = "approved"
    rejected: Final = "rejected"
    recalled: Final = "recalled"

    ALL: Final = (draft, submitted, in_review, approved, rejected, recalled)
    #: Состояния, из которых маршрут ещё может двигаться.
    OPEN: Final = (submitted, in_review)


class RouteStatus:
    active: Final = "active"
    approved: Final = "approved"
    rejected: Final = "rejected"
    recalled: Final = "recalled"

    ALL: Final = (active, approved, rejected, recalled)


class GroupStatus:
    pending: Final = "pending"
    active: Final = "active"
    satisfied: Final = "satisfied"
    failed: Final = "failed"

    ALL: Final = (pending, active, satisfied, failed)


class GroupPolicy:
    #: Решение должны принять все участники уровня.
    all_of: Final = "all"
    #: Достаточно решения одного участника.
    any_of: Final = "any"

    ALL: Final = (all_of, any_of)


class StepStatus:
    pending: Final = "pending"
    active: Final = "active"
    approved: Final = "approved"
    rejected: Final = "rejected"
    #: Уровень закрыт решением другого участника либо маршрут остановлен.
    not_required: Final = "not_required"
    expired: Final = "expired"

    ALL: Final = (pending, active, approved, rejected, not_required, expired)


class UserRole:
    author: Final = "author"
    approver: Final = "approver"
    admin: Final = "admin"

    ALL: Final = (author, approver, admin)


class NotifyStatus:
    unread: Final = "unread"
    read: Final = "read"

    ALL: Final = (unread, read)


class SignMethod:
    #: Простая электронная подпись по одноразовой ссылке, без авторизации.
    link: Final = "link"
    #: Простая электронная подпись внутри системы, под своей учётной записью.
    internal: Final = "internal"

    ALL: Final = (link, internal)
