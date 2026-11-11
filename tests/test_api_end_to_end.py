"""
Сквозная проверка через HTTP.

Повторяет приёмочные испытания № 2 и № 6 из раздела 7 ТЗ целиком по API:
автор входит, создаёт записку, запускает маршрут, согласующий открывает
ссылку без авторизации, принимает решение, повторное открытие ссылки
отклоняется.
"""

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import client_info
from app.core.security import hash_password
from app.db.base import Base
from app.db.enums import UserRole
from app.db.session import get_async_session
from app.db.tables import ApprovalStep, User
from app.main import app

PREFIX = "/v1"


@pytest_asyncio.fixture
async def api():
    """
    Приложение поверх SQLite в памяти.

    StaticPool здесь обязателен: без него каждое соединение к
    `sqlite:///:memory:` получает собственную пустую базу, и созданные в
    одном запросе строки не видны следующему.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_session():
        async with maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_async_session] = override_session
    # TestClient не передаёт адрес клиента, а журнал аудита его записывает.
    app.dependency_overrides[client_info] = lambda: {
        "ip": "10.0.0.7",
        "user_agent": "pytest",
    }

    async with maker() as setup:
        setup.add(
            User(
                email="author@example.com",
                full_name="Автор Служебки",
                position="Инженер",
                role=UserRole.author,
                password_hash=hash_password("verysecret123"),
            )
        )
        await setup.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, maker

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_full_cycle_from_login_to_signature(api):
    client, maker = api

    # ─── вход ────────────────────────────────────────────────────────────────
    resp = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "author@example.com", "password": "verysecret123"},
    )
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    resp = await client.get(f"{PREFIX}/auth/me", headers=headers)
    assert resp.json()["full_name"] == "Автор Служебки"

    # ─── создание черновика ──────────────────────────────────────────────────
    resp = await client.post(
        f"{PREFIX}/memos",
        headers=headers,
        json={"title": "О направлении на обучение", "body": {"days": 5}},
    )
    assert resp.status_code == 201, resp.text
    memo_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"
    assert resp.json()["number"] is None, "у черновика номера нет"

    # ─── запуск маршрута ─────────────────────────────────────────────────────
    resp = await client.post(
        f"{PREFIX}/memos/{memo_id}/submit",
        headers=headers,
        json={"route": [{"policy": "all", "signers": [{"email": "boss@example.com"}]}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "in_review"
    assert body["number"], "при запуске маршрута присваивается номер"
    assert body["route"]["groups"][0]["steps"][0]["status"] == "active"

    async with maker() as s:
        token = (await s.execute(__import__("sqlalchemy").select(ApprovalStep.action_token))).scalar_one()

    # ─── подписант открывает ссылку без авторизации ──────────────────────────
    resp = await client.get(f"{PREFIX}/public/sign/{token}")
    assert resp.status_code == 200, resp.text
    public = resp.json()
    assert public["title"] == "О направлении на обучение"
    assert public["author_name"] == "Автор Служебки"
    assert "id" not in public, "держателю ссылки внутренние идентификаторы не нужны"

    # Поля приходят уже с подписью и в читаемом виде.
    assert public["fields"] == [
        {"name": "days", "label": "days", "type": None, "value": 5, "display": "5"}
    ]

    # ─── подписант видит сам документ, а не пересказ полей ───────────────────
    resp = await client.get(f"{PREFIX}/public/sign/{token}/pdf")
    assert resp.status_code == 200, resp.text
    assert resp.content.startswith(b"%PDF-")
    assert resp.headers["content-disposition"].startswith("inline"), (
        "документ должен открываться в браузере, а не уходить в загрузки"
    )

    # ─── решение ─────────────────────────────────────────────────────────────
    resp = await client.post(
        f"{PREFIX}/public/sign/{token}/decide",
        json={"approve": True, "signer_name": "Иван Петров"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["memo_status"] == "approved"

    # ─── повторное открытие ссылки ───────────────────────────────────────────
    resp = await client.get(f"{PREFIX}/public/sign/{token}")
    assert resp.status_code == 410
    assert "уже использована" in resp.json()["detail"]

    resp = await client.post(
        f"{PREFIX}/public/sign/{token}/decide", json={"approve": True}
    )
    assert resp.status_code == 410, "повторно подписать по той же ссылке нельзя"

    resp = await client.get(f"{PREFIX}/public/sign/{token}/pdf")
    assert resp.status_code == 410, "документ по погашенной ссылке больше не отдаётся"

    # Автору тот же документ доступен под его учётной записью.
    resp = await client.get(f"{PREFIX}/memos/{memo_id}/pdf", headers=headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")

    resp = await client.get(f"{PREFIX}/memos/{memo_id}/pdf")
    assert resp.status_code == 401, "без токена документ не отдаётся"

    # ─── итоговое состояние ──────────────────────────────────────────────────
    resp = await client.get(f"{PREFIX}/memos/{memo_id}", headers=headers)
    assert resp.json()["status"] == "approved"
    step = resp.json()["route"]["groups"][0]["steps"][0]
    assert step["status"] == "approved"
    assert step["signer_name"] == "Иван Петров"


async def test_draft_is_private_to_its_author(api):
    client, maker = api

    async with maker() as s:
        s.add(
            User(
                email="other@example.com",
                full_name="Другой Сотрудник",
                role=UserRole.author,
                password_hash=hash_password("anothersecret1"),
            )
        )
        await s.commit()

    author = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "author@example.com", "password": "verysecret123"},
    )
    author_headers = {"Authorization": f"Bearer {author.json()['access_token']}"}

    created = await client.post(
        f"{PREFIX}/memos", headers=author_headers, json={"title": "Личный черновик"}
    )
    memo_id = created.json()["id"]

    other = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "other@example.com", "password": "anothersecret1"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    resp = await client.get(f"{PREFIX}/memos/{memo_id}", headers=other_headers)
    assert resp.status_code == 403


async def test_wrong_password_is_indistinguishable_from_unknown_user(api):
    client, _ = api

    unknown = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "nobody@example.com", "password": "whatever123"},
    )
    wrong = await client.post(
        f"{PREFIX}/auth/login",
        json={"email": "author@example.com", "password": "wrongpassword"},
    )

    assert unknown.status_code == wrong.status_code == 401
    # Разные сообщения превратили бы форму входа в справочник сотрудников.
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_health_is_open(api):
    client, _ = api
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
