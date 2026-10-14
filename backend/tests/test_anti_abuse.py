from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import time
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from starlette.requests import Request

from app.config import Settings
from app.core.identity import canonical_ip, identify
from app.core.quotas import clear_old_identities, weekly_identity_cleanup
from app.db.models import CreationQuotaRecord, Debate, ExecutionAttempt
from app.db.repo import DebateRepository
from app.engine.retry import OrchestratorError
from app.main import create_app

SECRET = "cQ8vZp1s7aN4wK2d9mF6xB0r5hL3tYue"
TOPIC = "four-day-work-week"


def shared_settings(path, **kwargs):
    return Settings(database_path=path, deployment_mode="shared", admin_token=SECRET,
                    rate_limit_hmac_secret=SECRET[::-1], groq_api_key="gsk_" + SECRET,
                    groq_model="openai/gpt-oss-20b", trusted_proxy_networks=("127.0.0.0/8",),
                    max_reserved_cost_24h_usd=Decimal("1.00"), **kwargs)


@asynccontextmanager
async def shared_app(path, *, settings=None):
    app = create_app(settings or shared_settings(path), shutdown_grace=0.01)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Fly-Client-IP": "192.0.2.1"}) as client:
            yield app, client


def creation(*, ip="192.0.2.1", request_id=None, token=None):
    return {"json": {"topic_id": TOPIC, "client_request_id": request_id or str(uuid4())},
            "headers": {"Fly-Client-IP": ip, "X-Debate-Token": token or secrets.token_hex(32)}}


def identity_request(peer, headers):
    return Request({"type": "http", "client": (peer, 1234), "headers": [(name.lower().encode(), value.encode()) for name, value in headers]})


@pytest.fixture
def rate_clock(monkeypatch):
    now = {"value": time.time()}
    clock = SimpleNamespace(time=lambda: now["value"])
    monkeypatch.setattr("limits.storage.memory.time", clock)
    monkeypatch.setattr("app.core.ratelimit.time", clock)
    return now


@pytest.mark.parametrize("field", ["admin_token", "rate_limit_hmac_secret", "groq_api_key", "groq_model"])
@pytest.mark.parametrize("value", ["", "changeme", "your-key-placeholder-1234567890123456789"])
async def test_shared_startup_rejects_missing_or_placeholder_config(tmp_path, field, value):
    app = create_app(replace(shared_settings(tmp_path / "invalid.db"), **{field: value}))
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pytest.fail("Unconfigured shared app must not start")
    assert not app.state.ready
    assert not (tmp_path / "invalid.db").exists()


def test_settings_hide_secrets_and_validate_limits(tmp_path):
    settings = shared_settings(tmp_path / "test.db")
    assert SECRET not in repr(settings)
    assert SECRET[::-1] not in repr(settings)
    for field, value in [("create_quota_per_ip_24h", 0), ("hmac_key_version", 0),
                         ("global_concurrency", 3), ("trusted_proxy_networks", ("bad",))]:
        with pytest.raises(ValueError):
            replace(settings, **{field: value})


def test_identity_canonicalization_trust_and_header_spoofing(tmp_path):
    settings = shared_settings(tmp_path / "identity.db")
    expected = hmac.new(settings.rate_limit_hmac_secret.encode(), b"192.0.2.1", hashlib.sha256).hexdigest()
    trusted = identify(identity_request("127.0.0.1", [("Fly-Client-IP", "::ffff:192.0.2.1")]), settings)
    assert trusted.bucket_hash == expected
    assert trusted.key_version == 1
    direct = identify(identity_request("192.0.2.1", [("Fly-Client-IP", "198.51.100.2"),
                             ("X-Forwarded-For", "203.0.113.3"), ("Forwarded", "for=203.0.113.3")]), settings)
    assert trusted == direct
    assert canonical_ip("2001:0DB8:0000:0000::1") == "2001:db8::1"
    assert identify(identity_request("192.0.2.2", []), settings) != trusted
    assert identify(identity_request("192.0.2.1", []), replace(settings, rate_limit_hmac_secret=SECRET)) != trusted


@pytest.mark.parametrize("headers", [[], [("Fly-Client-IP", "bad")], [("Fly-Client-IP", "192.0.2.1, 192.0.2.2")],
                                    [("Fly-Client-IP", "192.0.2.1"), ("Fly-Client-IP", "192.0.2.2")],
                                    [("Fly-Client-IP", "fe80::1%eth0")]])
def test_invalid_trusted_identity_fails_without_disclosing_address(tmp_path, headers):
    from app.api.errors import APIError
    with pytest.raises(APIError) as error:
        identify(identity_request("127.0.0.1", headers), shared_settings(tmp_path / "test.db"))
    assert error.value.status == 400
    assert "192.0.2" not in error.value.message


async def test_ten_polling_viewers_fit_combined_read_bucket(tmp_path, rate_clock):
    async with shared_app(tmp_path / "polling.db") as (_, client):
        async def viewer():
            for _ in range(40):
                assert (await client.get("/api/topics")).status_code == 200
        await asyncio.gather(*(viewer() for _ in range(10)))
        for index in range(200):
            path = "/api/health" if index % 2 else "/api/debates"
            assert (await client.get(path)).status_code == 200
        response = await client.get("/api/topics", headers={"Origin": "http://localhost:5173"})
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
        assert (await client.get("/api/topics", headers={"Fly-Client-IP": "192.0.2.2"})).status_code == 200
        rate_clock["value"] += 60
        assert (await client.get("/api/topics")).status_code == 200


async def test_combined_post_flood_bucket_and_admin_isolation(tmp_path, rate_clock):
    async with shared_app(tmp_path / "flood.db") as (_, client):
        for index in range(20):
            response = await client.post("/api/debates" if index % 2 else "/api/debates/missing/run")
            assert response.status_code in {404, 422}
        response = await client.post("/api/debates", **creation())
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "request_rate_limit"
        assert response.headers["Cache-Control"] == "no-store"
        assert (await client.get("/api/topics")).status_code == 200
        for _ in range(10):
            assert (await client.get("/api/admin/missing")).status_code == 404
        assert (await client.get("/api/admin/missing")).status_code == 429
        assert (await client.get("/api/topics")).status_code == 200
        assert (await client.options("/api/debates", headers={"Origin": "http://localhost:5173",
                       "Access-Control-Request-Method": "POST"})).status_code == 200
        rate_clock["value"] += 60
        assert (await client.post("/api/debates", **creation())).status_code == 201


async def test_default_creation_limit_and_replays_before_quota(tmp_path):
    async with shared_app(tmp_path / "quota.db") as (app, client):
        first = creation()
        saved = await client.post("/api/debates", **first)
        assert saved.status_code == 201
        for _ in range(9):
            assert (await client.post("/api/debates", **creation())).status_code == 201
        denied = await client.post("/api/debates", **creation())
        assert denied.status_code == 429
        assert denied.json()["error"]["code"] == "creation_quota_per_ip"
        assert int(denied.headers["Retry-After"]) > 86000
        app.state.topics.clear()
        replay = await client.post("/api/debates", **first)
        assert replay.status_code == 200
        assert replay.json()["id"] == saved.json()["id"]
        wrong = {**first, "headers": {**first["headers"], "X-Debate-Token": secrets.token_hex(32)}}
        assert (await client.post("/api/debates", **wrong)).status_code == 403
        changed = {**first, "json": {**first["json"], "topic_id": "changed"}}
        assert (await client.post("/api/debates", **changed)).status_code == 409
        async with app.state.sessions() as session:
            records = (await session.scalars(select(CreationQuotaRecord))).all()
            assert len(records) == 10
            assert records[0].key_version == 1
            assert len(records[0].ip_bucket_hash) == 64
            assert await session.scalar(select(func.count()).select_from(Debate)) == 10
            assert await session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
        async with app.state.sessions.begin() as session:
            await session.execute(update(Debate).where(Debate.id == saved.json()["id"]).values(hidden=True))
        assert (await client.post("/api/debates", **first)).status_code == 404


async def test_untrusted_forwarded_headers_cannot_evade_creation_quota(tmp_path):
    settings = shared_settings(tmp_path / "spoof.db", create_quota_per_ip_24h=1)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = creation(ip="192.0.2.1")
            assert (await client.post("/api/debates", **first)).status_code == 201
            second = creation(ip="192.0.2.2")
            second["headers"].update({"X-Forwarded-For": "203.0.113.3", "Forwarded": "for=203.0.113.4"})
            response = await client.post("/api/debates", **second)
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "creation_quota_per_ip"
            assert "198.51.100" not in response.text


@pytest.mark.parametrize("global_limit", [False, True])
async def test_competing_last_creation_slot_is_atomic(tmp_path, global_limit):
    settings = shared_settings(tmp_path / "race.db", create_quota_per_ip_24h=10 if global_limit else 1,
                               global_create_quota_24h=1 if global_limit else 100)
    async with shared_app(settings.database_path, settings=settings) as (app, client):
        inputs = [creation(ip=f"192.0.2.{index + 1}" if global_limit else "192.0.2.1") for index in range(6)]
        results = await asyncio.gather(*(client.post("/api/debates", **data) for data in inputs))
        assert sorted(response.status_code for response in results) == [201, 429, 429, 429, 429, 429]
        winner = next(index for index, response in enumerate(results) if response.status_code == 201)
        assert (await client.post("/api/debates", **inputs[winner])).status_code == 200
        async with app.state.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CreationQuotaRecord)) == 1
            assert await session.scalar(select(func.count()).select_from(Debate)) == 1


@pytest.mark.parametrize("global_limit", [False, True])
async def test_rolling_quota_survives_restart_midnight_and_expires(tmp_path, monkeypatch, global_limit):
    import app.api.debates as endpoints
    path = tmp_path / "rolling.db"
    settings = shared_settings(path, create_quota_per_ip_24h=10 if global_limit else 1,
                               global_create_quota_24h=1 if global_limit else 100)
    now = datetime(2026, 10, 12, 23, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(endpoints, "utcnow", lambda: now)
    first = creation()
    async with shared_app(path, settings=settings) as (_, client):
        assert (await client.post("/api/debates", **first)).status_code == 201
    now += timedelta(minutes=2)
    async with shared_app(path, settings=settings) as (_, client):
        denied = await client.post("/api/debates", **creation())
        assert denied.status_code == 429
        assert int(denied.headers["Retry-After"]) == 86280
        assert (await client.post("/api/debates", **first)).status_code == 200
        now += timedelta(hours=23, minutes=58)
        assert (await client.post("/api/debates", **creation())).status_code == 201


async def test_shared_execution_is_blocked_until_cost_guards(tmp_path):
    async with shared_app(tmp_path / "guard.db") as (app, client):
        data = creation()
        response = await client.post("/api/debates", **data)
        debate_id = response.json()["id"]
        app.state.executions.model_id = "openai/gpt-oss-20b"
        run = await client.post(f"/api/debates/{debate_id}/run", headers=data["headers"])
        assert run.status_code == 503
        assert run.json()["error"]["code"] == "usage_configuration"
        assert run.headers["Retry-After"] == "5"
        async with app.state.sessions() as session:
            assert (await session.get(Debate, debate_id)).accepted_attempts == 0
        assert not app.state.executions.tasks


@pytest.mark.parametrize("code", ["concurrency_per_ip", "global_concurrency"])
async def test_execution_quota_errors_include_retry_after(tmp_path, monkeypatch, code):
    app = create_app(Settings(database_path=tmp_path / "errors.db"))
    async with app.router.lifespan_context(app):
        async def reject(_id, **_kwargs):
            raise OrchestratorError("admission", "Unavailable", code=code)
        monkeypatch.setattr(app.state.executions, "admit", reject)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            data = creation()
            saved = await client.post("/api/debates", **data)
            response = await client.post(f"/api/debates/{saved.json()['id']}/run", headers=data["headers"])
            assert response.status_code == 429
            assert response.json()["error"]["code"] == code
            assert response.headers["Retry-After"] == "5"


async def test_atomic_ip_and_global_execution_capacity(tmp_path):
    async with shared_app(tmp_path / "capacity.db") as (app, client):
        ids = [(await client.post("/api/debates", **creation())).json()["id"] for _ in range(4)]
        repo = DebateRepository(app.state.sessions)
        results = await asyncio.gather(*(repo.begin_attempt(id, model_id="fake", capacity=2,
                            ip_bucket_hash="same-hash", concurrency_per_ip=1) for id in ids[:2]), return_exceptions=True)
        assert sum(isinstance(value, OrchestratorError) for value in results) == 1
        rejected = next(value for value in results if isinstance(value, OrchestratorError))
        assert rejected.code == "concurrency_per_ip"
        winner = next(value for value in results if not isinstance(value, Exception))
        other = await repo.begin_attempt(ids[2], model_id="fake", capacity=2, ip_bucket_hash="other-hash", concurrency_per_ip=1)
        with pytest.raises(OrchestratorError) as error:
            await repo.begin_attempt(ids[3], model_id="fake", capacity=2, ip_bucket_hash="third-hash", concurrency_per_ip=1)
        assert error.value.code == "global_concurrency"
        async with app.state.sessions() as session:
            assert (await session.get(Debate, ids[3])).accepted_attempts == 0
            assert await session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 2
        await winner[0].commit_failure(OrchestratorError("speech", "Stopped"))
        # Freed capacity can be used from another network, including for a manual resume.
        resumed = await repo.begin_attempt(winner[0].debate_id, model_id="fake", capacity=2,
                                           ip_bucket_hash="changed-network", concurrency_per_ip=1)
        async with app.state.sessions() as session:
            assert (await session.get(ExecutionAttempt, resumed[0].run_id)).ip_bucket_hash == "changed-network"
        await other[0].commit_failure(OrchestratorError("speech", "Stopped"))


async def test_cleanup_keeps_rows_and_recent_associations(tmp_path):
    async with shared_app(tmp_path / "cleanup.db") as (app, client):
        ids = [(await client.post("/api/debates", **creation())).json()["id"] for _ in range(3)]
        now = datetime.now(timezone.utc)
        repo = DebateRepository(app.state.sessions)
        for index, id in enumerate(ids):
            checkpoints, _, _ = await repo.begin_attempt(id, model_id="fake", ip_bucket_hash=f"bucket-{index}")
            await checkpoints.commit_failure(OrchestratorError("speech", "Stopped"))
        async with app.state.sessions.begin() as session:
            for model in (CreationQuotaRecord, ExecutionAttempt):
                await session.execute(update(model).where(model.debate_id == ids[0]).values(created_at=now - timedelta(days=8)))
                await session.execute(update(model).where(model.debate_id == ids[1]).values(created_at=now - timedelta(days=7)))
        await clear_old_identities(app.state.sessions, now=now)
        await clear_old_identities(app.state.sessions, now=now)
        async with app.state.sessions() as session:
            for model in (CreationQuotaRecord, ExecutionAttempt):
                rows = (await session.scalars(select(model))).all()
                assert len(rows) == 3
                values = {row.debate_id: row.ip_bucket_hash for row in rows}
                assert values[ids[0]] is None
                assert values[ids[1]] is not None
                assert values[ids[2]] is not None
            assert await session.scalar(select(func.count()).select_from(Debate)) == 3


async def test_weekly_cleanup_wait_and_cancellation(monkeypatch):
    import app.core.quotas as quotas
    called = asyncio.Event()
    intervals = []
    async def sleep(delay):
        intervals.append(delay)
        if len(intervals) > 1:
            await asyncio.Future()
    async def cleanup(_sessions):
        called.set()
    monkeypatch.setattr(quotas.asyncio, "sleep", sleep)
    monkeypatch.setattr(quotas, "clear_old_identities", cleanup)
    task = asyncio.create_task(weekly_identity_cleanup(None))
    await asyncio.wait_for(called.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert intervals == [604800, 604800]


async def test_local_mode_does_not_enforce_shared_quotas_or_buckets(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "local.db", create_quota_per_ip_24h=1))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(21):
                assert (await client.post("/api/debates", **creation())).status_code == 201
        async with app.state.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(CreationQuotaRecord)) == 0


async def test_startup_cleanup_catches_old_associations_after_downtime(tmp_path):
    path = tmp_path / "restart-cleanup.db"
    async with shared_app(path) as (app, client):
        await client.post("/api/debates", **creation())
        async with app.state.sessions.begin() as session:
            await session.execute(update(CreationQuotaRecord).values(created_at=datetime.now(timezone.utc) - timedelta(days=8)))
    async with shared_app(path) as (app, _):
        async with app.state.sessions() as session:
            record = await session.scalar(select(CreationQuotaRecord))
            assert record is not None and record.ip_bucket_hash is None
            assert await session.scalar(select(func.count()).select_from(Debate)) == 1


def test_runner_keeps_loopback_and_disables_forwarding_rewrites(monkeypatch):
    import app.server as server
    calls = []
    monkeypatch.setattr("sys.argv", ["server", "--provider", "fake"])
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    server.main()
    assert calls == [{"host": "127.0.0.1", "port": 8000, "workers": 1, "proxy_headers": False, "access_log": False}]
