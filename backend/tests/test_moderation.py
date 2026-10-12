from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.cli import load_topics
from app.config import Settings
from app.core.auth import token_hash
from app.db.models import Debate, DebateMessage, ExecutionAttempt, Judgment, ProviderCallReservation
from app.db.repo import load_speeches
from app.engine.provider import FakeProvider, ProviderResponse, _DEFAULT_JUDGMENT, _DEFAULT_SPEECHES
from app.engine.retry import OrchestratorError
from app.engine.screening import contains_profanity
from app.engine.validation import ValidationFailure, validate_judgment, validate_speech
from app.main import create_app
from tests.test_api import FAST, create, terminal

ADMIN = "offline-admin-token-with-at-least-32-characters"
ADMIN_HEADERS = {"X-Admin-Token": ADMIN}
FIELDS = ("pro_strengths", "pro_weaknesses", "con_strengths", "con_weaknesses", "overall_explanation")


@asynccontextmanager
async def running(path, *, provider_factory=FakeProvider):
    app = create_app(Settings(database_path=path, admin_token=ADMIN), provider_factory=provider_factory,
                     retry_policy=FAST, shutdown_grace=0.01)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client


def test_default_wordlist_false_positive_baseline():
    cases = json.loads((Path(__file__).parent / "fixtures" / "screening_cases.json").read_text())
    topics = [topic.title + " " + topic.pro_framing + " " + topic.con_framing for topic in load_topics()]
    accepted = topics + _DEFAULT_SPEECHES + [_DEFAULT_JUDGMENT[field] for field in FIELDS] + cases["acceptable"]
    assert len(accepted) == 22
    assert sum(contains_profanity(text) for text in accepted) == 0
    assert sum(contains_profanity(text) for text in cases["known_false_positives"]) == 1


@pytest.mark.parametrize("text", ["This is bullshit.", "DAMN!", "f*ck"])
def test_speech_filter_rejects_without_exposing_flagged_words(text):
    with pytest.raises(ValidationFailure) as error:
        validate_speech(text)
    assert error.value.code == "content_flagged"
    assert text not in str(error.value)


@pytest.mark.parametrize("field", FIELDS)
def test_judge_checks_every_free_text_field(field):
    payload = {**_DEFAULT_JUDGMENT, "referenced_message_ids": ["m1"], field: "This is bullshit."}
    with pytest.raises(ValidationFailure) as error:
        validate_judgment(json.dumps(payload), known_message_ids={"m1"})
    assert error.value.code == "content_flagged"


async def test_content_retry_uses_existing_budget_and_corrective_prompt(tmp_path):
    provider = FakeProvider(speeches=["", "This is bullshit.", *_DEFAULT_SPEECHES])
    async with running(tmp_path / "retry.db", provider_factory=lambda: provider) as (app, client):
        data, headers = await create(client)
        assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 202
        result = await terminal(client, data["id"])
        assert result["status"] == "completed"
        assert len(provider.calls) == 9
        assert "profanity filter" in provider.calls[2][1]
        assert "bullshit" not in provider.calls[2][1]
        async with app.state.sessions() as session:
            rows = (await session.scalars(select(ProviderCallReservation).where(ProviderCallReservation.stage == "speech_turn_1"))).all()
            assert len(rows) == 3
            assert sorted(row.dispatch_state for row in rows) == ["committed", "failed", "failed"]


async def test_flag_exhaustion_preserves_prefix_and_resume_starts_at_missing_turn(tmp_path):
    providers = [FakeProvider(speeches=[*_DEFAULT_SPEECHES[:3], *(["This is bullshit."] * 3)]),
                 FakeProvider(speeches=_DEFAULT_SPEECHES[3:])]
    async with running(tmp_path / "resume.db", provider_factory=lambda: providers.pop(0)) as (app, client):
        data, headers = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        failed = await terminal(client, data["id"])
        assert failed["error"]["code"] == "content_flagged"
        assert failed["error"]["failed_turn"] == 4
        assert len(failed["messages"]) == 3
        authorized = (await client.get(f"/api/debates/{data['id']}", headers=headers)).json()
        assert authorized["controls"]["can_resume"] is True
        async with app.state.sessions() as session:
            assert (await session.get(Debate, data["id"])).total_calls == 6
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        completed = await terminal(client, data["id"])
        assert completed["status"] == "completed"
        assert completed["messages"][:3] == failed["messages"]
        async with app.state.sessions() as session:
            assert (await session.get(Debate, data["id"])).total_calls == 10


async def test_judge_flag_exhaustion_keeps_speeches_for_judging_only_resume(tmp_path):
    class FlaggedJudge(FakeProvider):
        async def complete(self, **kwargs):
            response = await super().complete(**kwargs)
            if kwargs.get("json_schema") is not None:
                payload = json.loads(response.text)
                payload["con_weaknesses"] = "This is bullshit."
                return ProviderResponse(json.dumps(payload))
            return response
    first = FlaggedJudge()
    resumed = FakeProvider(speeches=[])
    providers = [first, resumed]
    async with running(tmp_path / "judge.db", provider_factory=lambda: providers.pop(0)) as (app, client):
        data, headers = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        failed = await terminal(client, data["id"])
        assert failed["error"]["code"] == "content_flagged" and failed["error"]["failed_stage"] == "judgment"
        assert failed["judgment"] is None and len(failed["messages"]) == 6
        assert len(first.calls) == 9
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        completed = await terminal(client, data["id"])
        assert completed["status"] == "completed"
        assert completed["messages"] == failed["messages"]
        assert len(resumed.calls) == 1
        async with app.state.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(Judgment)) == 1


async def test_last_failure_determines_content_flag_code(tmp_path):
    provider = FakeProvider(speeches=["This is bullshit.", "", ""])
    async with running(tmp_path / "mixed.db", provider_factory=lambda: provider) as (_, client):
        data, headers = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        assert (await terminal(client, data["id"]))["error"]["code"] == "provider_failed"
        assert len(provider.calls) == 3


async def test_admin_inspection_and_hide_require_authentication(tmp_path):
    async with running(tmp_path / "auth.db") as (app, client):
        data, owner = await create(client)
        for headers in ({}, {"X-Admin-Token": "wrong"}, owner):
            assert (await client.get(f"/api/admin/debates/{data['id']}", headers=headers)).status_code == 401
            assert (await client.post(f"/api/admin/debates/{data['id']}/hide", headers=headers)).status_code == 401
        async with app.state.sessions() as session:
            assert not (await session.get(Debate, data["id"])).hidden
        for method, path in ((client.get, "/api/admin/debates/missing"), (client.post, "/api/admin/debates/missing/hide")):
            assert (await method(path, headers=ADMIN_HEADERS)).status_code == 404


@pytest.mark.parametrize("state", ["created", "completed", "failed"])
async def test_hide_is_idempotent_preserves_content_and_blocks_every_public_path(tmp_path, state):
    provider = FakeProvider(speeches=["", "", ""]) if state == "failed" else FakeProvider()
    async with running(tmp_path / "hide.db", provider_factory=lambda: provider) as (app, client):
        request_id = str(uuid4())
        data, owner = await create(client, request_id=request_id)
        if state != "created":
            await client.post(f"/api/debates/{data['id']}/run", headers=owner)
            data = await terminal(client, data["id"])
        response = await client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS, json={"reason": "  Operator review  "})
        assert response.status_code == 200
        assert response.json() == {"id": data["id"], "hidden": True, "status": state}
        detail = (await client.get(f"/api/admin/debates/{data['id']}", headers=ADMIN_HEADERS)).json()
        assert detail["messages"] == data["messages"] and detail["judgment"] == data["judgment"]
        assert detail["moderation"]["reason"] == "Operator review"
        assert detail["revision"] == data["revision"] + 1
        await client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS, json={"reason": "Replacement"})
        repeated = (await client.get(f"/api/admin/debates/{data['id']}", headers=ADMIN_HEADERS)).json()
        assert repeated == detail
        for headers in ({}, owner, {"X-Debate-Token": "wrong"}):
            assert (await client.get(f"/api/debates/{data['id']}", headers=headers)).status_code == 404
            assert (await client.post(f"/api/debates/{data['id']}/run", headers=headers)).status_code == 404
            body = {"topic_id": "four-day-work-week", "client_request_id": request_id}
            replay = await client.post("/api/debates", headers=headers, json=body)
            assert replay.status_code == 404
        assert data["id"] not in (await client.get("/api/debates")).text
        for value in (owner["X-Debate-Token"], token_hash(owner["X-Debate-Token"]), "execution_token_hash", "controls"):
            assert value not in json.dumps(detail)
        async with app.state.sessions() as session:
            saved = await session.get(Debate, data["id"])
            assert saved.hidden and saved.hidden_at is not None
            if state == "failed":
                assert saved.error_code == "provider_failed"


class BlockingProvider(FakeProvider):
    def __init__(self, *, judging=False, ignore_cancel=False):
        super().__init__()
        self.judging = judging
        self.ignore_cancel = ignore_cancel
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()
        self.started = 0

    async def complete(self, **kwargs):
        self.started += 1
        block = kwargs.get("json_schema") is not None if self.judging else self.started == 2
        if block:
            self.blocked.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.ignore_cancel:
                    raise
                await self.release.wait()
        return await super().complete(**kwargs)


@pytest.mark.parametrize("judging,ignore_cancel", [(False, False), (True, False), (False, True)])
async def test_hide_active_work_cancels_provider_and_rejects_late_commits(tmp_path, judging, ignore_cancel):
    provider = BlockingProvider(judging=judging, ignore_cancel=ignore_cancel)
    async with running(tmp_path / "live.db", provider_factory=lambda: provider) as (app, client):
        data, owner = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=owner)
        await asyncio.wait_for(provider.blocked.wait(), 3)
        async with app.state.sessions() as session:
            saved = await session.get(Debate, data["id"])
            run_id = saved.active_run_id
            prefix = await load_speeches(session, data["id"])
        task = app.state.executions.tasks[run_id]
        response = await client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS)
        assert response.status_code == 200
        await asyncio.wait_for(provider.cancelled.wait(), 1)
        if ignore_cancel:
            assert not task.done()
            provider.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert provider.started == (7 if judging else 2)
        async with app.state.sessions() as session:
            saved = await session.get(Debate, data["id"])
            assert saved.hidden and saved.status == "failed" and saved.error_code == "operator_hidden"
            assert saved.active_run_id is None and saved.active_speaker is None
            assert (await session.get(ExecutionAttempt, run_id)).status == "failed"
            assert await load_speeches(session, data["id"]) == prefix
            assert await session.scalar(select(Judgment)) is None
            rows = (await session.scalars(select(ProviderCallReservation))).all()
            assert len(rows) == provider.started
            assert sum(row.dispatch_state == "abandoned" for row in rows) == 1
        assert (await client.get(f"/api/debates/{data['id']}")).status_code == 404


async def test_hide_between_reservation_and_dispatch_prevents_provider_call(tmp_path, monkeypatch):
    import app.core.costs as costs
    original = costs.reserve
    async with running(tmp_path / "dispatch-race.db") as (app, client):
        data, _ = await create(client)
        checkpoints, _, _ = await app.state.executions.repo.begin_attempt(data["id"], model_id="fake")
        async def hide_after_reserve(*args, **kwargs):
            id = await original(*args, **kwargs)
            await app.state.executions.hide(data["id"], reason="Race test")
            return id
        monkeypatch.setattr(costs, "reserve", hide_after_reserve)
        provider = FakeProvider()
        with pytest.raises(OrchestratorError) as error:
            await checkpoints.dispatch(provider, "speech_turn_1", system="System", user="User", max_output_tokens=512, json_schema=None)
        assert error.value.code == "stale_run" and provider.calls == []
        async with app.state.sessions() as session:
            assert (await session.scalar(select(ProviderCallReservation))).dispatch_state == "abandoned"


async def test_final_dispatch_authorization_is_serialized_with_hide(tmp_path, monkeypatch):
    async with running(tmp_path / "final-race.db") as (app, client):
        data, _ = await create(client)
        checkpoints, _, _ = await app.state.executions.repo.begin_attempt(data["id"], model_id="fake")
        reached = asyncio.Event()
        release = asyncio.Event()
        original = checkpoints._update
        async def paused_update(session, **values):
            await original(session, **values)
            if not values:
                reached.set()
                await release.wait()
        monkeypatch.setattr(checkpoints, "_update", paused_update)
        class WaitingProvider(FakeProvider):
            async def complete(self, **kwargs):
                await asyncio.Future()
        worker = asyncio.create_task(checkpoints.dispatch(WaitingProvider(), "speech_turn_1", system="System", user="User", max_output_tokens=512, json_schema=None))
        app.state.executions.tasks[checkpoints.run_id] = worker
        await asyncio.wait_for(reached.wait(), 2)
        hide = asyncio.create_task(app.state.executions.hide(data["id"], reason="Serialized stop"))
        await asyncio.sleep(0)
        assert app.state.executions.lock.locked() and not hide.done()
        release.set()
        await asyncio.wait_for(hide, 2)
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 2)
        async with app.state.sessions() as session:
            assert (await session.get(Debate, data["id"])).hidden
            assert (await session.scalar(select(ProviderCallReservation))).dispatch_state == "abandoned"


async def test_simultaneous_start_and_hide_leaves_no_active_owner(tmp_path):
    class WaitingProvider(FakeProvider):
        async def complete(self, **kwargs):
            await asyncio.Future()
    async with running(tmp_path / "start-race.db", provider_factory=WaitingProvider) as (app, client):
        data, owner = await create(client)
        run, hidden = await asyncio.gather(client.post(f"/api/debates/{data['id']}/run", headers=owner),
                                         client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS))
        assert run.status_code in {202, 404}
        assert hidden.status_code == 200
        tasks = list(app.state.executions.tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        async with app.state.sessions() as session:
            saved = await session.get(Debate, data["id"])
            assert saved.hidden and saved.active_run_id is None
            assert await session.scalar(select(func.count()).select_from(ExecutionAttempt).where(ExecutionAttempt.status == "active")) == 0


async def test_failed_hide_rolls_back_visibility_and_keeps_worker_alive(tmp_path):
    provider = BlockingProvider()
    async with running(tmp_path / "rollback.db", provider_factory=lambda: provider) as (app, client):
        data, owner = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=owner)
        await asyncio.wait_for(provider.blocked.wait(), 3)
        armed = True
        def reject_attempt_update(connection, cursor, statement, parameters, context, executemany):
            nonlocal armed
            if armed and statement.startswith("UPDATE execution_attempts"):
                armed = False
                raise OperationalError(statement, {}, Exception("offline rollback test"))
        event.listen(app.state.engine.sync_engine, "before_cursor_execute", reject_attempt_update)
        try:
            response = await client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS)
            assert response.status_code == 503
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", reject_attempt_update)
        assert not provider.cancelled.is_set()
        async with app.state.sessions() as session:
            saved = await session.get(Debate, data["id"])
            assert not saved.hidden and saved.hidden_at is None and saved.hidden_reason is None
            assert saved.active_run_id is not None
        provider.release.set()
        assert (await terminal(client, data["id"]))["status"] == "completed"


@pytest.mark.parametrize("body", [{"reason": " "}, {"reason": "x" * 501}, {"reason": 123}, {"unexpected": True}])
async def test_hide_reason_validation_does_not_modify_debate(tmp_path, body):
    async with running(tmp_path / "reason.db") as (app, client):
        data, _ = await create(client)
        response = await client.post(f"/api/admin/debates/{data['id']}/hide", headers=ADMIN_HEADERS, json=body)
        assert response.status_code == 422
        async with app.state.sessions() as session:
            assert not (await session.get(Debate, data["id"])).hidden
