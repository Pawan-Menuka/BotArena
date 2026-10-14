from __future__ import annotations

import asyncio

from alembic import context

from app.config import Settings
from app.db.base import Base, create_database
from app.db import models

target_metadata = Base.metadata


def configure(connection):
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine, _ = create_database(Settings().database_path)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(configure)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    from sqlalchemy import URL

    context.configure(url=URL.create("sqlite", database=str(Settings().database_path)),
                      target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        configure(connection)
    else:
        asyncio.run(online())
