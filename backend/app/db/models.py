from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Debate(Base):
    __tablename__ = "debates"
    __table_args__ = (
        CheckConstraint("status IN ('created','running','judging','completed','failed')"),
        CheckConstraint("activity IS NULL OR activity IN ('generating','retrying','evaluating')"),
        CheckConstraint("accepted_attempts BETWEEN 0 AND 3"),
        CheckConstraint("total_calls BETWEEN 0 AND 30"),
        Index("ix_debates_history", "hidden", "created_at"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    client_request_id: Mapped[str] = mapped_column(String, unique=True)
    topic_id: Mapped[str] = mapped_column(String)
    topic_snapshot: Mapped[dict] = mapped_column(JSON)
    execution_token_hash: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="created")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden_reason: Mapped[str | None] = mapped_column(Text)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    active_speaker: Mapped[str | None] = mapped_column(String)
    active_turn: Mapped[int | None] = mapped_column(Integer)
    activity: Mapped[str | None] = mapped_column(String)
    accepted_attempts: Mapped[int] = mapped_column(Integer, default=0)
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    active_run_id: Mapped[str | None] = mapped_column(String)
    failed_stage: Mapped[str | None] = mapped_column(String)
    failed_turn: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DebateMessage(Base):
    __tablename__ = "debate_messages"
    __table_args__ = (
        UniqueConstraint("debate_id", "turn_order"),
        CheckConstraint("turn_order BETWEEN 1 AND 6"),
        CheckConstraint("round_number BETWEEN 1 AND 3"),
        CheckConstraint("agent IN ('atlas','nova')"),
        CheckConstraint("side IN ('pro','con')"),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id"))
    agent: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    round_number: Mapped[int] = mapped_column(Integer)
    turn_order: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Judgment(Base):
    __tablename__ = "judgments"
    __table_args__ = (
        CheckConstraint("winner IN ('pro','con','draw')"),
        *(CheckConstraint(f"{side}_{field} BETWEEN 0 AND {cap}") for side in ("pro", "con")
          for field, cap in (("logical_reasoning", 35), ("relevance", 25), ("rebuttal_quality", 25), ("clarity", 15))),
    )
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id"), unique=True)
    pro_logical_reasoning: Mapped[int] = mapped_column(Integer)
    pro_relevance: Mapped[int] = mapped_column(Integer)
    pro_rebuttal_quality: Mapped[int] = mapped_column(Integer)
    pro_clarity: Mapped[int] = mapped_column(Integer)
    con_logical_reasoning: Mapped[int] = mapped_column(Integer)
    con_relevance: Mapped[int] = mapped_column(Integer)
    con_rebuttal_quality: Mapped[int] = mapped_column(Integer)
    con_clarity: Mapped[int] = mapped_column(Integer)
    pro_total: Mapped[int] = mapped_column(Integer)
    con_total: Mapped[int] = mapped_column(Integer)
    winner: Mapped[str] = mapped_column(String)
    pro_strengths: Mapped[str] = mapped_column(Text)
    pro_weaknesses: Mapped[str] = mapped_column(Text)
    con_strengths: Mapped[str] = mapped_column(Text)
    con_weaknesses: Mapped[str] = mapped_column(Text)
    overall_explanation: Mapped[str] = mapped_column(Text)
    referenced_message_ids: Mapped[list[str]] = mapped_column(JSON)
    model_id: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    rubric_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"
    __table_args__ = (
        UniqueConstraint("debate_id", "attempt_number"),
        CheckConstraint("status IN ('active','completed','failed','interrupted')"),
        Index("ix_attempts_debate_status", "debate_id", "status"),
    )
    run_id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starting_checkpoint: Mapped[int] = mapped_column(Integer)
    failed_stage: Mapped[str | None] = mapped_column(String)
    failed_turn: Mapped[int | None] = mapped_column(Integer)
    model_id: Mapped[str] = mapped_column(String)
    prompt_version: Mapped[str] = mapped_column(String)
    call_count: Mapped[int] = mapped_column(Integer, default=0)
    ip_bucket_hash: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreationQuotaRecord(Base):
    __tablename__ = "creation_quota_records"
    __table_args__ = (Index("ix_creations_bucket_time", "ip_bucket_hash", "created_at"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ip_bucket_hash: Mapped[str | None] = mapped_column(String)
    key_version: Mapped[int] = mapped_column(Integer)


class ProviderCallReservation(Base):
    __tablename__ = "provider_call_reservations"
    __table_args__ = (CheckConstraint("dispatch_state IN ('reserved','dispatched','committed','failed','abandoned')"),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    debate_id: Mapped[str] = mapped_column(ForeignKey("debates.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_attempts.run_id"))
    stage: Mapped[str] = mapped_column(String)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    model_id: Mapped[str] = mapped_column(String)
    price_version: Mapped[str] = mapped_column(String)
    input_token_cap: Mapped[int] = mapped_column(Integer)
    output_token_cap: Mapped[int] = mapped_column(Integer)
    reserved_max_cost_cents: Mapped[int] = mapped_column(Integer)
    dispatch_state: Mapped[str] = mapped_column(String, default="reserved")
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer)
    actual_cost_cents: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
