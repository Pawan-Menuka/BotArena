from __future__ import annotations

from uuid import uuid4

from app.engine.models import TURN_ORDER, ROUND_NUMBER, DebateResult, Speech, Topic
from app.engine.prompts import build_judge_prompt, build_speech_prompt
from app.engine.provider import Provider
from app.engine.judge import score_judgment
from app.engine.retry import OrchestratorError, RetryPolicy
from app.engine.validation import JUDGE_JSON_SCHEMA, validate_judgment, validate_speech

SPEECH_MAX_OUTPUT_TOKENS = 512
JUDGMENT_MAX_OUTPUT_TOKENS = 2048


def validate_checkpoint(speeches: list[Speech]) -> None:
    if len(speeches) > 6 or len({s.message_id for s in speeches}) != len(speeches):
        raise OrchestratorError("checkpoint", "Saved checkpoint is inconsistent", code="checkpoint_invalid")
    for position, speech in enumerate(speeches, start=1):
        round_name, agent, side = TURN_ORDER[position - 1]
        if (speech.turn_order, speech.round, speech.round_number, speech.agent, speech.side) != (
            position, round_name, ROUND_NUMBER[round_name], agent, side
        ):
            raise OrchestratorError("checkpoint", "Saved checkpoint is inconsistent", code="checkpoint_invalid")


async def run_debate(*, topic: Topic, provider: Provider, prior: list[Speech] | None = None,
                     persistence=None, retry_policy: RetryPolicy | None = None) -> DebateResult:
    speeches = list(prior or [])
    validate_checkpoint(speeches)
    policy = retry_policy or RetryPolicy()
    calls = 0

    async def before_call(stage, retrying):
        nonlocal calls
        if persistence is not None:
            await persistence.before_call(stage, retrying)
        elif calls >= 30:
            raise OrchestratorError(stage, "Debate call allowance exhausted", code="debate_call_limit")
        calls += 1

    for turn_order in range(len(speeches) + 1, 7):
        round_name, agent, side = TURN_ORDER[turn_order - 1]
        system, user = build_speech_prompt(topic=topic, agent=agent, side=side, round_name=round_name,
                                         round_number=ROUND_NUMBER[round_name], prior=speeches)

        async def dispatch(correction):
            return await provider.complete(system=system, user=user + correction, max_output_tokens=SPEECH_MAX_OUTPUT_TOKENS)

        content = await policy.run(f"speech_turn_{turn_order}", dispatch, validate_speech, before_call)
        speech = Speech(turn_order=turn_order, round=round_name, round_number=ROUND_NUMBER[round_name],
                        agent=agent, side=side, content=content,
                        saved_id=str(uuid4()) if persistence is not None else None)
        if persistence is not None:
            await persistence.commit_speech(speech)
        speeches.append(speech)

    system, user = build_judge_prompt(topic=topic, speeches=speeches)

    async def dispatch_judge(correction):
        return await provider.complete(system=system, user=user + correction,
                                       max_output_tokens=JUDGMENT_MAX_OUTPUT_TOKENS, json_schema=JUDGE_JSON_SCHEMA)

    judgment = await policy.run("judgment", dispatch_judge,
                                lambda raw: score_judgment(validate_judgment(raw, known_message_ids={s.message_id for s in speeches})), before_call)
    if persistence is not None:
        await persistence.commit_judgment(judgment)
    return DebateResult(topic=topic, speeches=speeches, judgment=judgment)
