"""Runs one fixed six-turn debate plus judging against a Provider.

This is Phase 1 scope only: pure in-memory sequencing and validation. Checkpointing,
persistence, resume, the task registry, and the full multi-call retry/reservation budget
are Phase 2/3 (blueprint.md §5, §13) and deliberately not here yet.
"""

from __future__ import annotations

from app.engine.models import TURN_ORDER, ROUND_NUMBER, DebateResult, Judgment, Speech, Topic
from app.engine.prompts import build_judge_prompt, build_speech_prompt
from app.engine.provider import Provider, ProviderError
from app.engine.judge import score_judgment
from app.engine.validation import (
    JUDGE_JSON_SCHEMA,
    ValidationFailure,
    validate_judgment,
    validate_speech,
)

SPEECH_MAX_OUTPUT_TOKENS = 512
JUDGMENT_MAX_OUTPUT_TOKENS = 2048
MAX_ATTEMPTS_PER_STAGE = 2  # one corrective retry; full stage budget is Phase 2/3 scope


class OrchestratorError(Exception):
    """Raised when a stage exhausts its attempts without producing valid output."""

    def __init__(self, stage: str, reason: str) -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(f"{stage}: {reason}")


async def run_debate(*, topic: Topic, provider: Provider) -> DebateResult:
    speeches: list[Speech] = []

    for turn_order, (round_name, agent, side) in enumerate(TURN_ORDER, start=1):
        round_number = ROUND_NUMBER[round_name]
        system, user = build_speech_prompt(
            topic=topic,
            agent=agent,
            side=side,
            round_name=round_name,
            round_number=round_number,
            prior=speeches,
        )

        last_error: str | None = None
        for _attempt in range(MAX_ATTEMPTS_PER_STAGE):
            try:
                response = await provider.complete(
                    system=system, user=user, max_output_tokens=SPEECH_MAX_OUTPUT_TOKENS
                )
                content = validate_speech(response.text)
                break
            except (ProviderError, ValidationFailure) as exc:
                last_error = str(exc)
        else:
            raise OrchestratorError(f"speech_turn_{turn_order}", last_error or "unknown failure")

        speeches.append(
            Speech(
                turn_order=turn_order,
                round=round_name,
                round_number=round_number,
                agent=agent,
                side=side,
                content=content,
            )
        )

    judgment = await _run_judging(topic=topic, speeches=speeches, provider=provider)
    return DebateResult(topic=topic, speeches=speeches, judgment=judgment)


async def _run_judging(*, topic: Topic, speeches: list[Speech], provider: Provider) -> Judgment:
    system, user = build_judge_prompt(topic=topic, speeches=speeches)
    known_ids = {s.message_id for s in speeches}

    last_error: str | None = None
    for _attempt in range(MAX_ATTEMPTS_PER_STAGE):
        try:
            response = await provider.complete(
                system=system,
                user=user,
                max_output_tokens=JUDGMENT_MAX_OUTPUT_TOKENS,
                json_schema=JUDGE_JSON_SCHEMA,
            )
            parsed = validate_judgment(response.text, known_message_ids=known_ids)
            return score_judgment(parsed)
        except (ProviderError, ValidationFailure) as exc:
            last_error = str(exc)

    raise OrchestratorError("judgment", last_error or "unknown failure")
