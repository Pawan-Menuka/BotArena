from __future__ import annotations

import base64
import json
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, text
from starlette.responses import JSONResponse

from app.api.errors import APIError
from app.core.auth import token_hash, verify_token
from app.core.quotas import check_creation
from app.db.models import CreationQuotaRecord, Debate, DebateMessage, Judgment, utcnow
from app.db.repo import load_speeches
from app.engine.retry import OrchestratorError

router = APIRouter(prefix="/api/debates")


class CreateDebate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic_id: str = Field(strict=True, min_length=1, max_length=100)
    client_request_id: UUID


def utc(value: datetime | None):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


async def public_debate(session, debate_id: str) -> Debate:
    debate = await session.scalar(select(Debate).where(Debate.id == debate_id, Debate.hidden.is_(False)))
    if debate is None:
        raise APIError(404, "not_found", "Battle not found.")
    return debate


async def snapshot(session, debate: Debate, token: str | None):
    messages = (await session.scalars(select(DebateMessage).where(
        DebateMessage.debate_id == debate.id).order_by(DebateMessage.turn_order))).all()
    judgment = await session.scalar(select(Judgment).where(Judgment.debate_id == debate.id))
    result = {
        "id": debate.id, "topic": debate.topic_snapshot, "status": debate.status, "revision": debate.revision,
        "created_at": utc(debate.created_at), "updated_at": utc(debate.updated_at), "completed_at": utc(debate.completed_at),
        "progress": {"completed_turns": len(messages), "total_turns": 6, "current_round": debate.current_round,
                     "active_speaker": debate.active_speaker, "active_turn": debate.active_turn, "activity": debate.activity},
        "messages": [{"id": message.id, "agent": message.agent, "side": message.side,
                      "round_number": message.round_number, "turn_order": message.turn_order,
                      "content": message.content, "created_at": utc(message.created_at)} for message in messages],
        "judgment": None,
        "error": {"code": debate.error_code, "message": debate.error_message,
                  "failed_stage": debate.failed_stage, "failed_turn": debate.failed_turn} if debate.error_code else None,
    }
    if judgment is not None:
        result["judgment"] = {
            **{f"{side}_scores": {field: getattr(judgment, f"{side}_{field}") for field in
                                  ("logical_reasoning", "relevance", "rebuttal_quality", "clarity")} for side in ("pro", "con")},
            **{field: getattr(judgment, field) for field in ("pro_total", "con_total", "winner", "pro_strengths",
               "pro_weaknesses", "con_strengths", "con_weaknesses", "overall_explanation", "referenced_message_ids",
               "model_id", "prompt_version", "rubric_version")},
        }
    if verify_token(token, debate.execution_token_hash):
        eligible = debate.accepted_attempts < 3 and debate.total_calls < 30 and debate.error_code != "checkpoint_invalid"
        try:
            await load_speeches(session, debate.id)
        except OrchestratorError:
            eligible = False
        result["controls"] = {"can_start": eligible and debate.status == "created",
                              "can_resume": eligible and debate.status == "failed",
                              "remaining_attempts": max(0, 3 - debate.accepted_attempts)}
    return result


@router.post("")
async def create_debate(body: CreateDebate, request: Request):
    token = request.headers.get("X-Debate-Token")
    async with request.app.state.sessions.begin() as session:
        # Serialize creation before the replay lookup so concurrent copies cannot both insert.
        await session.execute(text("BEGIN IMMEDIATE"))
        debate = await session.scalar(select(Debate).where(Debate.client_request_id == str(body.client_request_id)))
        if debate is not None:
            if debate.hidden:
                raise APIError(404, "not_found", "Battle not found.")
            if not verify_token(token, debate.execution_token_hash):
                raise APIError(403, "forbidden", "Execution token required or invalid.")
            if debate.topic_id != body.topic_id:
                raise APIError(409, "request_conflict", "Request ID was already used with different input.")
            status = 200
        else:
            hashed = token_hash(token)
            if hashed is None:
                raise APIError(403, "forbidden", "Execution token required or invalid.")
            topic = request.app.state.topics.get(body.topic_id)
            if topic is None:
                raise APIError(422, "invalid_topic", "Choose a topic from the catalogue.")
            now = utcnow()
            shared = request.app.state.settings.deployment_mode == "shared"
            if shared:
                await check_creation(session, request.app.state.settings, request.state.identity, now)
            debate = Debate(client_request_id=str(body.client_request_id), topic_id=topic.id,
                            topic_snapshot=asdict(topic), execution_token_hash=hashed, created_at=now)
            session.add(debate)
            await session.flush()
            if shared:
                session.add(CreationQuotaRecord(debate_id=debate.id, created_at=now,
                            ip_bucket_hash=request.state.identity.bucket_hash, key_version=request.state.identity.key_version))
            status = 201
        result = await snapshot(session, debate, token)
    return JSONResponse(jsonable_encoder(result), status_code=status)


@router.post("/{debate_id}/run", status_code=202)
async def run(debate_id: str, request: Request):
    async with request.app.state.sessions() as session:
        debate = await public_debate(session, debate_id)
        if not verify_token(request.headers.get("X-Debate-Token"), debate.execution_token_hash):
            raise APIError(403, "forbidden", "Execution token required or invalid.")
    try:
        if request.app.state.settings.deployment_mode == "shared":
            raise APIError(503, "cost_governance_unavailable", "Shared execution is unavailable until provider spending guards are installed.")
        return await request.app.state.executions.admit(debate_id, identity=getattr(request.state, "identity", None))
    except OrchestratorError as exc:
        quota = exc.code in {"concurrency_per_ip", "global_concurrency"}
        status = 429 if quota else (404 if exc.code == "not_found" else (503 if exc.code == "capacity_unavailable" else 409))
        message = "Battle not found." if status == 404 else ("Execution capacity unavailable. Try again shortly." if status == 503 else "Battle cannot start or resume in its current state.")
        if quota:
            message = "Active execution allowance reached. Try again shortly."
        raise APIError(status, exc.code, message, retry_after=5 if quota else None) from None


@router.get("/{debate_id}")
async def get_debate(debate_id: str, request: Request):
    async with request.app.state.sessions.begin() as session:
        # sqlite's legacy transaction mode otherwise permits separate reads to see different revisions.
        await session.execute(text("BEGIN"))
        debate = await public_debate(session, debate_id)
        return await snapshot(session, debate, request.headers.get("X-Debate-Token"))


def encode_cursor(debate: Debate) -> str:
    value = json.dumps([debate.created_at.isoformat(timespec="microseconds"), debate.id]).encode()
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def decode_cursor(cursor: str):
    try:
        if len(cursor) > 512:
            raise ValueError
        created_at, debate_id = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        date = datetime.fromisoformat(created_at)
        if date.tzinfo is not None:
            date = date.astimezone(timezone.utc).replace(tzinfo=None)
        return date, str(UUID(debate_id))
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise APIError(422, "invalid_cursor", "Invalid history cursor.") from None


@router.get("")
async def list_debates(request: Request, limit: int = Query(20, ge=1, le=100), cursor: str | None = None):
    query = select(Debate).where(Debate.hidden.is_(False))
    if cursor is not None:
        created_at, debate_id = decode_cursor(cursor)
        query = query.where(or_(Debate.created_at < created_at,
                                and_(Debate.created_at == created_at, Debate.id < debate_id)))
    async with request.app.state.sessions() as session:
        rows = (await session.scalars(query.order_by(Debate.created_at.desc(), Debate.id.desc()).limit(limit + 1))).all()
    page = rows[:limit]
    return {"items": [{"id": debate.id, "topic": debate.topic_snapshot, "status": debate.status,
                       "created_at": utc(debate.created_at), "completed_at": utc(debate.completed_at)} for debate in page],
            "next_cursor": encode_cursor(page[-1]) if len(rows) > limit else None}
