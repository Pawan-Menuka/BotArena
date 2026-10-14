from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from contextlib import closing

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db.base import Base
from app.db import models  # Register the schema before checking backup columns.

logger = logging.getLogger(__name__)
UTC = timezone.utc


def readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)


def verify(path: Path, deadline: float | None = None) -> None:
    def check():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Backup deadline exceeded")

    check()
    with closing(readonly(path)) as connection:
        connection.set_progress_handler(lambda: int(deadline is not None and time.monotonic() >= deadline), 1000)
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Backup integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Backup foreign key check failed")
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        heads = set(ScriptDirectory.from_config(config).get_heads())
        if {row[0] for row in connection.execute("SELECT version_num FROM alembic_version")} != heads:
            raise ValueError("Backup schema revision does not match this application")
        for table in Base.metadata.sorted_tables:
            check()
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')}
            if not set(table.columns.keys()) <= columns:
                raise ValueError("Backup schema is incomplete")
            # Read a representative row including large transcript/judgment fields, if present.
            connection.execute(f'SELECT * FROM "{table.name}" LIMIT 1').fetchone()
        check()


def warn_storage(database: Path, directory: Path) -> None:
    if database.stat().st_size > 500 * 1024**2:
        logger.warning("Live database exceeds 500 MB")
    wal = Path(str(database) + "-wal")
    if wal.exists() and wal.stat().st_size > 100 * 1024**2:
        logger.warning("Database WAL exceeds 100 MB")
    free = shutil.disk_usage(directory).free
    if free < max(1024**3, database.stat().st_size * 2):
        logger.warning("Backup disk space is low")


def backup_time(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.name.split("-")[1], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    except (ValueError, IndexError):
        return None


class BackupService:
    def __init__(self, database: Path, directory: Path | None = None, *, timeout: float = 60):
        self.database = Path(database)
        self.directory = Path(directory) if directory is not None else self.database.parent / "backups"
        self.timeout = timeout
        self.lock = threading.Lock()
        self.scheduler = AsyncIOScheduler(timezone=UTC)
        self.worker: asyncio.Task | None = None
        self.catchup: asyncio.Task | None = None

    def run(self, *, now: datetime | None = None) -> Path | None:
        if not self.lock.acquire(blocking=False):
            return None
        temporary = None
        try:
            now = now or datetime.now(UTC)
            deadline = time.monotonic() + self.timeout
            self.directory.mkdir(parents=True, exist_ok=True)
            warn_storage(self.database, self.directory)
            temporary = self.directory / f".backup-{uuid4().hex}.tmp"
            with closing(readonly(self.database)) as source, closing(sqlite3.connect(temporary)) as destination:
                def progress(_status, _remaining, _total):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Backup deadline exceeded")
                source.backup(destination, pages=128, progress=progress, sleep=0.01)
                destination.execute("PRAGMA journal_mode=DELETE")
            verify(temporary, deadline)
            with temporary.open("r+b") as handle:
                os.fsync(handle.fileno())
            final = self.directory / f"botarena-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}.sqlite3"
            os.replace(temporary, final)
            temporary = None
            self.prune(now, deadline, final)
            return final
        except Exception as error:
            logger.warning("Database backup failed (%s); previous verified backups retained", type(error).__name__)
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove incomplete backup")
            self.lock.release()

    def prune(self, now: datetime, deadline: float, newest: Path) -> None:
        for path in self.directory.glob("botarena-*.sqlite3"):
            stamp = backup_time(path)
            if path == newest or stamp is None or stamp >= now - timedelta(days=7):
                continue
            try:
                verify(path, deadline)
                path.unlink()
            except Exception:
                logger.warning("Backup cleanup skipped a copy; newest verified backup retained")

    def needs_catchup(self, now: datetime) -> bool:
        deadline = time.monotonic() + self.timeout
        due = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now < due:
            due -= timedelta(days=1)
        for path in self.directory.glob("botarena-*.sqlite3"):
            if time.monotonic() >= deadline:
                break
            stamp = backup_time(path)
            if stamp is not None and due <= stamp <= now:
                try:
                    verify(path, deadline)
                    return False
                except Exception:
                    pass
        return True

    async def job(self):
        if self.worker is not None and not self.worker.done():
            return
        self.worker = asyncio.create_task(asyncio.to_thread(self.run))
        # Cancelling a scheduler coroutine must not release ownership of a live backup thread.
        await asyncio.shield(self.worker)

    def start(self, idle):
        self.scheduler.add_job(self.job, "cron", hour=3, minute=0, id="nightly-backup",
                               max_instances=1, coalesce=True, misfire_grace_time=None)
        self.scheduler.start()

        async def catchup():
            try:
                if await asyncio.to_thread(self.needs_catchup, datetime.now(UTC)):
                    while not idle():
                        await asyncio.sleep(1)
                    if await asyncio.to_thread(self.needs_catchup, datetime.now(UTC)):
                        await self.job()
            except Exception:
                logger.warning("Missed-schedule backup check failed")

        self.catchup = asyncio.create_task(catchup(), name="backup-catchup")

    async def close(self):
        self.scheduler.pause()
        if self.catchup is not None:
            self.catchup.cancel()
            await asyncio.gather(self.catchup, return_exceptions=True)
        if self.worker is not None:
            await asyncio.shield(self.worker)
        self.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)


def main():
    from app.config import Settings
    parser = argparse.ArgumentParser(description="Force a verified online backup; never invokes a provider.")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = BackupService(Settings().database_path, args.directory).run()
    if result is None:
        raise SystemExit(1)
    print(result)


if __name__ == "__main__":
    main()
