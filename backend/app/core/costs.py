from __future__ import annotations

from datetime import timedelta
import asyncio

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db.models import Debate, ExecutionAttempt, ProviderCallReservation, utcnow
from app.engine.budget import INPUT_TOKEN_CAP, REASONING_TOKEN_HEADROOM, input_bound, price_for
from app.engine.retry import OrchestratorError
from app.engine.models import TURN_ORDER, ROUND_NUMBER


async def check_allowance(session, settings, model_id, amount, stage, now):
    window = ProviderCallReservation.reserved_at > now - timedelta(hours=24)
    calls, cost = (await session.execute(select(func.count(), func.coalesce(func.sum(
        ProviderCallReservation.reserved_max_cost_cents), 0)).where(window))).one()
    if calls >= settings.global_call_quota_24h or (settings.cost_cap_cents is not None and cost + amount > settings.cost_cap_cents):
        raise OrchestratorError(stage, "Global provider usage allowance exhausted", code="global_usage_limit")
    if model_id != "fake":
        breach = await session.scalar(select(ProviderCallReservation.id).where(
            ProviderCallReservation.model_id == model_id,
            ProviderCallReservation.price_version == price_for(model_id).version,
            (ProviderCallReservation.actual_input_tokens > ProviderCallReservation.input_token_cap)
            | (ProviderCallReservation.actual_output_tokens > ProviderCallReservation.output_token_cap)).limit(1))
        if breach is not None:
            raise OrchestratorError(stage, "Provider accounting requires review", code="token_accounting_unconfirmed")


async def reserve(checkpoints, stage, retrying, system, user, max_output_tokens, json_schema):
    settings = checkpoints.settings
    if stage not in {"judgment", *(f"speech_turn_{turn}" for turn in range(1, 7))}:
        raise OrchestratorError(stage, "Unexpected execution stage", code="usage_configuration")
    try:
        settings.validate_execution_budget(checkpoints.model_id)
        price = price_for(checkpoints.model_id)
    except ValueError:
        raise OrchestratorError(stage, "Provider usage configuration needs attention", code="usage_configuration") from None
    if max_output_tokens not in {512, 2048} or (stage == "judgment") != (max_output_tokens == 2048):
        raise OrchestratorError(stage, "Unexpected output cap", code="usage_configuration")
    output_cap = max_output_tokens + REASONING_TOKEN_HEADROOM
    input_cap = min(INPUT_TOKEN_CAP, price.context_window - output_cap)
    if input_bound(system, user, max_output_tokens, json_schema) > input_cap or output_cap > price.max_output_tokens:
        raise OrchestratorError(stage, "Provider input allowance exceeded", code="prompt_too_large")
    amount = price.cents(input_cap, output_cap)
    turn = int(stage.rsplit("_", 1)[1]) if stage.startswith("speech_turn_") else None
    now = utcnow()
    async with checkpoints.sessions.begin() as session:
        # Acquire the SQLite writer before all allowance reads and keep it until insertion.
        await checkpoints._update(session, activity="retrying" if retrying else ("generating" if turn else "evaluating"),
                                  active_turn=turn, active_speaker=TURN_ORDER[turn - 1][1].value if turn else None,
                                  current_round=ROUND_NUMBER[TURN_ORDER[turn - 1][0]] if turn else 3)
        debate = await session.get(Debate, checkpoints.debate_id)
        attempt = await session.get(ExecutionAttempt, checkpoints.run_id)
        if attempt is None or attempt.status != "active":
            raise OrchestratorError(stage, "Execution no longer owns this debate", code="stale_run")
        if debate.total_calls >= 30:
            raise OrchestratorError(stage, "Debate call allowance exhausted", code="debate_call_limit")
        stage_calls = await session.scalar(select(func.count()).select_from(ProviderCallReservation).where(
            ProviderCallReservation.run_id == checkpoints.run_id, ProviderCallReservation.stage == stage))
        if stage_calls >= 3:
            raise OrchestratorError(stage, "Stage call allowance exhausted", code="stage_call_limit")
        await check_allowance(session, settings, checkpoints.model_id, amount, stage, now)
        await session.execute(update(ProviderCallReservation).where(
            ProviderCallReservation.run_id == checkpoints.run_id, ProviderCallReservation.stage == stage,
            ProviderCallReservation.dispatch_state == "dispatched").values(dispatch_state="failed"))
        row = ProviderCallReservation(debate_id=checkpoints.debate_id, run_id=checkpoints.run_id, stage=stage,
            reserved_at=now, created_at=now, model_id=price.model_id, price_version=price.version,
            input_token_cap=input_cap, output_token_cap=output_cap, reserved_max_cost_cents=amount)
        session.add(row)
        debate.total_calls += 1
        attempt.call_count += 1
        await session.flush()
        return row.id


async def dispatch_reserved(checkpoints, provider, stage, retrying, **kwargs):
    from app.engine.budget import reserved_dispatch

    reservation_id = await reserve(checkpoints, stage, retrying, **kwargs)

    async def invoke():
        with reserved_dispatch(checkpoints.model_id, **kwargs):
            return await provider.complete(**kwargs)

    async def start():
        async with checkpoints.sessions.begin() as session:
            await checkpoints._update(session)
            await session.execute(update(ProviderCallReservation).where(ProviderCallReservation.id == reservation_id)
                                  .values(dispatch_state="dispatched"))
        return asyncio.create_task(invoke())

    # Share the hide/admission lock through final ownership verification and task creation,
    # then release it before awaiting the provider so hide can cancel that child promptly.
    if checkpoints.dispatch_lock is None:
        task = await start()
    else:
        async with checkpoints.dispatch_lock:
            task = await start()
    try:
        response = await task
    except asyncio.CancelledError as error:
        try:
            await record_usage(checkpoints, reservation_id, error, state="abandoned")
        except SQLAlchemyError:
            pass
        raise
    except Exception as error:
        await record_usage(checkpoints, reservation_id, error, state="failed")
        raise
    await record_usage(checkpoints, reservation_id, response, state="dispatched")
    return response


async def record_usage(checkpoints, reservation_id, response, *, state):
    async with checkpoints.sessions.begin() as session:
        row = await session.get(ProviderCallReservation, reservation_id)
        input_tokens = getattr(response, "input_tokens", None)
        output_tokens = getattr(response, "output_tokens", None)
        for name, value in (("actual_input_tokens", input_tokens), ("actual_output_tokens", output_tokens)):
            if type(value) is int and value >= 0:
                setattr(row, name, value)
        if row.actual_input_tokens is not None and row.actual_output_tokens is not None:
            row.actual_cost_cents = price_for(row.model_id).cents(row.actual_input_tokens, row.actual_output_tokens)
        elif row.model_id == "fake":
            row.actual_cost_cents = 0
        if row.dispatch_state not in {"abandoned", "failed", "committed"}:
            row.dispatch_state = state
        breached = ((row.actual_input_tokens is not None and row.actual_input_tokens > row.input_token_cap)
                    or (row.actual_output_tokens is not None and row.actual_output_tokens > row.output_token_cap))
    if breached:
        raise OrchestratorError("accounting", "Provider token usage exceeded its reservation", code="token_accounting_unconfirmed")
