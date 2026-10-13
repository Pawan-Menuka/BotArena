from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError

from app.config import Settings
from app.db.base import Base, create_database
from app.db.models import Debate, DebateMessage, ExecutionAttempt, Judgment
from app.db.repo import DebateRepository, load_speeches
from app.engine.models import TURN_ORDER, ROUND_NUMBER, Speech, Topic
from app.engine.orchestrator import OrchestratorError, validate_checkpoint
from app.engine.provider import FakeProvider
from app.engine.retry import RetryPolicy

TOPIC = Topic("test", "Test topic", "For", "Against")


async def no_sleep(_delay):
    pass


FAST = RetryPolicy(sleep=no_sleep, jitter=lambda: 0)


@pytest.fixture
async def database(tmp_path):
    engine, sessions = create_database(tmp_path / "test.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine, sessions, DebateRepository(sessions)
    await engine.dispose()


async def create(repo):
    return await repo.create(topic=TOPIC, client_request_id=str(uuid4()), execution_token_hash="a" * 64)


def speech(position=1):
    round_name, agent, side = TURN_ORDER[position - 1]
    return Speech(position, round_name, ROUND_NUMBER[round_name], agent, side, "Saved speech", str(uuid4()))


async def test_turn_four_failure_resumes_without_changing_saved_prefix(database):
    engine, sessions, repo = database
    debate_id = await create(repo)
    with pytest.raises(OrchestratorError):
        await repo.execute(debate_id, provider=FakeProvider(speeches=["one", "two", "three", "", "", ""]), model_id="fake", retry_policy=FAST)
    async with sessions() as session:
        prefix = await load_speeches(session, debate_id)
        failed = await session.get(Debate, debate_id)
        assert failed.status == "failed"
        assert failed.failed_stage == "speech_turn_4"
        assert failed.failed_turn == 4
        assert failed.total_calls == 6
        assert len(prefix) == 3
        for item in prefix:
            UUID(item.message_id)
    # A new engine/repository also reconstructs the checkpoint; no in-memory state is needed.
    await engine.dispose()
    provider = FakeProvider(speeches=["four", "five", "six"])
    result = await repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
    assert len(provider.calls) == 4
    assert result.speeches[:3] == prefix
    assert prefix[0].message_id in provider.calls[0][1]
    async with sessions() as session:
        debate = await session.get(Debate, debate_id)
        assert (debate.status, debate.accepted_attempts, debate.total_calls) == ("completed", 2, 10)
        assert (await load_speeches(session, debate_id))[:3] == prefix
        judgment = await session.scalar(select(Judgment).where(Judgment.debate_id == debate_id))
        assert judgment.pro_total == 80
        assert judgment.con_total == 75
        assert judgment.winner == "draw"
        attempts = (await session.scalars(select(ExecutionAttempt).order_by(ExecutionAttempt.attempt_number))).all()
        assert [(a.status, a.starting_checkpoint, a.call_count) for a in attempts] == [("failed", 0, 6), ("completed", 3, 4)]


async def test_judging_only_resume(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    with pytest.raises(OrchestratorError):
        await repo.execute(debate_id, provider=FakeProvider(judgment={}), model_id="fake", retry_policy=FAST)
    async with sessions() as session:
        prefix = await load_speeches(session, debate_id)
        assert len(prefix) == 6
    provider = FakeProvider(speeches=[])
    result = await repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
    assert len(provider.calls) == 1
    assert result.speeches == prefix


async def test_pragmas_on_every_connection(database):
    engine, _, _ = database
    async with engine.connect() as first, engine.connect() as second:
        for connection in (first, second):
            for pragma, expected in (("journal_mode", "wal"), ("busy_timeout", 5000), ("foreign_keys", 1)):
                assert await connection.scalar(text(f"PRAGMA {pragma}")) == expected


async def test_duplicate_turn_constraint_rolls_back_revision(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    checkpoints, _, _ = await repo.begin_attempt(debate_id, model_id="fake")
    saved = speech()
    await checkpoints.commit_speech(saved)
    async with sessions() as session:
        revision = (await session.get(Debate, debate_id)).revision
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            await session.execute(update(Debate).where(Debate.id == debate_id).values(revision=999))
            session.add(DebateMessage(debate_id=debate_id, agent="atlas", side="pro", round_number=1, turn_order=1, content="duplicate"))
    async with sessions() as session:
        assert (await session.get(Debate, debate_id)).revision == revision
        assert await load_speeches(session, debate_id) == [saved]


async def test_speech_write_failure_rolls_back_progress(database):
    engine, sessions, repo = database
    debate_id = await create(repo)
    checkpoints, _, _ = await repo.begin_attempt(debate_id, model_id="fake")
    async with sessions() as session:
        revision = (await session.get(Debate, debate_id)).revision

    def fail_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO debate_messages"):
            raise RuntimeError("simulated disk failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(RuntimeError):
            await checkpoints.commit_speech(speech())
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_insert)
    async with sessions() as session:
        assert (await session.get(Debate, debate_id)).revision == revision
        assert await load_speeches(session, debate_id) == []


async def test_concurrent_debates_and_no_transaction_during_provider(database):
    engine, sessions, repo = database
    ids = [await create(repo), await create(repo)]
    active_transactions = set()
    event.listen(engine.sync_engine, "begin", lambda connection: active_transactions.add(id(connection)))
    event.listen(engine.sync_engine, "commit", lambda connection: active_transactions.discard(id(connection)))
    event.listen(engine.sync_engine, "rollback", lambda connection: active_transactions.discard(id(connection)))

    class CheckingProvider(FakeProvider):
        async def complete(self, **kwargs):
            # The provider must not keep its worker's connection checked out.
            assert engine.sync_engine.pool.checkedout() <= 1
            await asyncio.sleep(0.001)
            return await super().complete(**kwargs)

    providers = [CheckingProvider(), CheckingProvider()]
    results = await asyncio.gather(*(repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
                                     for debate_id, provider in zip(ids, providers)))
    assert [len(p.calls) for p in providers] == [7, 7]
    assert not active_transactions
    assert all(len(result.speeches) == 6 for result in results)
    async with sessions() as session:
        assert len((await session.scalars(select(DebateMessage))).all()) == 12
        assert len((await session.scalars(select(Judgment))).all()) == 2


async def test_single_provider_has_no_open_transaction(database):
    engine, _, repo = database
    debate_id = await create(repo)

    class CheckingProvider(FakeProvider):
        async def complete(self, **kwargs):
            assert engine.sync_engine.pool.checkedout() == 0
            return await super().complete(**kwargs)

    await repo.execute(debate_id, provider=CheckingProvider(), model_id="fake", retry_policy=FAST)


async def test_failed_judgment_insert_rolls_back_completion(database):
    engine, sessions, repo = database
    debate_id = await create(repo)

    def fail_insert(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO judgments"):
            raise OperationalError(statement, {}, Exception("simulated disk failure"))

    event.listen(engine.sync_engine, "before_cursor_execute", fail_insert)
    provider = FakeProvider()
    try:
        with pytest.raises(OperationalError):
            await repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_insert)
    assert len(provider.calls) == 7
    async with sessions() as session:
        debate = await session.get(Debate, debate_id)
        assert debate.status == "judging"
        assert debate.completed_at is None
        assert debate.active_run_id is not None
        assert len(await load_speeches(session, debate_id)) == 6
        assert await session.scalar(select(Judgment)) is None
        attempt = await session.get(ExecutionAttempt, debate.active_run_id)
        assert attempt.status == "active"
        assert attempt.finished_at is None


async def test_failure_is_sanitized_and_config_error_can_resume_after_fix(database):
    from app.engine.provider import ProviderError

    _, sessions, repo = database
    debate_id = await create(repo)

    class BrokenProvider(FakeProvider):
        async def complete(self, **kwargs):
            self.calls.append((kwargs["system"], kwargs["user"]))
            raise ProviderError("SECRET provider diagnostics", retryable=False)

    provider = BrokenProvider()
    with pytest.raises(OrchestratorError):
        await repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
    assert len(provider.calls) == 1
    async with sessions() as session:
        debate = await session.get(Debate, debate_id)
        assert debate.error_code == "provider_configuration"
        assert "SECRET" not in debate.error_message
        attempt = await session.scalar(select(ExecutionAttempt))
        assert attempt.status == "failed"
        assert attempt.finished_at is not None
    result = await repo.execute(debate_id, provider=FakeProvider(), model_id="fake", retry_policy=FAST)
    assert len(result.speeches) == 6


async def test_cancel_preserves_checkpoint_and_marks_attempt_interrupted(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    waiting = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            if len(self.calls) == 1:
                waiting.set()
                await asyncio.Event().wait()
            return await super().complete(**kwargs)

    task = asyncio.create_task(repo.execute(debate_id, provider=WaitingProvider(), model_id="fake", retry_policy=FAST))
    await asyncio.wait_for(waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as session:
        debate = await session.get(Debate, debate_id)
        assert (debate.status, debate.error_code) == ("failed", "server_interrupted")
        assert len(await load_speeches(session, debate_id)) == 1
        attempt = await session.scalar(select(ExecutionAttempt))
        assert attempt.status == "interrupted"


async def test_write_contention_waits_without_redispatch(database):
    engine, sessions, repo = database
    debate_id = await create(repo)
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    first_output = asyncio.Event()

    async def lock_writer():
        async with sessions.begin() as session:
            await session.execute(update(Debate).where(Debate.id == debate_id).values(updated_at=Debate.updated_at))
            lock_acquired.set()
            await release_lock.wait()

    lock_task = None

    class ContendedProvider(FakeProvider):
        async def complete(self, **kwargs):
            nonlocal lock_task
            response = await super().complete(**kwargs)
            if len(self.calls) == 1:
                lock_task = asyncio.create_task(lock_writer())
                await lock_acquired.wait()
                first_output.set()
            return response

    provider = ContendedProvider()
    worker = asyncio.create_task(repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST))
    await asyncio.wait_for(first_output.wait(), timeout=5)
    await asyncio.sleep(0.03)
    assert not worker.done()
    release_lock.set()
    await lock_task
    await asyncio.wait_for(worker, timeout=5)
    assert len(provider.calls) == 7


@pytest.mark.parametrize("mutation", [lambda s: replace(s, turn_order=2), lambda s: replace(s, side=TURN_ORDER[1][2]),
                                     lambda s: replace(s, agent=TURN_ORDER[1][1]), lambda s: replace(s, round_number=2),
                                     lambda s: replace(s, round=TURN_ORDER[2][0])])
def test_checkpoint_rejects_invalid_prefix(mutation):
    with pytest.raises(OrchestratorError, match="checkpoint"):
        validate_checkpoint([mutation(speech())])


async def test_corrupt_checkpoint_is_non_resumable_and_consumes_no_attempt(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    async with sessions.begin() as session:
        session.add(DebateMessage(debate_id=debate_id, agent="nova", side="con", round_number=1, turn_order=2, content="gap"))
    provider = FakeProvider()
    for _ in range(2):
        with pytest.raises(OrchestratorError):
            await repo.execute(debate_id, provider=provider, model_id="fake", retry_policy=FAST)
    assert not provider.calls
    async with sessions() as session:
        debate = await session.get(Debate, debate_id)
        assert (debate.accepted_attempts, debate.error_code, debate.status) == (0, "checkpoint_invalid", "failed")


async def test_stale_run_cannot_commit(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    checkpoints, _, _ = await repo.begin_attempt(debate_id, model_id="fake")
    async with sessions.begin() as session:
        await session.execute(update(Debate).where(Debate.id == debate_id).values(active_run_id=str(uuid4())))
    with pytest.raises(OrchestratorError, match="ownership"):
        await checkpoints.commit_speech(speech())
    async with sessions() as session:
        assert await load_speeches(session, debate_id) == []


async def test_attempt_limit_and_call_limit(database):
    _, sessions, repo = database
    debate_id = await create(repo)
    for _ in range(3):
        with pytest.raises(OrchestratorError):
            await repo.execute(debate_id, provider=FakeProvider(speeches=["", "", ""]), model_id="fake", retry_policy=FAST)
    rejected_provider = FakeProvider()
    with pytest.raises(OrchestratorError):
        await repo.execute(debate_id, provider=rejected_provider, model_id="fake", retry_policy=FAST)
    assert not rejected_provider.calls
    other = await create(repo)
    async with sessions.begin() as session:
        await session.execute(update(Debate).where(Debate.id == other).values(total_calls=29))
    provider = FakeProvider()
    with pytest.raises(OrchestratorError) as error:
        await repo.execute(other, provider=provider, model_id="fake", retry_policy=FAST)
    assert error.value.code == "debate_call_limit"
    assert len(provider.calls) == 1
    async with sessions() as session:
        assert (await session.get(Debate, other)).total_calls == 30


def test_migration_upgrade_check_and_downgrade(tmp_path, monkeypatch):
    path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.check(config)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"debates", "debate_messages", "judgments", "execution_attempts", "creation_quota_records", "provider_call_reservations"} <= tables
    command.downgrade(config, "base")
    command.upgrade(config, "head")


def test_settings(monkeypatch):
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("DEPLOYMENT_MODE", raising=False)
    assert Settings().database_path == Path("./data/botarena.db")
    assert Settings().deployment_mode == "local"
    monkeypatch.setenv("DEPLOYMENT_MODE", "invalid")
    with pytest.raises(ValueError):
        Settings()
