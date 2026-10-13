from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path


INPUT_TOKEN_CAP = 8192
REASONING_TOKEN_HEADROOM = 1500
PRICES = json.loads((Path(__file__).parents[1] / "data" / "pricing.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Price:
    model_id: str
    version: str
    input_rate: Decimal
    output_rate: Decimal
    context_window: int
    max_output_tokens: int

    def cents(self, input_tokens: int, output_tokens: int) -> int:
        amount = (input_tokens * self.input_rate + output_tokens * self.output_rate) / 10000
        return int(amount.to_integral_value(rounding=ROUND_CEILING))


def price_for(model_id: str) -> Price:
    if model_id == "fake":
        return Price("fake", "fake-v1", Decimal(0), Decimal(0), 131072, 65536)
    data = PRICES["models"].get(model_id)
    if data is None:
        raise ValueError("Model pricing is unconfirmed")
    return Price(model_id, PRICES["price_version"], Decimal(data["input_usd_per_million"]),
                 Decimal(data["output_usd_per_million"]), data["context_window"], data["max_output_tokens"])


def request_payload(system, user, max_output_tokens, json_schema):
    value = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
             "max_completion_tokens": max_output_tokens + REASONING_TOKEN_HEADROOM, "reasoning_effort": "low"}
    if json_schema is not None:
        value["response_format"] = {"type": "json_schema", "json_schema": {"name": "judgment", "strict": True, "schema": json_schema}}
    return value


def request_bytes(system, user, max_output_tokens, json_schema) -> bytes:
    return json.dumps(request_payload(system, user, max_output_tokens, json_schema), ensure_ascii=False, sort_keys=True).encode("utf-8")


def input_bound(system, user, max_output_tokens, json_schema) -> int:
    # UTF-8 bytes bound byte-level BPE content; reserve additional space for chat/schema framing.
    # Shared paid work also requires explicit verification of this bound against provider usage.
    return len(request_bytes(system, user, max_output_tokens, json_schema)) + 1024


@dataclass
class DispatchPermit:
    model_id: str
    fingerprint: bytes
    used: bool = False


_permit: ContextVar[DispatchPermit | None] = ContextVar("provider_reservation", default=None)


@contextmanager
def reserved_dispatch(model_id, system, user, max_output_tokens, json_schema):
    permit = DispatchPermit(model_id, hashlib.sha256(request_bytes(system, user, max_output_tokens, json_schema)).digest())
    token = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(token)


def authorize_dispatch(model_id, system, user, max_output_tokens, json_schema) -> None:
    permit = _permit.get()
    fingerprint = hashlib.sha256(request_bytes(system, user, max_output_tokens, json_schema)).digest()
    if permit is None or permit.used or permit.model_id != model_id or permit.fingerprint != fingerprint:
        raise ValueError("A matching durable reservation is required")
    permit.used = True
