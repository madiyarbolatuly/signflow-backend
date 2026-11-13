"""Компании группы — для выбора бланка в редакторе."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.api.deps import CurrentUser, SessionDep
from app.core.exceptions import http_404
from app.schemas import CompanyOut
from app.services import companies

router = APIRouter(tags=["Компании"])


@router.get("", response_model=list[CompanyOut])
async def list_companies(session: SessionDep, user: CurrentUser) -> list[dict]:
    """Действующие компании; `is_default` — компания из отдела сотрудника."""
    items = await companies.active(session)
    default = await companies.default_for(session, user, items)
    return [
        {
            **{c.name: getattr(company, c.name) for c in company.__table__.columns},
            "is_default": default is not None and company.id == default.id,
            "has_logo": companies.logo_path(company) is not None,
        }
        for company in items
    ]


@router.get("/{company_id}/logo")
async def company_logo(company_id: int, session: SessionDep) -> FileResponse:
    """
    Логотип для шапки бланка. Без авторизации: шапку видит и подписант по
    ссылке, а логотип компании секретом не является.
    """
    company = await companies.get(session, company_id)
    path = companies.logo_path(company) if company else None
    if path is None:
        raise http_404("Логотип не найден")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})
