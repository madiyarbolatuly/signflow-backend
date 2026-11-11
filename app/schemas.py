"""Схемы запросов и ответов."""

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

from app.db.enums import GroupPolicy


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


#: Момент времени в ответе API — всегда с поясом. SQLite при чтении пояс
#: теряет, и значение уходило клиенту «голым»; браузер принимал его за
#: местное время и сдвигал дату. PostgreSQL пояс хранит, там это не
#: проявлялось — то есть разработка и эксплуатация показывали разное.
Moment = Annotated[datetime, AfterValidator(_as_utc)]


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
    #: «Кому» построчно. Пусто — берётся адресат из шаблона.
    addressee: str | None = Field(default=None, max_length=500)


class MemoUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: dict | None = None
    addressee: str | None = Field(default=None, max_length=500)


class RenderRequest(BaseModel):
    """Записка, которую ещё не сохранили, — для предпросмотра."""

    template_id: int | None = None
    body: dict = Field(default_factory=dict)
    addressee: str | None = Field(default=None, max_length=500)


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
    decided_at: Moment | None = None


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
    created_at: Moment
    submitted_at: Moment | None = None
    completed_at: Moment | None = None


class BodyField(BaseModel):
    """Поле записки вместе с подписью из схемы шаблона."""

    name: str
    label: str
    #: Тип из схемы шаблона; у записки без шаблона его нет.
    type: str | None = None
    value: object = None
    #: Значение в привычном человеку виде: дата `01.12.2026`, число с
    #: разрядами. Формируется на сервере, тем же кодом, что и PDF.
    display: str = ""


class DocumentSender(BaseModel):
    value: str
    caption: str


class DocumentHeaderField(BaseModel):
    label: str
    display: str


class DocumentStamp(BaseModel):
    """Штамп электронной подписи автора — есть у отправленного документа."""

    name: str
    at: str
    zone: str


class DocumentOut(BaseModel):
    """
    Документ в виде бумажного бланка — то же, что печатается в PDF.

    Собирается на сервере (`formatting.compose_document`), экран только
    раскладывает готовое: разойтись с PDF ему нечем.
    """

    kind: str
    addressee: list[str]
    sender: list[DocumentSender]
    header_fields: list[DocumentHeaderField]
    doc_title: str
    paragraphs: list[str]
    date: str
    signer: str
    stamp: DocumentStamp | None = None


class PreviewField(BodyField):
    #: Обязательное поле ещё не заполнено.
    missing: bool = False


class RenderOut(BaseModel):
    fields: list[PreviewField]
    #: None — у шаблона нет связного текста, документ печатается таблицей.
    document: DocumentOut | None = None


class MemoDetailOut(MemoOut):
    route: RouteOut | None = None
    #: Нужны листу предпросмотра: в документе стоит подпись автора, а не
    #: его идентификатор.
    author_name: str | None = None
    author_position: str | None = None
    addressee: str | None = None
    fields: list[BodyField] = Field(default_factory=list)
    document: DocumentOut | None = None


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    file_name: str
    size: int
    file_hash: str | None = None


class PublicAttachmentOut(BaseModel):
    """Вложение в том виде, в каком его видит подписант по ссылке."""

    id: int
    file_name: str
    size: int


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
    author_position: str | None = None
    created_at: Moment
    submitted_at: Moment | None = None
    signer_name: str | None = None
    signer_position: str | None = None
    expires_at: Moment | None = None
    attachments: list[PublicAttachmentOut] = Field(default_factory=list)
    fields: list[BodyField] = Field(default_factory=list)
    document: DocumentOut | None = None


# ─── уведомления ─────────────────────────────────────────────────────────────
class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    message: str
    link: str | None = None
    status: str
    created_at: Moment


class NotificationListOut(BaseModel):
    unread: int
    items: list[NotificationOut]


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
    text_template: str | None = None


class TemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    title: str
    body_schema: dict
    default_route: list | None = None
    text_template: str | None = None
    is_active: bool
