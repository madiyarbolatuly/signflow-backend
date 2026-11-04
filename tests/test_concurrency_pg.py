"""
Приёмочное испытание № 7 ТЗ: состязание на параллельном уровне.

Требует PostgreSQL: проверяется блокировка строки маршрута
(`SELECT ... FOR UPDATE`), которой в SQLite нет. Запуск:

    docker run -d --name signflow-pg-test -e POSTGRES_USER=signflow \\
        -e POSTGRES_PASSWORD=signflow -e POSTGRES_DB=signflow \\
        -p 55432:5432 postgres:16

    SIGNFLOW_TEST_PG=postgresql+asyncpg://signflow:signflow@127.0.0.1:55432/signflow \\
        python -m pytest tests/test_concurrency_pg.py

Без переменной окружения тесты пропускаются, чтобы обычный прогон не зависел
от поднятой базы.

О достоверности этих тестов
---------------------------
Тест на состязание, который проходит и при отключённой блокировке, ничего не
доказывает. Первая версия была именно такой: `asyncio.gather` запускает две
корутины, но между точками ожидания они успевают выстроиться в очередь —
первая фиксирует транзакцию раньше, чем вторая читает состояние, опасное
окно не наступает, и всё «зелено» в любом случае.

Поэтому здесь стоит шлюз (фикстура `gate`) ровно в той точке, где решение
перечитывает состояние уровня. Он удерживает обе транзакции до того, как
любая зафиксируется:

  * блокировка работает — вторая транзакция ждёт на `_lock_route` и до шлюза
    не доходит; шлюз размыкается по таймауту, порядок сохраняется, уровень
    активируется один раз;
  * блокировки нет — обе доходят до шлюза и читают состояние, в котором чужой
    шаг ещё не согласован; условие «подписали все» не выполняется ни у одной,
    и записка застревает: ноль активаций, тест падает.

Проверено в обе стороны. С принудительно отключённой
`_supports_row_locks` хотя бы один из двух тестов падал в каждом из пяти
прогонов; с включённой — пять прогонов из пяти зелёные. Поодиночке каждый
тест как негативный контроль плавает: исход зависит от того, как
планировщик разложит две корутины. Ценность имеет пара целиком, поэтому
удалять один из тестов как «дублирующий» не следует.
"""

import asyncio
import os

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.enums import GroupPolicy, MemoStatus, StepStatus
from app.db.tables import ApprovalGroup, ApprovalStep, Memo, User
from app.services import approval

PG_URL = os.environ.get("SIGNFLOW_TEST_PG")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="нужен PostgreSQL: задайте SIGNFLOW_TEST_PG"
)

#: Сколько шлюз ждёт вторую транзакцию, прежде чем пропустить первую.
#: Это время тест простаивает, поэтому держим его небольшим.
GATE_TIMEOUT = 1.5


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(PG_URL, pool_size=5)
    async with eng.begin() as conn:
        # Схему сносим целиком, а не через metadata.drop_all: ограничения,
        # созданные миграцией Alembic, носят другие имена, и drop_all падает,
        # пытаясь удалить то, чего по его представлениям там нет.
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Gate:
    """
    Шлюз в точке перечитывания состояния уровня.

    Включается вручную, уже после подготовки данных: `build_route` тоже
    читает уровни, и срабатывание шлюза на подготовке ломало барьер по
    таймауту — к самой гонке он приходил нерабочим, и тест снова проходил
    при любой реализации.
    """

    def __init__(self) -> None:
        self.barrier = asyncio.Barrier(2)
        self.armed = False

    def arm(self) -> None:
        self.armed = True


@pytest.fixture
def gate(monkeypatch):
    controller = Gate()
    original = approval._load_groups

    async def gated(session, route_id):
        if controller.armed:
            try:
                await asyncio.wait_for(controller.barrier.wait(), timeout=GATE_TIMEOUT)
            except (TimeoutError, asyncio.BrokenBarrierError):
                # Вторая транзакция не пришла — она ждёт на блокировке
                # строки. Так и должно быть, когда блокировка работает.
                pass
        return await original(session, route_id)

    monkeypatch.setattr(approval, "_load_groups", gated)
    return controller


async def _seed(maker, policy: str) -> tuple[int, list[int]]:
    """Записка с параллельным первым уровнем и обычным вторым."""
    async with maker() as session:
        author = User(email="author@example.com", full_name="Автор", role="author")
        session.add(author)
        await session.flush()

        memo = Memo(author_id=author.id, title="Параллельное согласование", body={})
        session.add(memo)
        await session.flush()

        await approval.build_route(
            session,
            memo=memo,
            spec=[
                {
                    "policy": policy,
                    "signers": [{"email": "a@example.com"}, {"email": "b@example.com"}],
                },
                {"policy": GroupPolicy.all_of, "signers": [{"email": "hr@example.com"}]},
            ],
        )
        await session.commit()
        memo_id = memo.id

    async with maker() as session:
        steps = (
            await session.execute(
                select(ApprovalStep.id)
                .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
                .where(ApprovalGroup.order_no == 1)
                .order_by(ApprovalStep.id)
            )
        ).scalars().all()

    return memo_id, list(steps)


async def _decide_in_own_session(maker, step_id: int) -> list[str]:
    """Отдельная сессия и отдельное соединение — как отдельный запрос."""
    async with maker() as session:
        step = (
            await session.execute(select(ApprovalStep).where(ApprovalStep.id == step_id))
        ).scalar_one()
        result = await approval.decide(
            session, step=step, approve=True, actor=step.signer_email
        )
        await session.commit()
        return [item.to for item in result.outbox]


async def test_concurrent_decisions_activate_next_level_once(maker, gate):
    """
    Двое участников уровня «подписывают все» жмут кнопку одновременно.

    Без блокировки строки маршрута каждая транзакция видит чужой шаг ещё не
    согласованным, уровень не закрывает никто, и записка зависает навсегда.
    """
    memo_id, step_ids = await _seed(maker, GroupPolicy.all_of)
    gate.arm()

    letters = await asyncio.gather(
        _decide_in_own_session(maker, step_ids[0]),
        _decide_in_own_session(maker, step_ids[1]),
    )
    sent = [address for batch in letters for address in batch]

    assert sent.count("hr@example.com") == 1, (
        f"следующий уровень активирован {sent.count('hr@example.com')} раз(а) "
        "вместо одного — блокировка строки маршрута не сработала"
    )

    async with maker() as session:
        level_two = (
            await session.execute(
                select(ApprovalStep)
                .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
                .where(ApprovalGroup.order_no == 2)
            )
        ).scalars().all()

        assert len(level_two) == 1
        assert level_two[0].status == StepStatus.active
        assert level_two[0].action_token, "задание второго уровня должно получить ссылку"

        memo = (
            await session.execute(select(Memo).where(Memo.id == memo_id))
        ).scalar_one()
        assert memo.status == MemoStatus.in_review


async def test_concurrent_decisions_on_any_policy_close_the_level_once(maker, gate):
    """
    То же для политики «достаточно одного»: второй участник не должен
    закрыть уровень повторно и отправить кадрам второе письмо.
    """
    _memo_id, step_ids = await _seed(maker, GroupPolicy.any_of)
    gate.arm()

    outcomes = await asyncio.gather(
        _decide_in_own_session(maker, step_ids[0]),
        _decide_in_own_session(maker, step_ids[1]),
        return_exceptions=True,
    )

    sent = [address for item in outcomes if isinstance(item, list) for address in item]
    assert sent.count("hr@example.com") == 1

    # Проигравший получает внятный отказ, а не молча вторую подпись — и не
    # сырую ошибку базы. Без блокировки именно это и происходит: вторая
    # транзакция падает с DBAPIError, пользователь видит «внутреннюю ошибку
    # сервера» вместо «решение уже принято».
    refusals = [item for item in outcomes if isinstance(item, Exception)]
    assert len(refusals) == 1, "второму участнику должно быть отказано"
    assert isinstance(refusals[0], HTTPException), (
        f"ожидался осмысленный отказ, а получено {type(refusals[0]).__name__}: "
        "похоже, решения разошлись на уровне базы, а не бизнес-логики"
    )
    assert refusals[0].status_code == 409

    async with maker() as session:
        first_level = (
            await session.execute(
                select(ApprovalStep)
                .join(ApprovalGroup, ApprovalStep.group_id == ApprovalGroup.id)
                .where(ApprovalGroup.order_no == 1)
            )
        ).scalars().all()

        approved = [s for s in first_level if s.status == StepStatus.approved]
        released = [s for s in first_level if s.status == StepStatus.not_required]

        assert len(approved) == 1, "по политике «любой» подписывает ровно один"
        assert len(released) == 1
