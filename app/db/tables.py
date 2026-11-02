"""
Схема базы данных SignFlow.

Один модуль на всю схему: сущностей одиннадцать, они плотно связаны, и
разносить их по файлам, как в EDMS, значит получить те же круговые импорты,
из-за которых там в `models.py` трижды подряд импортируется `Document`.

Состояния — строки с CHECK-ограничением, см. `app/db/enums.py`.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from ulid import ULID

from app.db.base import Base, created_at_column
from app.db.enums import (
    GroupPolicy,
    GroupStatus,
    MemoStatus,
    NotifyStatus,
    RouteStatus,
    SignMethod,
    StepStatus,
    UserRole,
)


#: 64-битный ключ, пригодный и для PostgreSQL, и для SQLite.
#:
#: SQLite назначает автоинкремент только колонке типа INTEGER PRIMARY KEY:
#: объявленный как BIGINT ключ не заполняется, и вставка падает на
#: «NOT NULL constraint failed». В PostgreSQL же нужен именно BIGINT —
#: регистрационные номера служебных записок переживут выход за int4.
#: `with_variant` даёт каждому диалекту свой тип из одного объявления.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


def _in(column: str, values) -> CheckConstraint:
    """CHECK-ограничение «значение из набора». Набор берётся из enums.py."""
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=f"ck_{column}_allowed")


def _ulid() -> str:
    return str(ULID())


# ─── организация ─────────────────────────────────────────────────────────────
class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    #: Руководитель подразделения — то, чего не было в EDMS и без чего
    #: невозможно предложить маршрут по умолчанию «на моего шефа».
    #: `use_alter` разрывает круг departments → users → departments при
    #: создании таблиц.
    head_user_id: Mapped[str | None] = mapped_column(
        String(26),
        ForeignKey("users.id", use_alter=True, name="fk_departments_head"),
        nullable=True,
    )

    created_at: Mapped[datetime] = created_at_column()


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=_ulid)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("departments.id"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.author)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_column()

    department = relationship("Department", foreign_keys=[department_id])

    __table_args__ = (_in("role", UserRole.ALL),)


# ─── служебные записки ───────────────────────────────────────────────────────
class MemoTemplate(Base):
    __tablename__ = "memo_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Описание полей формы. Администратор меняет состав полей, не трогая код
    #: (ТЗ, п. 5.1.2).
    body_schema: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    #: Маршрут по умолчанию. Автор вправе изменить его перед отправкой.
    default_route: Mapped[list | None] = mapped_column(JSON, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = created_at_column()


class Memo(Base):
    __tablename__ = "memos"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)

    #: Регистрационный номер. Присваивается при запуске маршрута, у черновика
    #: его нет: номер, выданный документу, который так и не отправили, создаёт
    #: дыры в нумерации и вопросы у кадровой службы.
    number: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)

    author_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id"), nullable=False)
    template_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("memo_templates.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=MemoStatus.draft)

    created_at: Mapped[datetime] = created_at_column()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    author = relationship("User", foreign_keys=[author_id])
    template = relationship("MemoTemplate")
    attachments = relationship("Attachment", back_populates="memo", cascade="all, delete-orphan")
    route = relationship(
        "ApprovalRoute", back_populates="memo", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        _in("status", MemoStatus.ALL),
        # Перечень «мои служебные записки» всегда фильтрует по автору
        # и сортирует по дате создания.
        Index("ix_memos_author_created", "author_id", "created_at"),
        Index("ix_memos_status", "status"),
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    memo_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("memos.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = created_at_column()

    memo = relationship("Memo", back_populates="attachments")

    __table_args__ = (Index("ix_attachments_memo", "memo_id"),)


# ─── маршрут согласования ────────────────────────────────────────────────────
class ApprovalRoute(Base):
    __tablename__ = "approval_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Один живой маршрут на записку. Повторная отправка после отклонения
    #: заменяет маршрут, а не добавляет второй.
    memo_id: Mapped[int] = mapped_column(
        BigIntPK, ForeignKey("memos.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=RouteStatus.active)

    #: Номер уровня, который выполняется сейчас. NULL — маршрут завершён.
    current_group_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memo = relationship("Memo", back_populates="route")
    groups = relationship(
        "ApprovalGroup",
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="ApprovalGroup.order_no",
    )

    __table_args__ = (_in("status", RouteStatus.ALL),)


class ApprovalGroup(Base):
    """
    Уровень маршрута: шаги внутри уровня активируются одновременно.

    Параллельность выражена именно так, а не флагом на шаге. Последовательный
    маршрут — это цепочка уровней по одному шагу, отдельной логики для него
    не нужно, и машина состояний остаётся одна.
    """

    __tablename__ = "approval_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("approval_routes.id", ondelete="CASCADE"), nullable=False
    )
    order_no: Mapped[int] = mapped_column(Integer, nullable=False)
    policy: Mapped[str] = mapped_column(String(16), nullable=False, default=GroupPolicy.all_of)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=GroupStatus.pending)

    route = relationship("ApprovalRoute", back_populates="groups")
    steps = relationship(
        "ApprovalStep", back_populates="group", cascade="all, delete-orphan", order_by="ApprovalStep.id"
    )

    __table_args__ = (
        _in("policy", GroupPolicy.ALL),
        _in("status", GroupStatus.ALL),
        UniqueConstraint("route_id", "order_no", name="uq_group_route_order"),
    )


class ApprovalStep(Base):
    __tablename__ = "approval_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("approval_groups.id", ondelete="CASCADE"), nullable=False
    )

    #: Согласующий может не иметь учётной записи — он подписывает по ссылке.
    #: Поэтому обязателен адрес почты, а не ссылка на пользователя.
    signer_user_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("users.id"), nullable=True
    )
    signer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    signer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signer_position: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=StepStatus.pending)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Одноразовый токен ссылки подписания. Выдаётся при активации шага,
    #: гасится простановкой `token_used_at`.
    action_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_column()

    group = relationship("ApprovalGroup", back_populates="steps")
    signer = relationship("User", foreign_keys=[signer_user_id])
    signature = relationship(
        "Signature", back_populates="step", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        _in("status", StepStatus.ALL),
        # Перечень «мне на подпись» ищет активные шаги по пользователю и почте.
        Index("ix_steps_signer_status", "signer_user_id", "status"),
        Index("ix_steps_email_status", "signer_email", "status"),
    )


class Signature(Base):
    """
    Факт подписания. Отдельно от шага, чтобы решение и его доказательная
    часть не смешивались: шаг — это задание и его состояние, подпись — то,
    что предъявляется при споре.
    """

    __tablename__ = "signatures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("approval_steps.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    #: Хеш файла на момент подписания. Расхождение с текущим `memos.file_hash`
    #: означает, что документ после подписания менялся.
    file_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    signed_at: Mapped[datetime] = created_at_column()
    method: Mapped[str] = mapped_column(String(16), nullable=False, default=SignMethod.link)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    step = relationship("ApprovalStep", back_populates="signature")

    __table_args__ = (_in("method", SignMethod.ALL),)


# ─── системные таблицы ───────────────────────────────────────────────────────
class AuditLog(Base):
    """
    Журнал аудита. Только добавление: ТЗ, п. 5.1.7 запрещает изменение и
    удаление записей средствами интерфейса, поэтому в API нет ни PATCH,
    ни DELETE на эту таблицу.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Кто совершил действие. Строка, а не ссылка на пользователя: подписант
    #: по ссылке учётной записи не имеет, и сослаться на него нечем.
    actor: Mapped[str] = mapped_column(String(255), nullable=False)

    at: Mapped[datetime] = created_at_column()
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(26), ForeignKey("users.id"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=NotifyStatus.unread)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        _in("status", NotifyStatus.ALL),
        Index("ix_notifications_user_status", "user_id", "status"),
    )
