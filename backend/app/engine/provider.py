"""Provider adapter: the one seam between the engine and an actual AI backend.

blueprint.md §7/§13: model and pricing are intentionally NOT hardcoded here — pick and
verify the real model via GROQ_MODEL during this Phase 1 proof-of-concept, against Groq's
current model list and docs, not against this file's defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol
from app.engine.budget import REASONING_TOKEN_HEADROOM, authorize_dispatch, request_payload


class ProviderError(Exception):
    """Raised when the provider fails to produce usable output."""

    def __init__(self, message: str, *, retryable: bool = True, retry_after: float | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class Provider(Protocol):
    async def complete(
        self, *, system: str, user: str, max_output_tokens: int, json_schema: dict | None = None
    ) -> ProviderResponse: ...


class FakeProvider:
    """Deterministic, offline provider for tests and the engine-proof CLI run.

    `speeches` is consumed in order, one per debater call. `judgment` is returned verbatim
    (as a JSON string) for the judge call. Both can be overridden per-test to exercise
    validation failure paths.
    """

    def __init__(
        self,
        *,
        speeches: list[str] | None = None,
        judgment: dict | None = None,
    ) -> None:
        self._speeches = list(speeches) if speeches is not None else list(_DEFAULT_SPEECHES)
        self._judgment = judgment
        self._speech_calls = 0
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, *, system: str, user: str, max_output_tokens: int, json_schema: dict | None = None
    ) -> ProviderResponse:
        self.calls.append((system, user))
        if "JSON object" in system:  # judge call — see prompts.JUDGE_SCHEMA_DESCRIPTION
            judgment = self._judgment
            if judgment is None:
                import re

                judgment = dict(_DEFAULT_JUDGMENT)
                judgment["referenced_message_ids"] = re.findall(r"^\[([^\]]+)\] Side [AB] ", user, re.MULTILINE)
            return ProviderResponse(text=json.dumps(judgment))
        if self._speech_calls >= len(self._speeches):
            raise ProviderError("FakeProvider ran out of scripted speeches")
        text = self._speeches[self._speech_calls]
        self._speech_calls += 1
        return ProviderResponse(text=text)


_DEFAULT_SPEECHES = [
    "Pro opening: the policy delivers clear, measurable benefits that outweigh its costs, "
    "based on the available evidence.",
    "Con opening: the policy's costs and unintended consequences are understated, and "
    "better alternatives exist.",
    "Pro rebuttal: Con's cost concerns assume the worst case and ignore mitigations already "
    "in place; the core benefit argument stands.",
    "Con rebuttal: Pro has not addressed the distributional impact raised in our opening, "
    "and the mitigations cited are unproven at scale.",
    "Pro closing: on balance, the measurable benefits and addressed mitigations support "
    "adoption.",
    "Con closing: the unresolved distributional and scaling concerns mean the burden of "
    "proof has not been met.",
]

_DEFAULT_JUDGMENT = {
    "pro": {"logical_reasoning": 28, "relevance": 20, "rebuttal_quality": 20, "clarity": 12},
    "con": {"logical_reasoning": 26, "relevance": 19, "rebuttal_quality": 18, "clarity": 12},
    "pro_strengths": "Clear, evidence-based core claim; addressed the cost objection directly.",
    "pro_weaknesses": "Did not fully engage the distributional-impact point.",
    "con_strengths": "Identified a real gap in Pro's mitigation evidence.",
    "con_weaknesses": "Did not quantify the alternatives it alluded to.",
    "overall_explanation": "Both sides argued coherently; Pro's rebuttal was marginally "
    "stronger because it directly engaged Con's stated concern.",
    "referenced_message_ids": ["m1", "m2", "m3", "m4"],
}


class GroqProvider:
    """Real provider adapter using the official `groq` SDK.

    Not exercised by the unit test suite (no network). Run it via
    `uv run python -m app.cli --provider groq --topic <id>` once GROQ_API_KEY and
    GROQ_MODEL are set, to satisfy Phase 1's "one coherent CLI debate with a real model"
    exit criterion.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        from groq import AsyncGroq  # imported lazily so tests never need the package reachable

        self._model = model or os.environ.get("GROQ_MODEL")
        if not self._model:
            raise ProviderError(
                "GROQ_MODEL is not set. Check Groq's current model list at "
                "https://console.groq.com/docs/models and pick one that supports the "
                "context length this app needs (see blueprint.md §7), then set it.",
                retryable=False,
            )
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise ProviderError("GROQ_API_KEY is not set", retryable=False)
        self._client = AsyncGroq(api_key=key, max_retries=0, timeout=35)

    async def complete(
        self, *, system: str, user: str, max_output_tokens: int, json_schema: dict | None = None
    ) -> ProviderResponse:
        try:
            authorize_dispatch(self._model, system, user, max_output_tokens, json_schema)
        except ValueError:
            raise ProviderError("A durable provider reservation is required", retryable=False) from None
        kwargs = {"model": self._model, **request_payload(system, user, max_output_tokens, json_schema)}

        try:
            completion = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", {})
            from app.engine.retry import parse_retry_after

            raise ProviderError(
                "Provider request failed",
                retryable=status is None or status in {408, 429} or status >= 500,
                retry_after=parse_retry_after(headers.get("retry-after")) if status == 429 else None,
            ) from exc

        choice = completion.choices[0]
        usage = getattr(completion, "usage", None)
        counts = {"input_tokens": getattr(usage, "prompt_tokens", None), "output_tokens": getattr(usage, "completion_tokens", None)}
        if choice.finish_reason == "length":
            raise ProviderError("Groq output was cut off at the token limit", **counts)
        text = choice.message.content or ""
        if not text.strip():
            raise ProviderError("Groq returned empty output", **counts)
        return ProviderResponse(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )

    async def aclose(self) -> None:
        await self._client.close()
