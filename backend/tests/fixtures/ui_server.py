from __future__ import annotations

import asyncio

from app.engine.provider import FakeProvider
from app.engine.retry import RetryPolicy
from app.main import create_app

attempts = 0


async def no_sleep(_delay):
    pass


class SlowProvider(FakeProvider):
    async def complete(self, **kwargs):
        await asyncio.sleep(1.5)
        return await super().complete(**kwargs)


def provider_factory():
    global attempts
    attempts += 1
    if attempts == 2:
        return SlowProvider(speeches=["Pro opening saved.", "Con opening saved.", "Pro rebuttal saved.", "", "", ""])
    if attempts == 3:
        return SlowProvider(speeches=["Con rebuttal resumed.", "Pro closing resumed.", "Con closing resumed."])
    return SlowProvider()


app = create_app(provider_factory=provider_factory, retry_policy=RetryPolicy(sleep=no_sleep))
