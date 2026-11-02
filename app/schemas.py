"""Схемы запросов и ответов."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.enums import GroupPolicy


# ─── авторизация ─────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: EmailStr
    full_name: str
    position: str | None = None
    department_id: int | None = None
    role: str


# ─── служебные записки ───────────────────────────────────────────────────────
class MemoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    template_id: int | None = None
    body: dict = Field(default_factory=dict)


class MemoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: dict | None = None


class SignerSpec(BaseModel):
    #: Для сотрудника достаточно `user_id` — почта и должность берутся из
    #: справочника. Для внешнего согласующего обязателен `email`.
    user_id: str | None = None
    email: EmailStr | None = None
    name: str | None = None
    position: str | None = None


class LevelSpec(BaseModel):
    policy: str = GroupPolicy.all_of
    signers: list[SignerSpec] = Field(min_length=1)


class SubmitRequest(BaseModel):
    route: list[LevelSpec] = Field(min_length=1)


class DecisionRequest(BaseModel):
    approve: bool
    #: Обязателен при отклонении — проверяется в машине состояний, где
    #: доступен контекст шага.
    comment: str | None = None


class PublicDecisionRequest(DecisionRequest):
    #: Подписант по ссылке может не быть в справочнике, тогда ФИО вводится
    #: на странице подписания и попадает в лист согласования.
    signer_name: str | None = None


class StepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    signer_email: EmailStr
    signer_name: str | None = None
    signer_position: str | None = None
    status: str
    comment: str | None = None
    decided_at: datetime | None = None


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_no: int
    policy: str
    status: str
    steps: list[StepOut]


class RouteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    current_group_no: int | None = None
    groups: list[GroupOut]


class MemoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str | None = None
    title: str
    status: str
    author_id: str
    body: dict
    created_at: datetime
    submitted_at: datetime | None = None
    completed_at: datetime | None = None


class MemoDetailOut(MemoOut):
    route: RouteOut | None = None


class PublicMemoOut(BaseModel):
    """
    Что видит подписант по ссылке.

    Сознательно урезано: ни идентификаторов, ни маршрута целиком, ни других
    записок — держатель ссылки получает доступ к одному документу и только
    к тому, что нужно для решения.
    """

    number: str | None = None
    title: str
    body: dict
    author_name: str
    created_at: datetime
    signer_name: str | None = None
    signer_position: str | None = None
    expires_at: datetime | None = None


# ─── администрирование ───────────────────────────────────────────────────────
class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    head_user_id: str | None = None


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    head_user_id: str | None = None


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    position: str | None = None
    department_id: int | None = None
    role: str = "author"
    password: str | None = Field(default=None, min_length=8)


class TemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    body_schema: dict = Field(default_factory=dict)
    default_route: list | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    body_schema: dict
    default_route: list | None = None
    is_active: bool
