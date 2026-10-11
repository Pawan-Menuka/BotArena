"""Output validation. blueprint.md §14: reject empty/excessive output rather than
silently truncating it; validate judge structure, score bounds, and message references.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

SPEECH_WORD_CAP = 250


class ValidationFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_speech(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValidationFailure("empty speech output")
    word_count = len(stripped.split())
    if word_count > SPEECH_WORD_CAP:
        raise ValidationFailure(f"speech exceeds {SPEECH_WORD_CAP} words ({word_count})")
    return stripped


class _CriterionScoresModel(BaseModel):
    logical_reasoning: int = Field(ge=0, le=35)
    relevance: int = Field(ge=0, le=25)
    rebuttal_quality: int = Field(ge=0, le=25)
    clarity: int = Field(ge=0, le=15)


_CRITERIA = ["logical_reasoning", "relevance", "rebuttal_quality", "clarity"]
_SCORES_SCHEMA = {
    "type": "object",
    "properties": {name: {"type": "integer"} for name in _CRITERIA},
    "required": _CRITERIA,
    "additionalProperties": False,
}

# Structure-only schema for Groq strict mode (constrained decoding). Score ranges and
# message-id existence are NOT expressible there, so JudgeOutputModel still enforces them.
JUDGE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "pro": _SCORES_SCHEMA,
        "con": _SCORES_SCHEMA,
        "pro_strengths": {"type": "string"},
        "pro_weaknesses": {"type": "string"},
        "con_strengths": {"type": "string"},
        "con_weaknesses": {"type": "string"},
        "overall_explanation": {"type": "string"},
        "referenced_message_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "pro", "con", "pro_strengths", "pro_weaknesses", "con_strengths",
        "con_weaknesses", "overall_explanation", "referenced_message_ids",
    ],
    "additionalProperties": False,
}


class JudgeOutputModel(BaseModel):
    pro: _CriterionScoresModel
    con: _CriterionScoresModel
    pro_strengths: str = Field(min_length=1)
    pro_weaknesses: str = Field(min_length=1)
    con_strengths: str = Field(min_length=1)
    con_weaknesses: str = Field(min_length=1)
    overall_explanation: str = Field(min_length=1)
    referenced_message_ids: list[str] = Field(min_length=1)


def validate_judgment(raw_text: str, *, known_message_ids: set[str]) -> JudgeOutputModel:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValidationFailure(f"judge output is not valid JSON: {exc}") from exc

    try:
        parsed = JudgeOutputModel.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailure(f"judge output failed schema validation: {exc}") from exc

    unknown = [mid for mid in parsed.referenced_message_ids if mid not in known_message_ids]
    if unknown:
        raise ValidationFailure(f"judge referenced unknown message ids: {unknown}")

    return parsed
