from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

from app.engine.provider import ProviderError
from app.engine.validation import ValidationFailure

T = TypeVar("T")


class OrchestratorError(Exception):
    def __init__(self, stage: str, reason: str, *, code: str = "provider_failed") -> None:
        self.stage = stage
        self.reason = reason
        self.code = code
        super().__init__(f"{stage}: {reason}")


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


@dataclass(frozen=True)
class RetryPolicy:
    call_timeout: float = 35
    stage_timeout: float = 120
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    jitter: Callable[[], float] = random.random

    async def run(self, stage: str, dispatch, validate, before_call) -> T:
        deadline = self.clock() + self.stage_timeout
        correction = ""
        failure_code = "provider_failed"
        for attempt in range(3):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            await before_call(stage, attempt > 0)
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(self.call_timeout, remaining)):
                    response = await dispatch(correction)
                return validate(response.text)
            except (ProviderError, ValidationFailure, TimeoutError) as exc:
                failure_code = exc.code if isinstance(exc, ValidationFailure) else "provider_failed"
                if isinstance(exc, ProviderError) and not exc.retryable:
                    raise OrchestratorError(stage, "Provider configuration or authentication failed", code="provider_configuration") from exc
                if isinstance(exc, ValidationFailure):
                    correction = ("\nYour previous output was rejected by the profanity filter. Rewrite without profanity while preserving the requested format, argument, score bounds, references and length rules."
                                  if exc.code == "content_flagged" else "\nYour previous output failed validation. Return a complete response satisfying the specified format, score bounds, message references and length rules.")
                if attempt == 2:
                    break
                delay = (1, 3)[attempt] + self.jitter() * 0.25
                if isinstance(exc, ProviderError) and exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                if delay >= deadline - self.clock():
                    break
                await self.sleep(delay)
        raise OrchestratorError(stage, "Stage could not produce valid output within its retry budget", code=failure_code)
