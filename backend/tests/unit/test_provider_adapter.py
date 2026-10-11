from __future__ import annotations

from types import SimpleNamespace

import groq
import pytest

from app.engine.provider import GroqProvider, ProviderError, REASONING_TOKEN_HEADROOM


@pytest.fixture
def sdk(monkeypatch):
    observed = {}

    async def create(**kwargs):
        observed["request"] = kwargs
        if "error" in observed:
            raise observed["error"]
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason=observed.get("finish_reason", "stop"), message=SimpleNamespace(content="Valid speech"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    def client(**kwargs):
        observed["client"] = kwargs
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(groq, "AsyncGroq", client)
    return observed


async def test_adapter_disables_sdk_retries_and_preserves_reasoning_caps(sdk):
    provider = GroqProvider(api_key="offline-test", model="test-model")
    result = await provider.complete(system="system", user="user", max_output_tokens=512, json_schema={"type": "object"})
    assert sdk["client"]["max_retries"] == 0
    assert sdk["client"]["timeout"] == 35
    assert sdk["request"]["max_completion_tokens"] == 512 + REASONING_TOKEN_HEADROOM
    assert sdk["request"]["reasoning_effort"] == "low"
    assert sdk["request"]["response_format"]["json_schema"]["strict"] is True
    assert (result.input_tokens, result.output_tokens) == (10, 20)


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (403, False), (429, True), (500, True)])
async def test_adapter_classifies_errors_without_exposing_diagnostics(sdk, status, retryable):
    class SDKError(Exception):
        status_code = status
        response = SimpleNamespace(headers={"retry-after": "9"})

    sdk["error"] = SDKError("SECRET provider diagnostics")
    provider = GroqProvider(api_key="offline-test", model="test-model")
    with pytest.raises(ProviderError) as error:
        await provider.complete(system="system", user="user", max_output_tokens=512)
    assert error.value.retryable is retryable
    assert error.value.retry_after == (9 if status == 429 else None)
    assert "SECRET" not in str(error.value)


async def test_adapter_rejects_truncated_output(sdk):
    sdk["finish_reason"] = "length"
    with pytest.raises(ProviderError, match="cut off"):
        await GroqProvider(api_key="offline-test", model="test-model").complete(system="system", user="user", max_output_tokens=512)


def test_missing_configuration_fails_without_retries(sdk, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError) as error:
        GroqProvider(model="test-model")
    assert not error.value.retryable
    assert "client" not in sdk
