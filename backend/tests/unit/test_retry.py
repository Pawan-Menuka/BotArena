from __future__ import annotations

import asyncio

import pytest

from app.engine.provider import ProviderError, ProviderResponse
from app.engine.retry import OrchestratorError, RetryPolicy, parse_retry_after
from app.engine.validation import validate_speech


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay
        self.delays.append(delay)


async def noop(*_args):
    pass


async def test_three_call_budget_and_retry_after():
    clock = Clock()
    policy = RetryPolicy(clock=clock.time, sleep=clock.sleep, jitter=lambda: 0)
    calls = []

    async def dispatch(correction):
        calls.append(correction)
        raise ProviderError("limited", retry_after=8)

    with pytest.raises(OrchestratorError):
        await policy.run("judgment", dispatch, validate_speech, noop)
    assert len(calls) == 3
    assert clock.delays == [8, 8]


async def test_retry_after_beyond_deadline_stops():
    clock = Clock()
    policy = RetryPolicy(clock=clock.time, sleep=clock.sleep)
    calls = []

    async def dispatch(_correction):
        calls.append(1)
        raise ProviderError("limited", retry_after=121)

    with pytest.raises(OrchestratorError):
        await policy.run("speech_turn_1", dispatch, validate_speech, noop)
    assert calls == [1]
    assert not clock.delays


async def test_elapsed_stage_deadline_prevents_another_call():
    clock = Clock()
    calls = []

    async def dispatch(_correction):
        calls.append(1)
        clock.now = 119.5
        raise ProviderError("temporarily unavailable")

    with pytest.raises(OrchestratorError):
        await RetryPolicy(clock=clock.time, sleep=clock.sleep, jitter=lambda: 0).run("judgment", dispatch, validate_speech, noop)
    assert calls == [1]
    assert not clock.delays


async def test_invalid_output_has_corrective_retry_in_same_budget():
    clock = Clock()
    policy = RetryPolicy(clock=clock.time, sleep=clock.sleep, jitter=lambda: 0)
    prompts = []

    async def dispatch(correction):
        prompts.append(correction)
        return ProviderResponse("" if len(prompts) < 3 else "Valid speech")

    assert await policy.run("speech_turn_1", dispatch, validate_speech, noop) == "Valid speech"
    assert prompts[0] == ""
    assert "failed validation" in prompts[1]
    assert clock.delays == [1, 3]


async def test_auth_failure_is_not_retried_or_exposed():
    async def dispatch(_correction):
        raise ProviderError("secret raw error", retryable=False)

    with pytest.raises(OrchestratorError) as error:
        await RetryPolicy().run("judgment", dispatch, validate_speech, noop)
    assert error.value.code == "provider_configuration"
    assert "secret" not in str(error.value)


async def test_call_deadline_cancels_provider():
    clock = Clock()
    cancelled = []

    async def dispatch(_correction):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    with pytest.raises(OrchestratorError):
        await RetryPolicy(call_timeout=0.005, clock=clock.time, sleep=clock.sleep).run("judgment", dispatch, validate_speech, noop)
    assert len(cancelled) == 3


def test_retry_after_parsing():
    assert parse_retry_after("12") == 12
    assert parse_retry_after("-1") == 0
    assert parse_retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0
    assert parse_retry_after("garbage") is None
    assert parse_retry_after(None) is None
