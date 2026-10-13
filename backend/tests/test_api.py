from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update

from app.config import Settings
from app.core.auth import token_hash
from app.db.base import create_database
from app.db.migrate import upgrade_database
from app.db.models import Debate, DebateMessage, ExecutionAttempt
from app.db.repo import DebateRepository
from app.engine.provider import FakeProvider
from app.engine.models import TURN_ORDER, ROUND_NUMBER, Speech, Topic
from app.engine.retry import RetryPolicy
from app.main import create_app

TOPIC_ID = "four-day-work-week"


async def no_sleep(_delay):
    pass


FAST = RetryPolicy(sleep=no_sleep, jitter=lambda: 0)


@asynccontextmanager
async def running_app(path, **kwargs):
    app = create_app(Settings(database_path=path), retry_policy=FAST, shutdown_grace=0.01, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client


@pytest.fixture
async def api(tmp_path):
    async with running_app(tmp_path / "api.db") as value:
        yield value


async def create(client, *, token=None, request_id=None, topic=TOPIC_ID):
    token = token or secrets.token_hex(32)
    response = await client.post("/api/debates", headers={"X-Debate-Token": token},
                                 json={"topic_id": topic, "client_request_id": request_id or str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json(), {"X-Debate-Token": token}


async def terminal(client, debate_id):
    async with asyncio.timeout(5):
        while True:
            response = await client.get(f"/api/debates/{debate_id}")
            assert response.status_code == 200, response.text
            data = response.json()
            if data["status"] in {"completed", "failed"}:
                return data
            await asyncio.sleep(0.005)


async def test_create_replay_conflict_and_no_implicit_execution(api):
    app, client = api
    request_id = str(uuid4())
    data, headers = await create(client, request_id=request_id)
    assert data["status"] == "created"
    assert data["controls"] == {"can_start": True, "can_resume": False, "remaining_attempts": 3}
    body = {"client_request_id": request_id, "topic_id": TOPIC_ID}
    replay = await client.post("/api/debates", headers=headers, json=body)
    assert replay.status_code == 200
    assert replay.json()["id"] == data["id"]
    # Replay is resolved without consulting the current catalogue or future quota checks.
    topic = app.state.topics.pop(TOPIC_ID)
    replay = await client.post("/api/debates", headers=headers, json=body)
    assert replay.status_code == 200
    app.state.topics[TOPIC_ID] = topic
    conflict = await client.post("/api/debates", headers=headers, json={**body, "topic_id": "no-longer-exists"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "request_conflict"
    forbidden = await client.post("/api/debates", headers={"X-Debate-Token": secrets.token_hex(32)}, json=body)
    assert forbidden.status_code == 403
    assert data["id"] not in forbidden.text
    assert (await client.get(f"/api/debates/{data['id']}" )).json()["status"] == "created"
    assert (await client.get("/api/debates")).status_code == 200
    assert not app.state.executions.tasks
    async with app.state.sessions() as session:
        saved = await session.get(Debate, data["id"])
        assert saved.accepted_attempts == saved.total_calls == 0
        assert saved.execution_token_hash == token_hash(headers["X-Debate-Token"])
        assert saved.execution_token_hash != headers["X-Debate-Token"]


async def test_concurrent_create_replays_produce_one_row(api):
    app, client = api
    body = {"topic_id": TOPIC_ID, "client_request_id": str(uuid4())}
    headers = {"X-Debate-Token": secrets.token_hex(32)}
    results = await asyncio.gather(*(client.post("/api/debates", json=body, headers=headers) for _ in range(4)))
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    assert len({r.json()["id"] for r in results}) == 1
    async with app.state.sessions() as session:
        assert len((await session.scalars(select(Debate))).all()) == 1


async def test_public_read_hides_controls_and_secrets(api):
    _, client = api
    data, headers = await create(client)
    for request_headers in ({}, {"X-Debate-Token": "bad"}):
        response = await client.get(f"/api/debates/{data['id']}", headers=request_headers)
        assert response.status_code == 200
        assert "controls" not in response.json()
        assert "execution_token" not in response.text
        assert headers["X-Debate-Token"] not in response.text
        assert token_hash(headers["X-Debate-Token"]) not in response.text
    history = await client.get("/api/debates")
    assert "controls" not in history.text
    assert "token" not in history.text


@pytest.mark.parametrize("token", [None, "bad", "a" * 63, "g" * 64])
async def test_creation_requires_valid_header_token(api, token):
    _, client = api
    response = await client.post("/api/debates", headers={"X-Debate-Token": token} if token else {},
                                 json={"topic_id": TOPIC_ID, "client_request_id": str(uuid4())})
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"


async def test_run_requires_token_and_completes(api):
    app, client = api
    data, headers = await create(client)
    path = f"/api/debates/{data['id']}/run"
    for invalid in ({}, {"X-Debate-Token": secrets.token_hex(32)}):
        response = await client.post(path, headers=invalid)
        assert response.status_code == 403
    accepted = await client.post(path, headers=headers)
    assert accepted.status_code == 202
    result = await terminal(client, data["id"])
    assert result["status"] == "completed"
    assert [m["turn_order"] for m in result["messages"]] == list(range(1, 7))
    assert result["judgment"]["pro_total"] == 80
    assert result["judgment"]["winner"] == "draw"
    assert result["progress"]["completed_turns"] == 6
    assert result["error"] is None
    denied = await client.post(path, headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "already_completed"
    async with app.state.sessions() as session:
        assert (await session.get(Debate, data["id"])).accepted_attempts == 1


async def test_duplicate_run_registers_exactly_one_worker(tmp_path):
    gate = asyncio.Event()
    provider = None

    class BlockedProvider(FakeProvider):
        async def complete(self, **kwargs):
            await gate.wait()
            return await super().complete(**kwargs)

    def factory():
        nonlocal provider
        assert provider is None
        provider = BlockedProvider()
        return provider

    async with running_app(tmp_path / "duplicate.db", provider_factory=factory) as (app, client):
        data, headers = await create(client)
        results = await asyncio.gather(*(client.post(f"/api/debates/{data['id']}/run", headers=headers) for _ in range(4)))
        assert sorted(r.status_code for r in results) == [202, 409, 409, 409]
        assert all(r.json()["error"]["code"] == "already_active" for r in results if r.status_code == 409)
        assert len(app.state.executions.tasks) == 1
        gate.set()
        assert (await terminal(client, data["id"]))["status"] == "completed"
        assert len(provider.calls) == 7


async def test_global_capacity_two_and_rejected_attempt_is_free(tmp_path):
    gate = asyncio.Event()

    class BlockedProvider(FakeProvider):
        async def complete(self, **kwargs):
            await gate.wait()
            return await super().complete(**kwargs)

    async with running_app(tmp_path / "capacity.db", provider_factory=BlockedProvider) as (app, client):
        battles = [await create(client) for _ in range(3)]
        results = await asyncio.gather(*(client.post(f"/api/debates/{data['id']}/run", headers=headers) for data, headers in battles))
        assert sorted(r.status_code for r in results) == [202, 202, 503]
        rejected = next(index for index, response in enumerate(results) if response.status_code == 503)
        assert results[rejected].headers["Retry-After"] == "5"
        async with app.state.sessions() as session:
            assert (await session.get(Debate, battles[rejected][0]["id"])).accepted_attempts == 0
        gate.set()
        for index, (data, _) in enumerate(battles):
            if index != rejected:
                await terminal(client, data["id"])


async def test_hidden_and_missing_are_404_on_all_public_paths(api):
    app, client = api
    request_id = str(uuid4())
    data, headers = await create(client, request_id=request_id)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).where(Debate.id == data["id"]).values(hidden=True))
    for debate_id in (data["id"], str(uuid4()), "not-an-id"):
        for supplied in ({}, headers):
            assert (await client.get(f"/api/debates/{debate_id}", headers=supplied)).status_code == 404
            assert (await client.post(f"/api/debates/{debate_id}/run", headers=supplied)).status_code == 404
    for supplied in ({}, headers):
        replay = await client.post("/api/debates", headers=supplied, json={"topic_id": TOPIC_ID, "client_request_id": request_id})
        assert replay.status_code == 404
    assert (await client.get("/api/debates")).json()["items"] == []


async def test_pagination_stable_with_ties_and_new_insert(api):
    app, client = api
    data = [(await create(client))[0] for _ in range(5)]
    same_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).values(created_at=same_time))
    expected = sorted((item["id"] for item in data), reverse=True)
    response = await client.get("/api/debates?limit=2")
    page = response.json()
    ids = [item["id"] for item in page["items"]]
    assert ids == expected[:2]
    new, _ = await create(client)
    while page["next_cursor"]:
        page = (await client.get("/api/debates", params={"limit": 2, "cursor": page["next_cursor"]})).json()
        ids.extend(item["id"] for item in page["items"])
    assert ids == expected
    assert new["id"] not in ids


@pytest.mark.parametrize("params", [{"cursor": "bad"}, {"cursor": "W10"}, {"limit": 0}, {"limit": 101}])
async def test_invalid_history_request(api, params):
    _, client = api
    response = await client.get("/api/debates", params=params)
    assert response.status_code == 422
    assert response.headers["Cache-Control"] == "no-store"


async def test_health_catalogue_validation_and_cors(api):
    _, client = api
    assert (await client.get("/api/health")).json() == {"status": "ready"}
    assert TOPIC_ID in {topic["id"] for topic in (await client.get("/api/topics")).json()["topics"]}
    for origin, allowed in (("http://localhost:5173", True), ("https://untrusted.example", False)):
        response = await client.options("/api/debates", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                                "Access-Control-Request-Headers": "X-Debate-Token"})
        assert ("Access-Control-Allow-Origin" in response.headers) is allowed
        assert response.headers["Cache-Control"] == "no-store"
    response = await client.post("/api/debates", json={"topic_id": TOPIC_ID, "client_request_id": "bad"})
    assert response.status_code == 422
    response = await client.post("/api/debates", headers={"X-Debate-Token": secrets.token_hex(32)},
                                 json={"topic_id": "bad", "client_request_id": str(uuid4())})
    assert response.status_code == 422


async def test_client_disconnect_does_not_cancel_accepted_worker(tmp_path):
    gate = asyncio.Event()
    entered = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            entered.set()
            await gate.wait()
            return await super().complete(**kwargs)

    app = create_app(Settings(database_path=tmp_path / "disconnect.db"), provider_factory=WaitingProvider, retry_policy=FAST)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            data, headers = await create(client)
            response = await client.post(f"/api/debates/{data['id']}/run", headers=headers)
            assert response.status_code == 202
            await asyncio.wait_for(entered.wait(), 5)
        assert len(app.state.executions.tasks) == 1
        gate.set()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as returning_client:
            result = await terminal(returning_client, data["id"])
            assert result["status"] == "completed"


async def test_cancelled_response_after_admission_does_not_cancel_worker(tmp_path):
    gate = asyncio.Event()
    admitted = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            await gate.wait()
            return await super().complete(**kwargs)

    async with running_app(tmp_path / "cancel-response.db", provider_factory=WaitingProvider) as (app, client):
        data, headers = await create(client)

        async def lost_connection(scope, receive, send):
            async def blocked_send(message):
                if message["type"] == "http.response.start":
                    admitted.set()
                    await asyncio.Event().wait()
                await send(message)
            await app(scope, receive, blocked_send)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lost_connection), base_url="http://test") as lost_client:
            request = asyncio.create_task(lost_client.post(f"/api/debates/{data['id']}/run", headers=headers))
            await asyncio.wait_for(admitted.wait(), 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        assert len(app.state.executions.tasks) == 1
        gate.set()
        assert (await terminal(client, data["id"]))["status"] == "completed"


@pytest.mark.parametrize("checkpoint", [3, 6])
async def test_startup_reconciles_running_and_judging_without_auto_resume(tmp_path, checkpoint):
    path = tmp_path / "restart.db"
    token = secrets.token_hex(32)
    engine, sessions = create_database(path)
    await upgrade_database(engine)
    try:
        repo = DebateRepository(sessions)
        debate_id = await repo.create(topic=Topic(TOPIC_ID, "Frozen original topic", "For", "Against"),
                                      client_request_id=str(uuid4()), execution_token_hash=token_hash(token))
        checkpoints, _, _ = await repo.begin_attempt(debate_id, model_id="fake")
        prefix = []
        for index in range(checkpoint):
            round_name, agent, side = TURN_ORDER[index]
            saved = Speech(index + 1, round_name, ROUND_NUMBER[round_name], agent, side, f"saved {index}", str(uuid4()))
            prefix.append(saved)
            await checkpoints.commit_speech(saved)
    finally:
        await engine.dispose()

    providers = []

    def factory():
        provider = FakeProvider()
        providers.append(provider)
        return provider

    async with running_app(path, provider_factory=factory) as (app, client):
        data = (await client.get(f"/api/debates/{debate_id}", headers={"X-Debate-Token": token})).json()
        assert data["status"] == "failed"
        assert data["error"]["code"] == "server_interrupted"
        assert data["controls"]["can_resume"]
        assert [m["id"] for m in data["messages"]] == [s.message_id for s in prefix]
        assert not providers
        async with app.state.sessions() as session:
            attempt = await session.get(ExecutionAttempt, checkpoints.run_id)
            assert attempt.status == "interrupted"
            assert attempt.finished_at is not None
        response = await client.post(f"/api/debates/{debate_id}/run", headers={"X-Debate-Token": token})
        assert response.status_code == 202
        result = await terminal(client, debate_id)
        assert result["status"] == "completed"
        assert len(providers[0].calls) == 7 - checkpoint
        assert [m["id"] for m in result["messages"][:checkpoint]] == [s.message_id for s in prefix]
        assert result["topic"]["title"] == "Frozen original topic"


async def test_supervisor_detects_orphan_without_resuming(tmp_path):
    async with running_app(tmp_path / "orphan.db", supervisor_interval=0.01) as (app, client):
        data, _ = await create(client)
        checkpoints, _, _ = await app.state.executions.repo.begin_attempt(data["id"], model_id="fake")
        result = await terminal(client, data["id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "server_interrupted"
        assert not app.state.executions.tasks
        async with app.state.sessions() as session:
            assert (await session.get(ExecutionAttempt, checkpoints.run_id)).status == "interrupted"


async def test_graceful_shutdown_interrupts_and_preserves_checkpoint(tmp_path):
    waiting = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            if len(self.calls) == 1:
                waiting.set()
                await asyncio.Event().wait()
            return await super().complete(**kwargs)

    path = tmp_path / "shutdown.db"
    async with running_app(path, provider_factory=WaitingProvider) as (app, client):
        data, headers = await create(client)
        assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 202
        await asyncio.wait_for(waiting.wait(), 5)
    assert not app.state.executions.admitting
    assert not app.state.executions.tasks
    async with running_app(path) as (_, client):
        result = (await client.get(f"/api/debates/{data['id']}", headers=headers)).json()
        assert result["status"] == "failed"
        assert len(result["messages"]) == 1
        assert result["error"]["code"] == "server_interrupted"
        assert result["controls"]["can_resume"]
        assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 202
        assert (await terminal(client, data["id"]))["status"] == "completed"


async def test_attempt_deadline_stops_worker_with_saved_transcript(tmp_path):
    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            if len(self.calls) == 1:
                await asyncio.Event().wait()
            return await super().complete(**kwargs)

    async with running_app(tmp_path / "deadline.db", provider_factory=WaitingProvider, attempt_timeout=1) as (_, client):
        data, headers = await create(client)
        assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 202
        result = await terminal(client, data["id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "attempt_timeout"
        assert len(result["messages"]) == 1


async def test_api_resume_reuses_saved_turns(tmp_path):
    providers = []

    def factory():
        provider = FakeProvider(speeches=["one", "two", "three", "", "", ""]) if not providers else FakeProvider()
        providers.append(provider)
        return provider

    async with running_app(tmp_path / "resume.db", provider_factory=factory) as (_, client):
        data, headers = await create(client)
        path = f"/api/debates/{data['id']}/run"
        assert (await client.post(path, headers=headers)).status_code == 202
        failed = await terminal(client, data["id"])
        assert failed["status"] == "failed"
        assert failed["error"]["failed_stage"] == "speech_turn_4"
        assert failed["error"]["failed_turn"] == 4
        assert (await client.get(f"/api/debates/{data['id']}", headers=headers)).json()["controls"]["can_resume"]
        assert (await client.post(path, headers=headers)).status_code == 202
        result = await terminal(client, data["id"])
        assert result["status"] == "completed"
        assert result["messages"][:3] == failed["messages"]
        assert len(providers[1].calls) == 4


async def test_unexpected_provider_failure_is_sanitized(tmp_path, caplog):
    class BrokenProvider(FakeProvider):
        async def complete(self, **kwargs):
            raise RuntimeError("SECRET raw provider exception")

    async with running_app(tmp_path / "unexpected.db", provider_factory=BrokenProvider) as (_, client):
        data, headers = await create(client)
        assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 202
        result = await terminal(client, data["id"])
        assert result["error"]["code"] == "internal_error"
        assert "SECRET" not in str(result)
    assert "SECRET" not in caplog.text


async def test_storage_errors_return_sanitized_503(api):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    app, client = api
    data, _ = await create(client)

    def fail_select(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("SELECT"):
            raise OperationalError(statement, {}, Exception("SECRET storage diagnostics"))

    event.listen(app.state.engine.sync_engine, "before_cursor_execute", fail_select)
    try:
        response = await client.get(f"/api/debates/{data['id']}")
        assert response.status_code == 503
        assert response.headers["Cache-Control"] == "no-store"
        assert "SECRET" not in response.text
        assert (await client.get("/api/health")).status_code == 503
    finally:
        event.remove(app.state.engine.sync_engine, "before_cursor_execute", fail_select)


async def test_unexpected_api_error_is_sanitized_without_propagating_to_server_logs(api, monkeypatch, caplog):
    import app.api.debates as debates_api

    _, client = api
    data, _ = await create(client)

    async def broken_snapshot(*_args):
        raise RuntimeError("SECRET raw exception")

    monkeypatch.setattr(debates_api, "snapshot", broken_snapshot)
    response = await client.get(f"/api/debates/{data['id']}")
    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "no-store"
    assert "SECRET" not in response.text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("values,code", [({"accepted_attempts": 3}, "attempt_limit"),
                                        ({"total_calls": 30}, "debate_call_limit"),
                                        ({"error_code": "checkpoint_invalid"}, "checkpoint_invalid")])
async def test_ineligible_resume_has_no_controls_or_attempt_consumption(api, values, code):
    app, client = api
    data, headers = await create(client)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).where(Debate.id == data["id"]).values(status="failed", **values))
    snapshot = (await client.get(f"/api/debates/{data['id']}", headers=headers)).json()
    assert not snapshot["controls"]["can_resume"]
    response = await client.post(f"/api/debates/{data['id']}/run", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    assert not app.state.executions.tasks
    async with app.state.sessions() as session:
        assert (await session.get(Debate, data["id"])).accepted_attempts == values.get("accepted_attempts", 0)


async def test_capacity_is_atomic_even_without_manager_lock(api):
    app, client = api
    ids = [(await create(client))[0]["id"] for _ in range(3)]
    repo = DebateRepository(app.state.sessions)
    results = await asyncio.gather(*(repo.begin_attempt(debate_id, model_id="fake", capacity=2) for debate_id in ids),
                                   return_exceptions=True)
    assert sum(isinstance(result, tuple) for result in results) == 2
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert errors[0].code == "capacity_unavailable"


async def test_shared_mode_fails_closed_without_required_secrets(tmp_path):
    app = create_app(Settings(database_path=tmp_path / "shared.db", deployment_mode="shared"))
    with pytest.raises(RuntimeError, match="Shared deployment is unavailable"):
        async with app.router.lifespan_context(app):
            pass
    assert not app.state.ready
