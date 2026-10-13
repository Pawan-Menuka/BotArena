from __future__ import annotations

import asyncio
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text

from app.api.errors import APIError
from app.api.debates import snapshot, utc
from app.db.models import CreationQuotaRecord, Debate, ProviderCallReservation, utcnow
from app.engine.retry import OrchestratorError

router = APIRouter(prefix="/api/admin")


def database_size(path):
    total = 0
    for filename in (str(path), str(path) + "-wal", str(path) + "-shm"):
        try:
            total += Path(filename).stat().st_size
        except FileNotFoundError:
            pass
    return total


def require_admin(request: Request):
    settings = request.app.state.settings
    supplied = request.headers.get("X-Admin-Token", "")
    if not settings.admin_token or not secrets.compare_digest(supplied.encode(), settings.admin_token.encode()):
        raise APIError(401, "admin_unauthorized", "Admin authorization required.")


@router.get("/stats", dependencies=[Depends(require_admin)])
async def stats(request: Request):
    settings = request.app.state.settings
    cutoff = utcnow() - timedelta(hours=24)
    async with request.app.state.sessions.begin() as session:
        await session.execute(text("BEGIN"))
        active = await session.scalar(select(func.count()).select_from(Debate).where(Debate.status.in_(["running", "judging"])))
        creations = await session.scalar(select(func.count()).select_from(CreationQuotaRecord).where(CreationQuotaRecord.created_at > cutoff))
        calls, reserved, actual, known = (await session.execute(select(func.count(),
            func.coalesce(func.sum(ProviderCallReservation.reserved_max_cost_cents), 0),
            func.coalesce(func.sum(ProviderCallReservation.actual_cost_cents), 0),
            func.count(ProviderCallReservation.actual_cost_cents)).where(ProviderCallReservation.reserved_at > cutoff))).one()
        failures = (await session.execute(select(Debate.failed_stage, Debate.error_code, func.count())
                    .where(Debate.status == "failed").group_by(Debate.failed_stage, Debate.error_code))).all()
        breaches = await session.scalar(select(func.count()).select_from(ProviderCallReservation).where(
            (ProviderCallReservation.actual_input_tokens > ProviderCallReservation.input_token_cap)
            | (ProviderCallReservation.actual_output_tokens > ProviderCallReservation.output_token_cap)))
    return {"active_debates": active, "creations_24h": creations, "provider_calls_24h": calls,
            "actual_cost_basis": "Reported tokens at uncached published rates, rounded up per call to whole cents; not a provider invoice.",
            "reserved_cost_24h_cents": reserved, "actual_cost_24h_cents": actual,
            "actual_cost_known_calls": known, "actual_cost_unknown_calls": calls - known,
            "remaining_calls_24h": max(0, settings.global_call_quota_24h - calls),
            "remaining_reserved_cost_24h_cents": max(0, settings.cost_cap_cents - reserved) if settings.cost_cap_cents is not None else None,
            "remaining_global_creations_24h": max(0, settings.global_create_quota_24h - creations),
            "accounting_breaches": breaches,
            "failures": [{"stage": stage, "code": code, "count": count} for stage, code, count in failures],
            "database_bytes": await asyncio.to_thread(database_size, settings.database_path)}


class HideDebate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="Hidden by operator.", strict=True, min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("A reason cannot be blank")
        return value


@router.post("/debates/{debate_id}/hide", dependencies=[Depends(require_admin)])
async def hide_debate(debate_id: str, request: Request, body: HideDebate | None = None):
    try:
        return await request.app.state.executions.hide(debate_id, reason=(body or HideDebate()).reason)
    except OrchestratorError as error:
        if error.code == "not_found":
            raise APIError(404, "not_found", "Battle not found.") from None
        raise


@router.get("/debates/{debate_id}", dependencies=[Depends(require_admin)])
async def inspect_debate(debate_id: str, request: Request):
    async with request.app.state.sessions.begin() as session:
        await session.execute(text("BEGIN"))
        debate = await session.get(Debate, debate_id)
        if debate is None:
            raise APIError(404, "not_found", "Battle not found.")
        result = await snapshot(session, debate, None)
        result["moderation"] = {"hidden": debate.hidden, "reason": debate.hidden_reason, "hidden_at": utc(debate.hidden_at)}
        result["execution"] = {"accepted_attempts": debate.accepted_attempts, "total_calls": debate.total_calls}
        return result
