from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from app.config import Settings
from app.core.costs import reserve
from app.db.base import Base, create_database
from app.db.models import Debate, ExecutionAttempt, ProviderCallReservation
from app.db.repo import DebateRepository
from app.engine.budget import authorize_dispatch, input_bound, price_for, reserved_dispatch
from app.engine.lifecycle import reconcile
from app.engine.models import Topic
from app.engine.provider import FakeProvider, GroqProvider, ProviderError, ProviderResponse
from app.engine.retry import OrchestratorError, RetryPolicy
from app.main import create_app

MODEL = "openai/gpt-oss-20b"
TOPIC = Topic("test", "Test topic", "For", "Against")
CALL = {"system": "System", "user": "User", "max_output_tokens": 512, "json_schema": None}


async def no_sleep(_delay):
    pass


FAST = RetryPolicy(sleep=no_sleep, jitter=lambda: 0)


@pytest.fixture
async def ledger(tmp_path):
    settings = Settings(database_path=tmp_path / "ledger.db", input_accounting_confirmed=True,
                        max_reserved_cost_24h_usd=Decimal("1.00"))
    engine, sessions = create_database(settings.database_path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine, sessions, DebateRepository(sessions, settings)
    await engine.dispose()


async def create(repo):
    return await repo.create(topic=TOPIC, client_request_id=str(uuid4()), execution_token_hash="a" * 64)


async def claim(repo, model=MODEL):
    id = await create(repo)
    checkpoints, _, _ = await repo.begin_attempt(id, model_id=model)
    return checkpoints


async def test_every_persistent_call_has_a_committed_reservation_before_dispatch(ledger):
    engine, sessions, repo = ledger
    class ObservingProvider(FakeProvider):
        async def complete(self, **kwargs):
            assert engine.sync_engine.pool.checkedout() == 0
            async with sessions() as session:
                row = await session.scalar(select(ProviderCallReservation).where(ProviderCallReservation.dispatch_state == "dispatched"))
                assert row is not None
                assert (await session.get(Debate, row.debate_id)).total_calls == len(self.calls) + 1
                assert row.output_token_cap == kwargs["max_output_tokens"] + 1500
            return await super().complete(**kwargs)
    provider = ObservingProvider()
    await repo.execute(await create(repo), provider=provider, model_id="fake", retry_policy=FAST)
    async with sessions() as session:
        rows = (await session.scalars(select(ProviderCallReservation))).all()
        assert len(rows) == 7 == len(provider.calls)
        assert {row.dispatch_state for row in rows} == {"committed"}
        assert {row.input_token_cap for row in rows} == {8192}
        assert sorted(row.output_token_cap for row in rows) == [2012] * 6 + [3548]


@pytest.mark.parametrize("budget", ["calls", "cents"])
async def test_competing_last_global_allowance_cannot_overspend(ledger, budget):
    _, sessions, original = ledger
    settings = replace(original.settings, global_call_quota_24h=1 if budget == "calls" else 300,
                       max_reserved_cost_24h_usd=Decimal("0.01") if budget == "cents" else Decimal("1.00"))
    repo = DebateRepository(sessions, settings)
    claims = [await claim(repo), await claim(repo)]
    results = await asyncio.gather(*(reserve(checkpoint, "speech_turn_1", False, **CALL) for checkpoint in claims), return_exceptions=True)
    assert sum(isinstance(result, str) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, OrchestratorError))
    assert rejected.code == "global_usage_limit"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)) == 1
        assert await session.scalar(select(func.sum(ProviderCallReservation.reserved_max_cost_cents))) == 1
        assert await session.scalar(select(func.sum(Debate.total_calls))) == 1
        assert await session.scalar(select(func.sum(ExecutionAttempt.call_count))) == 1


async def test_stage_budget_is_enforced_in_durable_reservation(ledger):
    _, sessions, repo = ledger
    checkpoints = await claim(repo)
    for _ in range(3):
        await reserve(checkpoints, "speech_turn_1", False, **CALL)
    with pytest.raises(OrchestratorError) as error:
        await reserve(checkpoints, "speech_turn_1", True, **CALL)
    assert error.value.code == "stage_call_limit"
    async with sessions() as session:
        assert (await session.get(Debate, checkpoints.debate_id)).total_calls == 3


async def test_reserved_crash_remains_counted_after_reconnect_and_resume(ledger):
    engine, sessions, original = ledger
    settings = replace(original.settings, global_call_quota_24h=1)
    repo = DebateRepository(sessions, settings)
    checkpoints = await claim(repo)
    reservation_id = await reserve(checkpoints, "speech_turn_1", False, **CALL)
    await reconcile(sessions, set())
    await engine.dispose()
    provider = FakeProvider()
    with pytest.raises(OrchestratorError) as error:
        await repo.execute(checkpoints.debate_id, provider=provider, model_id=MODEL, retry_policy=FAST)
    assert error.value.code == "global_usage_limit"
    assert provider.calls == []
    async with sessions() as session:
        row = await session.get(ProviderCallReservation, reservation_id)
        assert row.dispatch_state == "abandoned"
        assert row.reserved_max_cost_cents == 1
        debate = await session.get(Debate, checkpoints.debate_id)
        assert debate.error_code == "server_interrupted"
        assert debate.accepted_attempts == 1


async def test_retries_and_next_day_resume_get_fresh_reservations(ledger):
    _, sessions, repo = ledger
    id = await create(repo)
    with pytest.raises(OrchestratorError):
        await repo.execute(id, provider=FakeProvider(speeches=["", "", ""]), model_id=MODEL, retry_policy=FAST)
    async with sessions.begin() as session:
        rows = (await session.scalars(select(ProviderCallReservation))).all()
        assert len(rows) == 3
        assert {row.dispatch_state for row in rows} == {"failed"}
        await session.execute(update(ProviderCallReservation).values(reserved_at=datetime.now(timezone.utc) - timedelta(days=1, minutes=1)))
    await repo.execute(id, provider=FakeProvider(), model_id=MODEL, retry_policy=FAST)
    async with sessions() as session:
        rows = (await session.scalars(select(ProviderCallReservation))).all()
        assert len(rows) == 10
        assert sum(row.reserved_max_cost_cents for row in rows) == 10
        assert len({row.run_id for row in rows}) == 2
        assert (await session.get(Debate, id)).total_calls == 10


async def test_usage_recorded_for_successful_and_truncated_responses_without_refunds(ledger):
    _, sessions, repo = ledger
    checkpoints = await claim(repo)
    class UsageProvider(FakeProvider):
        async def complete(self, **kwargs):
            return ProviderResponse("valid", input_tokens=100, output_tokens=200)
    await checkpoints.dispatch(UsageProvider(), "speech_turn_1", **CALL)
    class TruncatedProvider(FakeProvider):
        async def complete(self, **kwargs):
            raise ProviderError("Cut off", input_tokens=200, output_tokens=2012)
    with pytest.raises(ProviderError):
        await checkpoints.dispatch(TruncatedProvider(), "speech_turn_1", **CALL)
    async with sessions() as session:
        rows = (await session.scalars(select(ProviderCallReservation).order_by(ProviderCallReservation.reserved_at))).all()
        assert [(row.actual_input_tokens, row.actual_output_tokens) for row in rows] == [(100, 200), (200, 2012)]
        assert [row.actual_cost_cents for row in rows] == [1, 1]
        assert sum(row.reserved_max_cost_cents for row in rows) == 2


async def test_oversized_prompt_rejected_before_reservation_or_provider(ledger):
    _, sessions, repo = ledger
    checkpoints = await claim(repo)
    provider = FakeProvider()
    with pytest.raises(OrchestratorError) as error:
        await checkpoints.dispatch(provider, "speech_turn_1", **{**CALL, "user": "é" * 5000})
    assert error.value.code == "prompt_too_large"
    assert provider.calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)) == 0
        assert (await session.get(Debate, checkpoints.debate_id)).total_calls == 0


async def test_ownership_loss_prevents_reservation_and_dispatch(ledger):
    _, sessions, repo = ledger
    checkpoints = await claim(repo)
    async with sessions.begin() as session:
        await session.execute(update(Debate).values(active_run_id=str(uuid4())))
    provider = FakeProvider()
    with pytest.raises(OrchestratorError) as error:
        await checkpoints.dispatch(provider, "speech_turn_1", **CALL)
    assert error.value.code == "stale_run"
    assert provider.calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)) == 0


async def test_mid_match_global_exhaustion_preserves_checkpoint_and_never_auto_resumes(ledger):
    _, sessions, original = ledger
    repo = DebateRepository(sessions, replace(original.settings, global_call_quota_24h=2))
    provider = FakeProvider()
    id = await create(repo)
    with pytest.raises(OrchestratorError) as error:
        await repo.execute(id, provider=provider, model_id=MODEL, retry_policy=FAST)
    assert error.value.code == "global_usage_limit"
    assert len(provider.calls) == 2
    async with sessions() as session:
        debate = await session.get(Debate, id)
        assert debate.status == "failed" and debate.failed_turn == 3
        assert debate.total_calls == 2
    await reconcile(sessions, set())
    assert len(provider.calls) == 2


async def test_accounting_breach_records_usage_and_latches_future_paid_dispatch(ledger):
    _, sessions, repo = ledger
    checkpoints = await claim(repo)
    class ExcessiveUsage(FakeProvider):
        async def complete(self, **kwargs):
            return ProviderResponse("valid", input_tokens=8193, output_tokens=10)
    with pytest.raises(OrchestratorError) as error:
        await checkpoints.dispatch(ExcessiveUsage(), "speech_turn_1", **CALL)
    assert error.value.code == "token_accounting_unconfirmed"
    provider = FakeProvider()
    id = await create(repo)
    with pytest.raises(OrchestratorError) as error:
        await repo.begin_attempt(id, model_id=MODEL)
    assert error.value.code == "token_accounting_unconfirmed"
    assert provider.calls == []
    async with sessions() as session:
        row = await session.scalar(select(ProviderCallReservation))
        assert row.actual_input_tokens == 8193


def test_verified_price_and_conservative_integer_cents_include_reasoning():
    price = price_for(MODEL)
    assert price.version == "groq-standard-2026-10-12"
    assert price.input_rate == Decimal("0.075")
    assert price.output_rate == Decimal("0.30")
    assert price.cents(8192, 512 + 1500) == 1
    assert price.cents(8192, 2048 + 1500) == 1
    assert price_for("fake").cents(8192, 3548) == 0
    with pytest.raises(ValueError):
        price_for("unconfirmed-model")
    assert input_bound(**{**CALL, "user": "é"}) > input_bound(**{**CALL, "user": "e"})


def test_dispatch_permit_is_request_bound_and_single_use():
    with pytest.raises(ValueError):
        authorize_dispatch(MODEL, **CALL)
    with reserved_dispatch(MODEL, **CALL):
        with pytest.raises(ValueError):
            authorize_dispatch(MODEL, **{**CALL, "user": "changed"})
        authorize_dispatch(MODEL, **CALL)
        with pytest.raises(ValueError):
            authorize_dispatch(MODEL, **CALL)
    with pytest.raises(ValueError):
        authorize_dispatch(MODEL, **CALL)


async def test_groq_adapter_cannot_dispatch_without_reservation(monkeypatch):
    import groq
    called = []
    async def sdk_call(**kwargs):
        called.append(kwargs)
    monkeypatch.setattr(groq, "AsyncGroq", lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk_call))))
    provider = GroqProvider(api_key="offline", model=MODEL)
    with pytest.raises(ProviderError):
        await provider.complete(**CALL)
    assert called == []


async def test_offline_sdk_integration_consumes_matching_durable_permits(ledger, monkeypatch):
    import groq
    _, sessions, repo = ledger
    fake = FakeProvider()
    async def sdk_call(**kwargs):
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)
                                        .where(ProviderCallReservation.dispatch_state == "dispatched")) == 1
        response = await fake.complete(system=kwargs["messages"][0]["content"], user=kwargs["messages"][1]["content"],
                                       max_output_tokens=kwargs["max_completion_tokens"] - 1500,
                                       json_schema=kwargs.get("response_format", {}).get("json_schema", {}).get("schema"))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=response.text))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=200))
    def sdk_client(**kwargs):
        assert kwargs["max_retries"] == 0
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk_call)))
    monkeypatch.setattr(groq, "AsyncGroq", sdk_client)
    await repo.execute(await create(repo), provider=GroqProvider(api_key="offline", model=MODEL), model_id=MODEL, retry_policy=FAST)
    assert len(fake.calls) == 7
    async with sessions() as session:
        rows = (await session.scalars(select(ProviderCallReservation))).all()
        assert {row.dispatch_state for row in rows} == {"committed"}
        assert {row.actual_input_tokens for row in rows} == {100}


@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("0.001"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_cost_cap_rejected(value):
    with pytest.raises(ValueError):
        Settings(max_reserved_cost_24h_usd=value)


async def test_missing_shared_cost_cap_refuses_readiness(tmp_path):
    from tests.test_anti_abuse import shared_settings
    settings = replace(shared_settings(tmp_path / "missing-cap.db"), max_reserved_cost_24h_usd=None)
    app = create_app(settings)
    with pytest.raises(RuntimeError, match="MAX_RESERVED_COST_24H_USD"):
        async with app.router.lifespan_context(app):
            pytest.fail("Shared startup must fail closed")
    assert not app.state.ready


async def test_unconfirmed_accounting_blocks_paid_execution_without_consuming_calls(ledger):
    _, sessions, original = ledger
    repo = DebateRepository(sessions, replace(original.settings, input_accounting_confirmed=False))
    provider = FakeProvider()
    id = await create(repo)
    with pytest.raises(OrchestratorError) as error:
        await repo.execute(id, provider=provider, model_id=MODEL, retry_policy=FAST)
    assert error.value.code == "usage_configuration"
    assert provider.calls == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)) == 0


async def test_cli_fake_run_uses_durable_ledger_without_loading_dotenv(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from app.cli import main_async
    settings = Settings(database_path=tmp_path / "cli.db")
    monkeypatch.setattr("app.cli.Settings", lambda: settings)
    assert await main_async(Namespace(provider="fake", topic="four-day-work-week")) == 0
    assert "Winner: DRAW" in capsys.readouterr().out
    engine, sessions = create_database(settings.database_path)
    try:
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(ProviderCallReservation)) == 7
    finally:
        await engine.dispose()


async def test_usage_persistence_failure_never_redispatches_provider(ledger, monkeypatch):
    from sqlalchemy.exc import OperationalError
    _, _, repo = ledger
    async def broken(*args, **kwargs):
        raise OperationalError("offline storage failure", {}, Exception())
    monkeypatch.setattr("app.core.costs.record_usage", broken)
    provider = FakeProvider()
    with pytest.raises(OperationalError):
        await repo.execute(await create(repo), provider=provider, model_id=MODEL, retry_policy=FAST)
    assert len(provider.calls) == 1


async def test_admin_stats_authentication_usage_and_privacy(tmp_path):
    token = "offline-admin-secret-with-32-characters"
    settings = Settings(database_path=tmp_path / "stats.db", admin_token=token, input_accounting_confirmed=True,
                        max_reserved_cost_24h_usd=Decimal("1.00"))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        repo = DebateRepository(app.state.sessions, settings)
        provider = FakeProvider(speeches=["", "", ""])
        with pytest.raises(OrchestratorError):
            await repo.execute(await create(repo), provider=provider, model_id=MODEL, retry_policy=FAST)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for headers in ({}, {"X-Admin-Token": "wrong"}):
                assert (await client.get("/api/admin/stats", headers=headers)).status_code == 401
            response = await client.get("/api/admin/stats", headers={"X-Admin-Token": token})
            assert response.status_code == 200
            assert response.headers["Cache-Control"] == "no-store"
            data = response.json()
            assert data["provider_calls_24h"] == 3
            assert data["reserved_cost_24h_cents"] == 3
            assert data["actual_cost_unknown_calls"] == 3
            assert data["remaining_calls_24h"] == 297
            assert data["remaining_reserved_cost_24h_cents"] == 97
            assert data["failures"] == [{"stage": "speech_turn_1", "code": "provider_failed", "count": 1}]
            assert data["database_bytes"] > 0
            for secret in (token, "execution_token", "ip_bucket_hash", "127.0.0.1"):
                assert secret not in response.text


async def test_shared_fake_execution_uses_reservations_and_ip_admission(tmp_path):
    from tests.test_anti_abuse import shared_settings, creation, SECRET
    from tests.test_api import terminal
    gate = asyncio.Event()
    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            await gate.wait()
            return await super().complete(**kwargs)
    settings = shared_settings(tmp_path / "shared-flow.db")
    app = create_app(settings, provider_factory=WaitingProvider, retry_policy=FAST, shutdown_grace=0.01)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Fly-Client-IP": "192.0.2.1"}) as client:
            inputs = [creation(), creation()]
            ids = [(await client.post("/api/debates", **data)).json()["id"] for data in inputs]
            assert (await client.post(f"/api/debates/{ids[0]}/run", headers=inputs[0]["headers"])).status_code == 202
            blocked = await client.post(f"/api/debates/{ids[1]}/run", headers=inputs[1]["headers"])
            assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "concurrency_per_ip"
            gate.set()
            assert (await terminal(client, ids[0]))["status"] == "completed"
            data = (await client.get("/api/admin/stats", headers={"X-Admin-Token": SECRET})).json()
            assert data["provider_calls_24h"] == 7
            assert data["reserved_cost_24h_cents"] == 0
            assert data["actual_cost_known_calls"] == 7


async def test_exhausted_usage_rejects_http_admission_without_spending_attempt(tmp_path):
    from tests.test_anti_abuse import creation
    from tests.test_api import terminal
    settings = Settings(database_path=tmp_path / "admission.db", global_call_quota_24h=1)
    app = create_app(settings, retry_policy=FAST)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            inputs = [creation(), creation()]
            ids = [(await client.post("/api/debates", **data)).json()["id"] for data in inputs]
            assert (await client.post(f"/api/debates/{ids[0]}/run", headers=inputs[0]["headers"])).status_code == 202
            assert (await terminal(client, ids[0]))["error"]["code"] == "global_usage_limit"
            rejected = await client.post(f"/api/debates/{ids[1]}/run", headers=inputs[1]["headers"])
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "global_usage_limit"
            assert rejected.headers["Retry-After"] == "5"
            async with app.state.sessions() as session:
                debate = await session.get(Debate, ids[1])
                assert debate.status == "created" and debate.accepted_attempts == 0
