from __future__ import annotations

import math
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.api.errors import APIError
from app.db.models import CreationQuotaRecord, ExecutionAttempt, utcnow


def retry_after(oldest: datetime, now: datetime) -> int:
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return max(1, math.ceil((oldest + timedelta(hours=24) - now).total_seconds()))


async def check_creation(session, settings, identity, now: datetime) -> None:
    window = CreationQuotaRecord.created_at > now - timedelta(hours=24)
    for code, limit, conditions in (
        ("creation_quota_per_ip", settings.create_quota_per_ip_24h, [window, CreationQuotaRecord.ip_bucket_hash == identity.bucket_hash]),
        ("global_creation_quota", settings.global_create_quota_24h, [window]),
    ):
        count, oldest = (await session.execute(select(func.count(), func.min(CreationQuotaRecord.created_at)).where(*conditions))).one()
        if count >= limit:
            raise APIError(429, code, "Daily battle creation allowance reached. Try again later.", retry_after=retry_after(oldest, now))


async def clear_old_identities(sessions, *, now: datetime | None = None) -> None:
    cutoff = (now or utcnow()) - timedelta(days=7)
    async with sessions.begin() as session:
        for model in (CreationQuotaRecord, ExecutionAttempt):
            await session.execute(update(model).where(model.created_at < cutoff, model.ip_bucket_hash.is_not(None))
                                  .values(ip_bucket_hash=None))


async def weekly_identity_cleanup(sessions) -> None:
    while True:
        await asyncio.sleep(7 * 24 * 60 * 60)
        try:
            await clear_old_identities(sessions)
        except SQLAlchemyError:
            logging.getLogger(__name__).warning("Could not clear expired client associations")
