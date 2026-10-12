from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Debate, ExecutionAttempt, utcnow
from app.db.repo import DebateRepository
from app.engine.orchestrator import run_debate
from app.engine.retry import OrchestratorError

logger = logging.getLogger(__name__)


async def reconcile(sessions, live_run_ids: set[str]) -> None:
    async with sessions.begin() as session:
        conditions = [Debate.status.in_(["running", "judging"])]
        if live_run_ids:
            conditions.append(Debate.active_run_id.is_(None) | Debate.active_run_id.not_in(live_run_ids))
        result = await session.execute(update(Debate).where(*conditions).values(
            status="failed", active_run_id=None, activity=None, active_turn=None, active_speaker=None,
            error_code="server_interrupted", error_message="Execution was interrupted. Saved speeches are preserved.",
            failed_stage="interruption", failed_turn=None, revision=Debate.revision + 1, updated_at=utcnow(),
        ).returning(Debate.id))
        ids = result.scalars().all()
        if ids:
            await session.execute(update(ExecutionAttempt).where(
                ExecutionAttempt.debate_id.in_(ids), ExecutionAttempt.status == "active"
            ).values(status="interrupted", finished_at=utcnow(), failed_stage="interruption"))
        # An attempt may outlive its ownership after a failure between lifecycle writes.
        owned = select(Debate.active_run_id).where(Debate.status.in_(["running", "judging"]), Debate.active_run_id.is_not(None))
        await session.execute(update(ExecutionAttempt).where(
            ExecutionAttempt.status == "active", ExecutionAttempt.run_id.not_in(owned)
        ).values(status="interrupted", finished_at=utcnow(), failed_stage="interruption"))


class ExecutionManager:
    def __init__(self, sessions, provider_factory, model_id: str, *, retry_policy=None,
                 attempt_timeout: float = 900, shutdown_grace: float = 10, supervisor_interval: float = 5, settings=None):
        self.sessions = sessions
        self.repo = DebateRepository(sessions)
        self.provider_factory = provider_factory
        self.model_id = model_id
        self.retry_policy = retry_policy
        self.attempt_timeout = attempt_timeout
        self.shutdown_grace = shutdown_grace
        self.supervisor_interval = supervisor_interval
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()
        self.admitting = True
        self.supervisor: asyncio.Task | None = None

    def start_supervisor(self):
        self.supervisor = asyncio.create_task(self._supervise(), name="debate-supervisor")

    async def admit(self, debate_id: str, *, identity=None):
        async with self.lock:
            if not self.admitting:
                raise OrchestratorError("admission", "Server is shutting down", code="capacity_unavailable")
            shared = self.settings is not None and self.settings.deployment_mode == "shared"
            if shared and identity is None:
                raise ValueError("Shared admission requires a client identity")
            checkpoints, topic, speeches = await self.repo.begin_attempt(
                debate_id, model_id=self.model_id, capacity=self.settings.global_concurrency if shared else 2,
                ip_bucket_hash=identity.bucket_hash if shared else None,
                concurrency_per_ip=self.settings.concurrency_per_ip if shared else None)
            task = asyncio.create_task(self._worker(checkpoints, topic, speeches), name=f"debate-{debate_id}")
            self.tasks[checkpoints.run_id] = task
            task.add_done_callback(lambda finished: self._finished(checkpoints.run_id, finished))
            return {"id": debate_id, "status": "judging" if len(speeches) == 6 else "running"}

    def _finished(self, run_id, task):
        self.tasks.pop(run_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Debate worker stopped; reconciliation will preserve its checkpoint")

    async def _failure(self, checkpoints, code, *, interrupted=False):
        try:
            await checkpoints.commit_failure(OrchestratorError("execution", "Execution stopped", code=code), interrupted=interrupted)
        except SQLAlchemyError:
            logger.warning("Could not persist execution failure; reconciliation required")

    async def _worker(self, checkpoints, topic, speeches):
        provider = None
        try:
            async with asyncio.timeout(self.attempt_timeout):
                provider = self.provider_factory()
                await run_debate(topic=topic, provider=provider, prior=speeches, persistence=checkpoints,
                                 retry_policy=self.retry_policy)
        except asyncio.CancelledError:
            await self._failure(checkpoints, "server_interrupted", interrupted=True)
            raise
        except TimeoutError:
            await self._failure(checkpoints, "attempt_timeout")
        except OrchestratorError as exc:
            try:
                await checkpoints.commit_failure(exc)
            except SQLAlchemyError:
                logger.warning("Could not persist stage failure; reconciliation required")
        except SQLAlchemyError:
            await self._failure(checkpoints, "storage_unavailable")
        except Exception:
            await self._failure(checkpoints, "internal_error")
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.warning("Could not close provider connection")

    async def reconcile(self):
        async with self.lock:
            live = {run_id for run_id, task in self.tasks.items() if not task.done()}
            await reconcile(self.sessions, live)

    async def _supervise(self):
        while True:
            await asyncio.sleep(self.supervisor_interval)
            try:
                await self.reconcile()
            except SQLAlchemyError:
                logger.warning("Execution supervisor could not reach storage")

    async def close(self):
        async with self.lock:
            self.admitting = False
        if self.supervisor is not None:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
        tasks = list(self.tasks.values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.shutdown_grace)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.reconcile()
        except SQLAlchemyError:
            logger.warning("Shutdown could not reach storage; startup reconciliation required")
