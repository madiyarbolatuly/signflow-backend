"""
Общая обвязка тестов.

Переменные окружения выставляются до импорта модулей приложения: настройки
читаются один раз при импорте `app.core.config`, и задать их позже уже нельзя.
"""

import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-at-least-16-chars")
os.environ.setdefault("DATABASE_URL_OVERRIDE", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("MAIL_DRY_RUN", "true")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base  # noqa: E402
from app.db.enums import UserRole  # noqa: E402
from app.db.tables import Memo, User  # noqa: E402
from app.services import storage  # noqa: E402


@pytest.fixture(autouse=True)
def storage_in_tmp(tmp_path, monkeypatch):
    """
    Хранилище на время теста — временный каталог.

    Для всех тестов без исключения. Раньше изоляция стояла только в
    test_documents.py, а сквозной тест API писал в рабочее `./storage`:
    номера записок в тестовой базе начинаются с единицы, поэтому прогон
    `pytest` затирал `storage/memos/1/memo.pdf` настоящей записки №1 чужим
    документом — и её сохранённый хеш переставал совпадать с файлом.
    """
    monkeypatch.setattr(storage.settings, "storage_path", str(tmp_path / "storage"))
    return tmp_path


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """
    Сессия поверх SQLite в памяти.

    Соединение одно на тест (StaticPool не нужен: тесты однопоточные и
    держат одну сессию), схема создаётся из метаданных — проверка самих
    миграций живёт отдельно, в `alembic upgrade/downgrade`.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s

    await engine.dispose()


@pytest_asyncio.fixture
async def author(session: AsyncSession) -> User:
    user = User(
        email="author@example.com",
        full_name="Автор Служебки",
        position="Инженер",
        role=UserRole.author,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def memo(session: AsyncSession, author: User) -> Memo:
    m = Memo(author_id=author.id, title="О направлении на обучение", body={"days": 5})
    session.add(m)
    await session.flush()
    return m


async def make_user(session: AsyncSession, email: str, name: str) -> User:
    user = User(email=email, full_name=name, role=UserRole.approver)
    session.add(user)
    await session.flush()
    return user
