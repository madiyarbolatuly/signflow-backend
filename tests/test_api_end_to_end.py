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


# ─── уведомления ─────────────────────────────────────────────────────────────
async def _login(client, email: str, password: str) -> dict:
    resp = await client.post(
        f"{PREFIX}/auth/login", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def test_author_sees_the_outcome_in_notifications(api):
    from sqlalchemy import select

    client, maker = api
    headers = await _login(client, "author@example.com", "verysecret123")

    empty = await client.get(f"{PREFIX}/notifications", headers=headers)
    assert empty.json() == {"unread": 0, "items": []}

    created = await client.post(
        f"{PREFIX}/memos", headers=headers, json={"title": "Отпуск", "body": {}}
    )
    memo_id = created.json()["id"]
    await client.post(
        f"{PREFIX}/memos/{memo_id}/submit",
        headers=headers,
        json={"route": [{"policy": "all", "signers": [{"email": "boss@example.com"}]}]},
    )

    async with maker() as s:
        token = (await s.execute(select(ApprovalStep.action_token))).scalar_one()
    await client.post(f"{PREFIX}/public/sign/{token}/decide", json={"approve": True})

    listing = (await client.get(f"{PREFIX}/notifications", headers=headers)).json()
    assert listing["unread"] == 1
    item = listing["items"][0]
    assert "согласована" in item["message"]
    assert item["link"] == f"/memos/{memo_id}"
    assert item["status"] == "unread"

    # Чужое уведомление неотличимо от несуществующего.
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
    other = await _login(client, "other@example.com", "anothersecret1")
    resp = await client.post(f"{PREFIX}/notifications/{item['id']}/read", headers=other)
    assert resp.status_code == 404
    still = (await client.get(f"{PREFIX}/notifications", headers=headers)).json()
    assert still["unread"] == 1, "чужой запрос не должен отметить уведомление"

    resp = await client.post(f"{PREFIX}/notifications/{item['id']}/read", headers=headers)
    assert resp.status_code == 200
    after = (await client.get(f"{PREFIX}/notifications", headers=headers)).json()
    assert after["unread"] == 0
    assert after["items"][0]["status"] == "read"

    assert (await client.get(f"{PREFIX}/notifications")).status_code == 401


# ─── заявления по бланкам ────────────────────────────────────────────────────
async def test_dismissal_statement_from_preview_to_signed_pdf(api):
    """
    Заявление на увольнение: предпросмотр до сохранения, карточка, страница
    подписания и PDF показывают один и тот же документ по образцу бланка.
    """
    from sqlalchemy import select

    from app.db.tables import MemoTemplate
    from app.scripts.seed import apply_templates

    client, maker = api
    async with maker() as s:
        await apply_templates(s)
        await s.commit()
        template_id = (
            await s.execute(select(MemoTemplate.id).where(MemoTemplate.code == "dismissal"))
        ).scalar_one()

    headers = await _login(client, "author@example.com", "verysecret123")

    templates = (await client.get(f"{PREFIX}/admin/templates", headers=headers)).json()
    assert [t["code"] for t in templates] == ["hiring", "annual_leave", "dismissal"]

    # ─── предпросмотр до сохранения: пустое поле видно как пропуск ──────────
    resp = await client.post(
        f"{PREFIX}/memos/render", headers=headers,
        json={"template_id": template_id, "body": {}},
    )
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    assert preview["fields"][0]["missing"] is True
    assert "________" in preview["document"]["paragraphs"][0]
    assert preview["document"]["sender"][-1]["value"] == "Автор Служебки"

    resp = await client.post(
        f"{PREFIX}/memos/render", headers=headers,
        json={"template_id": template_id, "body": {"dismissal_date": "2026-12-15"}},
    )
    assert resp.json()["document"]["paragraphs"] == [
        "Прошу Вас расторгнуть со мной трудовой договор по собственному желанию "
        "15 декабря 2026 года."
    ]

    # ─── отправка ────────────────────────────────────────────────────────────
    created = await client.post(
        f"{PREFIX}/memos", headers=headers,
        json={
            "title": "Заявление на увольнение",
            "template_id": template_id,
            "body": {"dismissal_date": "2026-12-15"},
            "addressee": "Директору\nТОО «GQ-Group Invest»",
        },
    )
    memo_id = created.json()["id"]
    submitted = await client.post(
        f"{PREFIX}/memos/{memo_id}/submit", headers=headers,
        json={"route": [{"policy": "all", "signers": [{"email": "boss@example.com"}]}]},
    )
    assert submitted.status_code == 200, submitted.text
    document = submitted.json()["document"]
    assert document["addressee"] == ["Директору", "ТОО «GQ-Group Invest»"], (
        "адресат, заданный в записке, важнее адресата шаблона"
    )
    assert "15 декабря 2026 года" in document["paragraphs"][0]

    # ─── подписант видит тот же документ ─────────────────────────────────────
    async with maker() as s:
        token = (await s.execute(select(ApprovalStep.action_token))).scalar_one()
    public = (await client.get(f"{PREFIX}/public/sign/{token}")).json()
    assert public["document"] == document

    pdf = await client.get(f"{PREFIX}/public/sign/{token}/pdf")
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")

