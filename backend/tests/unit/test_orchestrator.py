import pytest

from app.engine.models import Agent, RoundName, Side, Topic
from app.engine.orchestrator import OrchestratorError, run_debate
from app.engine.provider import FakeProvider

TOPIC = Topic(
    id="t1",
    title="Test topic",
    pro_framing="Pro framing.",
    con_framing="Con framing.",
)

EXPECTED_SEQUENCE = [
    (RoundName.OPENING, Agent.ATLAS, Side.PRO),
    (RoundName.OPENING, Agent.NOVA, Side.CON),
    (RoundName.REBUTTAL, Agent.ATLAS, Side.PRO),
    (RoundName.REBUTTAL, Agent.NOVA, Side.CON),
    (RoundName.CLOSING, Agent.ATLAS, Side.PRO),
    (RoundName.CLOSING, Agent.NOVA, Side.CON),
]


async def test_produces_six_speeches_in_fixed_order():
    result = await run_debate(topic=TOPIC, provider=FakeProvider())

    assert len(result.speeches) == 6
    for i, speech in enumerate(result.speeches):
        assert speech.turn_order == i + 1
        round_name, agent, side = EXPECTED_SEQUENCE[i]
        assert speech.round == round_name
        assert speech.agent == agent
        assert speech.side == side
        assert speech.message_id == f"m{i + 1}"


async def test_produces_one_committed_judgment():
    result = await run_debate(topic=TOPIC, provider=FakeProvider())

    assert result.judgment.winner in {"pro", "con", "draw"}
    assert result.judgment.pro_scores.total <= 35 + 25 + 25 + 15
    assert result.judgment.con_scores.total <= 35 + 25 + 25 + 15


async def test_atlas_opening_has_no_prior_context():
    provider = FakeProvider()
    await run_debate(topic=TOPIC, provider=provider)

    first_call_system, first_call_user = provider.calls[0]
    assert "no prior debate text" in first_call_user


async def test_later_turns_see_prior_speeches():
    provider = FakeProvider()
    await run_debate(topic=TOPIC, provider=provider)

    # Turn 3 (Atlas rebuttal) must have seen turns 1 and 2 in its prompt.
    _, turn3_user = provider.calls[2]
    assert "[m1]" in turn3_user
    assert "[m2]" in turn3_user
    # Turn 6 (Nova closing) must have seen all five prior speeches.
    _, turn6_user = provider.calls[5]
    for mid in ("m1", "m2", "m3", "m4", "m5"):
        assert f"[{mid}]" in turn6_user


async def test_fixed_sides_cannot_be_overridden_by_model_output():
    # Even if the fake provider's text argued the "wrong" side, the Speech record's
    # side/agent fields are set by the orchestrator from TURN_ORDER, not parsed from
    # the model's text — so there is nothing for the model to override.
    provider = FakeProvider(speeches=["I actually support Con here."] * 6)
    result = await run_debate(topic=TOPIC, provider=provider)
    assert result.speeches[0].side == Side.PRO
    assert result.speeches[1].side == Side.CON


async def test_empty_speech_output_fails_the_stage_not_silently_truncated():
    provider = FakeProvider(speeches=["", "", "con opener"])
    with pytest.raises(OrchestratorError) as exc_info:
        await run_debate(topic=TOPIC, provider=provider)
    assert exc_info.value.stage == "speech_turn_1"


async def test_oversized_speech_is_rejected():
    long_speech = "word " * 300
    provider = FakeProvider(speeches=[long_speech, long_speech])
    with pytest.raises(OrchestratorError) as exc_info:
        await run_debate(topic=TOPIC, provider=provider)
    assert exc_info.value.stage == "speech_turn_1"


async def test_judge_failure_after_exhausting_retries_raises():
    provider = FakeProvider(judgment={"not": "valid"})
    with pytest.raises(OrchestratorError) as exc_info:
        await run_debate(topic=TOPIC, provider=provider)
    assert exc_info.value.stage == "judgment"
