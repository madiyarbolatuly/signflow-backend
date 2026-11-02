"""
Асинхронный движок и фабрика сессий.

В отличие от EDMS, здесь нет второго, синхронного движка: он использовался
там только для `metadata.create_all()` на старте приложения. Схему создаёт
Alembic, поэтому второй движок не нужен.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

_connect_args: dict = {}
if settings.async_database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

async_engine = create_async_engine(
    settings.async_database_url,
    echo=settings.debug,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Зависимость FastAPI: выдаёт сессию и фиксирует транзакцию по завершении
    обработчика.

    Здесь сознательное расхождение с EDMS. Там зависимость делала `flush()`,
    а `commit()` не делал никто, из-за чего часть изменений не доходила до
    базы. Правило этого проекта: транзакцией владеет зависимость — она
    фиксирует при успехе и откатывает при исключении, а сервисы и обработчики
    `commit()` не вызывают.
    """
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
