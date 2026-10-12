from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


async def upgrade_database(engine) -> None:
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))

    def upgrade(connection):
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

    async with engine.begin() as connection:
        await connection.run_sync(upgrade)
