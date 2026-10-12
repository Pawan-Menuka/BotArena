from __future__ import annotations

import asyncio
from dataclasses import asdict

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Debate, DebateMessage, ExecutionAttempt, Judgment, ProviderCallReservation, new_id, utcnow
from app.config import Settings
from app.engine.models import Agent, ROUND_NUMBER, TURN_ORDER, Side, Speech, Topic
from app.engine.orchestrator import run_debate, validate_checkpoint
from app.engine.retry import OrchestratorError

PROMPT_VERSION = "1"
RUBRIC_VERSION = "1"


def to_speech(row: DebateMessage) -> Speech:
    round_name = next(name for name, number in ROUND_NUMBER.items() if number == row.round_number)
    return Speech(row.turn_order, round_name, row.round_number, Agent(row.agent), Side(row.side), row.content, row.id)


async def load_speeches(session, debate_id: str) -> list[Speech]:
    rows = (await session.scalars(select(DebateMessage).where(DebateMessage.debate_id == debate_id)
                                 .order_by(DebateMessage.turn_order))).all()
    try:
        speeches = [to_speech(row) for row in rows]
        validate_checkpoint(speeches)
    except (ValueError, StopIteration) as exc:
        raise OrchestratorError("checkpoint", "Saved checkpoint is inconsistent", code="checkpoint_invalid") from exc
    return speeches


class DebateRepository:
    def __init__(self, sessions, settings: Settings | None = None, *, dispatch_lock=None):
        self.sessions = sessions
        self.settings = settings or Settings()
        self.dispatch_lock = dispatch_lock

    async def create(self, *, topic: Topic, client_request_id: str, execution_token_hash: str) -> str:
        debate = Debate(topic_id=topic.id, topic_snapshot=asdict(topic), client_request_id=client_request_id,
                        execution_token_hash=execution_token_hash)
        async with self.sessions.begin() as session:
            session.add(debate)
        return debate.id

    async def begin_attempt(self, debate_id: str, *, model_id: str, capacity: int | None = None,
                            ip_bucket_hash: str | None = None, concurrency_per_ip: int | None = None):
        run_id = new_id()
        async with self.sessions.begin() as session:
            conditions = []
            per_ip = select(func.count()).select_from(ExecutionAttempt).join(
                Debate, Debate.active_run_id == ExecutionAttempt.run_id
            ).where(ExecutionAttempt.ip_bucket_hash == ip_bucket_hash, ExecutionAttempt.status == "active",
                    Debate.status.in_(["running", "judging"])).scalar_subquery()
            if concurrency_per_ip is not None:
                if ip_bucket_hash is None:
                    raise ValueError("Per-IP admission requires a bucket hash")
                conditions.append(per_ip < concurrency_per_ip)
            if capacity is not None:
                active = select(func.count()).select_from(Debate).where(
                    Debate.status.in_(["running", "judging"])
                ).scalar_subquery()
                conditions.append(active < capacity)
            claimed = await session.execute(update(Debate).where(
                Debate.id == debate_id, Debate.hidden.is_(False), Debate.status.in_(["created", "failed"]),
                Debate.accepted_attempts < 3, Debate.total_calls < 30,
                (Debate.error_code.is_(None) | (Debate.error_code != "checkpoint_invalid")),
                *conditions,
            ).values(status="running", active_run_id=run_id, accepted_attempts=Debate.accepted_attempts + 1,
                     revision=Debate.revision + 1, updated_at=utcnow(), error_code=None, error_message=None,
                     failed_stage=None, failed_turn=None).returning(Debate))
            debate = claimed.scalar_one_or_none()
            if debate is None:
                existing = await session.get(Debate, debate_id)
                if existing is None or existing.hidden:
                    code = "not_found"
                elif existing.status in {"running", "judging"}:
                    code = "already_active"
                elif existing.status == "completed":
                    code = "already_completed"
                elif existing.accepted_attempts >= 3:
                    code = "attempt_limit"
                elif existing.total_calls >= 30:
                    code = "debate_call_limit"
                elif existing.error_code == "checkpoint_invalid":
                    code = "checkpoint_invalid"
                elif concurrency_per_ip is not None and await session.scalar(select(per_ip)) >= concurrency_per_ip:
                    code = "concurrency_per_ip"
                else:
                    code = "global_concurrency" if ip_bucket_hash is not None else "capacity_unavailable"
                raise OrchestratorError("admission", "Debate cannot accept an execution", code=code)
            try:
                speeches = await load_speeches(session, debate_id)
            except OrchestratorError:
                # Persist a non-resumable diagnosis without consuming an accepted attempt.
                debate.status = "failed"
                debate.active_run_id = None
                debate.accepted_attempts -= 1
                debate.error_code = "checkpoint_invalid"
                debate.error_message = "Saved checkpoint is inconsistent"
                debate.failed_stage = "checkpoint"
                speeches = None
            if speeches is not None:
                from app.core.costs import check_allowance
                from app.engine.budget import INPUT_TOKEN_CAP, REASONING_TOKEN_HEADROOM, price_for
                try:
                    self.settings.validate_execution_budget(model_id)
                    price = price_for(model_id)
                except ValueError:
                    raise OrchestratorError("admission", "Provider usage configuration needs attention", code="usage_configuration") from None
                cap = (2048 if len(speeches) == 6 else 512) + REASONING_TOKEN_HEADROOM
                await check_allowance(session, self.settings, model_id,
                                      price.cents(min(INPUT_TOKEN_CAP, price.context_window - cap), cap), "admission", utcnow())
                debate.status = "judging" if len(speeches) == 6 else "running"
                debate.activity = "evaluating" if len(speeches) == 6 else "generating"
                debate.active_turn = None if len(speeches) == 6 else len(speeches) + 1
                debate.active_speaker = TURN_ORDER[len(speeches)][1].value if len(speeches) < 6 else None
                debate.current_round = ROUND_NUMBER[TURN_ORDER[len(speeches)][0]] if len(speeches) < 6 else 3
                session.add(ExecutionAttempt(run_id=run_id, debate_id=debate_id,
                            attempt_number=debate.accepted_attempts, starting_checkpoint=len(speeches),
                            model_id=model_id, prompt_version=PROMPT_VERSION, ip_bucket_hash=ip_bucket_hash))
                topic = Topic(**debate.topic_snapshot)
        if speeches is None:
            raise OrchestratorError("checkpoint", "Saved checkpoint is inconsistent", code="checkpoint_invalid")
        return Checkpoints(self.sessions, debate_id, run_id, model_id, self.settings, dispatch_lock=self.dispatch_lock), topic, speeches

    async def execute(self, debate_id: str, *, provider, model_id: str, retry_policy=None):
        checkpoints, topic, speeches = await self.begin_attempt(debate_id, model_id=model_id)
        try:
            return await run_debate(topic=topic, provider=provider, prior=speeches,
                                    persistence=checkpoints, retry_policy=retry_policy)
        except OrchestratorError as exc:
            await checkpoints.commit_failure(exc)
            raise
        except asyncio.CancelledError:
            try:
                await checkpoints.commit_failure(OrchestratorError("interruption", "Execution was interrupted", code="server_interrupted"), interrupted=True)
            except SQLAlchemyError:
                pass  # Startup reconciliation must handle an unavailable database.
            raise


class Checkpoints:
    def __init__(self, sessions, debate_id: str, run_id: str, model_id: str, settings: Settings | None = None, *, dispatch_lock=None):
        self.sessions = sessions
        self.debate_id = debate_id
        self.run_id = run_id
        self.model_id = model_id
        self.settings = settings or Settings()
        self.retrying = False
        self.dispatch_lock = dispatch_lock

    async def _update(self, session, **values):
        result = await session.execute(update(Debate).where(
            Debate.id == self.debate_id, Debate.active_run_id == self.run_id,
            Debate.status.in_(["running", "judging"]), Debate.hidden.is_(False)
        ).values(revision=Debate.revision + 1, updated_at=utcnow(), **values))
        if result.rowcount != 1:
            raise OrchestratorError("ownership", "Execution no longer owns this debate", code="stale_run")

    async def before_call(self, stage: str, retrying: bool):
        self.retrying = retrying

    async def dispatch(self, provider, stage, **kwargs):
        from app.core.costs import dispatch_reserved
        return await dispatch_reserved(self, provider, stage, self.retrying, **kwargs)

    async def _commit_responses(self, session, stage):
        await session.execute(update(ProviderCallReservation).where(
            ProviderCallReservation.run_id == self.run_id, ProviderCallReservation.stage == stage,
            ProviderCallReservation.dispatch_state == "dispatched").values(dispatch_state="committed"))

    async def commit_speech(self, speech: Speech):
        async with self.sessions.begin() as session:
            await self._update(session, status="judging" if speech.turn_order == 6 else "running",
                               current_round=speech.round_number, active_turn=None, active_speaker=None,
                               activity="evaluating" if speech.turn_order == 6 else None)
            saved = await load_speeches(session, self.debate_id)
            validate_checkpoint(saved + [speech])
            session.add(DebateMessage(id=speech.message_id, debate_id=self.debate_id, agent=speech.agent.value,
                        side=speech.side.value, round_number=speech.round_number,
                        turn_order=speech.turn_order, content=speech.content))
            await self._commit_responses(session, f"speech_turn_{speech.turn_order}")

    async def commit_judgment(self, judgment):
        async with self.sessions.begin() as session:
            await self._update(session, status="completed", completed_at=utcnow(), active_run_id=None,
                               active_turn=None, active_speaker=None, activity=None)
            speeches = await load_speeches(session, self.debate_id)
            if len(speeches) != 6 or not set(judgment.referenced_message_ids) <= {s.message_id for s in speeches}:
                raise OrchestratorError("checkpoint", "Judgment does not match saved transcript", code="checkpoint_invalid")
            values = asdict(judgment)
            for side in ("pro", "con"):
                scores = values.pop(f"{side}_scores")
                values.update({f"{side}_{key}": value for key, value in scores.items()})
                values[f"{side}_total"] = sum(scores.values())
            diff = values["pro_total"] - values["con_total"]
            values["winner"] = "draw" if abs(diff) <= 5 else ("pro" if diff > 0 else "con")
            session.add(Judgment(debate_id=self.debate_id, model_id=self.model_id,
                                prompt_version=PROMPT_VERSION, rubric_version=RUBRIC_VERSION, **values))
            attempt = await session.get(ExecutionAttempt, self.run_id)
            attempt.status = "completed"
            attempt.finished_at = utcnow()
            await self._commit_responses(session, "judgment")

    async def commit_failure(self, error: OrchestratorError, *, interrupted: bool = False):
        turn = int(error.stage.rsplit("_", 1)[1]) if error.stage.startswith("speech_turn_") else None
        messages = {
            "provider_failed": "The provider could not complete this stage. You can resume from the saved checkpoint.",
            "provider_configuration": "Provider configuration or authentication needs attention.",
            "checkpoint_invalid": "Saved checkpoint is inconsistent and cannot be resumed.",
            "debate_call_limit": "This debate has reached its provider call allowance.",
            "server_interrupted": "Execution was interrupted. Saved speeches are preserved.",
            "attempt_timeout": "Execution exceeded its time limit. Saved speeches are preserved.",
            "internal_error": "Execution stopped unexpectedly. Saved speeches are preserved.",
            "storage_unavailable": "Storage was unavailable. Saved speeches are preserved.",
            "global_usage_limit": "Provider usage allowance exhausted. Saved speeches are preserved; resume manually after capacity returns.",
            "stage_call_limit": "This stage has reached its provider call allowance.",
            "usage_configuration": "Provider pricing, input accounting or spending configuration needs attention.",
            "prompt_too_large": "The saved debate exceeds the provider input allowance.",
            "token_accounting_unconfirmed": "Provider usage accounting requires review before more paid work.",
            "content_flagged": "Output was rejected by the profanity filter. Saved speeches are preserved; you can resume manually.",
        }
        async with self.sessions.begin() as session:
            result = await session.execute(update(Debate).where(
                Debate.id == self.debate_id, Debate.active_run_id == self.run_id
            ).values(status="failed", error_code=error.code, error_message=messages.get(error.code, "Execution stopped."),
                     failed_stage=error.stage, failed_turn=turn, active_run_id=None, active_turn=None,
                     active_speaker=None, activity=None, revision=Debate.revision + 1, updated_at=utcnow()))
            if result.rowcount:
                attempt = await session.get(ExecutionAttempt, self.run_id)
                attempt.status = "interrupted" if interrupted else "failed"
                attempt.finished_at = utcnow()
                attempt.failed_stage = error.stage
                attempt.failed_turn = turn
                await session.execute(update(ProviderCallReservation).where(
                    ProviderCallReservation.run_id == self.run_id,
                    ProviderCallReservation.dispatch_state.in_(["reserved", "dispatched"])
                ).values(dispatch_state="abandoned" if interrupted else "failed"))
