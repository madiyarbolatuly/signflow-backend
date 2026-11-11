"""
Точка входа SignFlow.

В отличие от EDMS, на старте схема не создаётся: там `lifespan` вызывал
`metadata.create_all()`, из-за чего Alembic фактически не использовался, а
схема на разных площадках расходилась. Здесь схему ставит только
`alembic upgrade head`.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router as api_router
from app.core.config import settings
from app.services.mail import describe_mode

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# При запуске — в каком режиме почта. Настройки читаются один раз при старте,
# и по этой строке сразу видно, подхватил ли сервер правку .env.
logging.getLogger("signflow.mail").info(describe_mode())

app = FastAPI(
    title=settings.title,
    description=settings.description,
    version=settings.version,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health", tags=["Служебные"])
async def health() -> dict:
    return {"status": "ok", "version": settings.version}
