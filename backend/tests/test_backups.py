from __future__ import annotations

import asyncio
import errno
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing

import httpx
import pytest
from sqlalchemy import update

from app.config import Settings
from app.db.models import Debate, ExecutionAttempt, ProviderCallReservation, CreationQuotaRecord
from app.ops.backup import BackupService, backup_time, verify, warn_storage
from app.ops.prune_old_debates import prune
from app.ops.restore import restore, pause_path
from app.main import create_app
from tests.test_api import running_app, create, terminal, FAST

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


@pytest.fixture
async def completed(tmp_path):
    path = tmp_path / "live.db"
    async with running_app(path) as (app, client):
        data, headers = await create(client)
        await client.post(f"/api/debates/{data['id']}/run", headers=headers)
        result = await terminal(client, data["id"])
        assert result["status"] == "completed"
        yield path, app, client, result


async def test_live_wal_backup_restore_and_public_get(completed, tmp_path):
    path, app, client, original = completed
    assert Path(str(path) + "-wal").exists()
    service = BackupService(path, tmp_path / "copies")
    copy = await asyncio.to_thread(service.run, now=NOW)
    assert copy is not None
    assert backup_time(copy) == NOW
    verify(copy)
    isolated = tmp_path / "restored.db"
    restore(copy, isolated)
    assert pause_path(isolated).exists()
    settings = Settings(database_path=isolated, input_accounting_confirmed=True)
    with pytest.raises(ValueError, match="post-restore"):
        settings.validate_execution_budget("openai/gpt-oss-20b")
    settings.validate_execution_budget("fake")
    calls = []
    restored = create_app(settings, provider_factory=lambda: calls.append(1), retry_policy=FAST)
    async with restored.router.lifespan_context(restored):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restored), base_url="http://restore") as reader:
            response = await reader.get(f"/api/debates/{original['id']}")
            assert response.status_code == 200
            data = response.json()
            assert data["messages"] == original["messages"]
            assert data["judgment"] == original["judgment"]
            assert data["status"] == "completed"
            assert "controls" not in data
            assert not calls
    with pytest.raises(FileExistsError):
        restore(copy, isolated)
    with closing(sqlite3.connect(isolated)) as connection:
        assert connection.execute("SELECT count(*) FROM provider_call_reservations").fetchone()[0] == 7


@pytest.mark.parametrize("failure", ["disk", "verify", "rename", "deadline"])
async def test_failed_backup_retains_good_copy_and_cleans_temp(completed, tmp_path, failure, monkeypatch, caplog):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    good = service.run(now=NOW - timedelta(days=10))
    before = good.read_bytes()
    if failure == "disk":
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
    elif failure == "verify":
        monkeypatch.setattr("app.ops.backup.verify", lambda *a: (_ for _ in ()).throw(ValueError("bad")))
    elif failure == "rename":
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("rename")))
    else:
        service.timeout = 0
    assert service.run(now=NOW) is None
    assert good.read_bytes() == before
    assert not list(service.directory.glob("*.tmp"))
    assert "backup failed" in caplog.text


async def test_pruning_only_verified_expired_copies_and_cleanup_failure(completed, tmp_path, monkeypatch, caplog):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    old = service.run(now=NOW - timedelta(days=10))
    corrupt = service.directory / "botarena-20260901T120000000000Z-invalid.sqlite3"
    corrupt.write_text("broken")
    recent = service.run(now=NOW - timedelta(days=2))
    assert not old.exists()
    assert corrupt.exists()
    original_unlink = Path.unlink
    def fail_one(self, *a, **kw):
        if self == recent:
            raise OSError(errno.EACCES, "denied")
        return original_unlink(self, *a, **kw)
    monkeypatch.setattr(Path, "unlink", fail_one)
    newest = service.run(now=NOW + timedelta(days=8))
    assert newest.exists() and recent.exists() and corrupt.exists()
    assert "cleanup skipped" in caplog.text


async def test_no_overlap_thread_deadline_ownership_and_event_loop(completed, tmp_path, monkeypatch):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    entered, release = threading.Event(), threading.Event()
    real_verify = verify
    def slow_verify(*args):
        entered.set()
        assert release.wait(5)
        real_verify(*args)
    monkeypatch.setattr("app.ops.backup.verify", slow_verify)
    task = asyncio.create_task(service.job())
    assert await asyncio.to_thread(entered.wait, 5)
    assert await asyncio.to_thread(service.run) is None
    await service.job()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.worker.done()
    assert await asyncio.to_thread(service.run) is None
    release.set()
    await service.worker
    assert len(list(service.directory.glob("*.sqlite3"))) == 1


async def test_catchup_schedule_idle_and_single_attempt(completed, tmp_path, monkeypatch):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    assert service.needs_catchup(NOW)
    service.run(now=NOW)
    assert not service.needs_catchup(NOW)
    assert service.needs_catchup(NOW + timedelta(days=1))
    assert not service.needs_catchup(NOW + timedelta(hours=14))  # Before next 03:00 UTC.
    monkeypatch.setattr(service, "needs_catchup", lambda now: True)
    attempts = []
    monkeypatch.setattr(service, "run", lambda: attempts.append(1))
    idle = False
    service.start(lambda: idle)
    try:
        job = service.scheduler.get_job("nightly-backup")
        assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time is None
        assert str(job.trigger.timezone) == "UTC"
        await asyncio.sleep(0.02)
        assert attempts == []
        idle = True
        async with asyncio.timeout(3):
            await service.catchup
        assert attempts == [1]
    finally:
        await service.close()


async def test_corrupt_recent_backup_requires_catchup(completed, tmp_path):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    copy = service.run(now=NOW)
    copy.write_bytes(b"broken")
    assert service.needs_catchup(NOW)
    with pytest.raises(sqlite3.DatabaseError):
        verify(copy)
    target = tmp_path / "bad-restore.db"
    with pytest.raises(sqlite3.DatabaseError):
        restore(copy, target)
    assert not target.exists()


async def test_schema_and_foreign_key_checks(completed, tmp_path):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    copy = service.run(now=NOW)
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute("UPDATE alembic_version SET version_num='unknown'")
    with pytest.raises(ValueError, match="revision"):
        verify(copy)
    copy = service.run(now=NOW)
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute("UPDATE debate_messages SET debate_id='missing'")
    with pytest.raises(ValueError, match="foreign key"):
        verify(copy)
    copy = service.run(now=NOW)
    with closing(sqlite3.connect(copy)) as connection, connection:
        connection.execute("DROP TABLE judgments")
    with pytest.raises(ValueError, match="incomplete"):
        verify(copy)


def test_storage_warning_thresholds(tmp_path, monkeypatch, caplog):
    path = tmp_path / "large.db"
    with path.open("wb") as handle:
        handle.truncate(501 * 1024**2)
    with Path(str(path) + "-wal").open("wb") as handle:
        handle.truncate(101 * 1024**2)
    from collections import namedtuple
    usage = namedtuple("Usage", "total used free")(100, 99, 1)
    monkeypatch.setattr("app.ops.backup.shutil.disk_usage", lambda _: usage)
    warn_storage(path, tmp_path)
    assert "500 MB" in caplog.text and "100 MB" in caplog.text and "space is low" in caplog.text


async def test_manual_prune_preview_and_confirmation_atomicity(completed):
    path, app, client, data = completed
    old = NOW - timedelta(days=30)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).values(created_at=old, updated_at=old))
        await session.execute(update(ProviderCallReservation).values(reserved_at=old, created_at=old))
    preview = prune(path, 7, now=NOW)
    assert preview["rows"]["debates"] == [data["id"]]
    assert len(preview["rows"]["debate_messages"]) == 6
    assert len(preview["rows"]["provider_call_reservations"]) == 7
    assert len(preview["rows"]["judgments"]) == 1
    assert (await client.get(f"/api/debates/{data['id']}")).status_code == 200
    with pytest.raises(ValueError, match="changed"):
        prune(path, 7, confirmation="wrong", now=NOW)
    assert (await client.get(f"/api/debates/{data['id']}")).status_code == 200
    result = prune(path, 7, confirmation=preview["confirmation"], now=NOW)
    assert result == preview
    assert (await client.get(f"/api/debates/{data['id']}")).status_code == 404
    with closing(sqlite3.connect(path)) as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT count(*) FROM execution_attempts").fetchone()[0] == 0


@pytest.mark.parametrize("protection", ["running", "judging", "attempt", "quota", "reservation", "breach", "updated"])
async def test_retention_preserves_active_recent_evidence_and_permanent_breaches(completed, protection):
    path, app, client, data = completed
    old = NOW - timedelta(days=30)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).values(created_at=old, updated_at=old))
        await session.execute(update(ProviderCallReservation).values(reserved_at=old, created_at=old))
        if protection in {"running", "judging"}:
            await session.execute(update(Debate).values(status=protection))
        elif protection == "attempt":
            await session.execute(update(ExecutionAttempt).values(status="active"))
        elif protection == "quota":
            session.add(CreationQuotaRecord(debate_id=data["id"], created_at=NOW, key_version=1))
        elif protection == "reservation":
            await session.execute(update(ProviderCallReservation).values(reserved_at=NOW))
        elif protection == "breach":
            await session.execute(update(ProviderCallReservation).values(actual_input_tokens=999999))
        else:
            await session.execute(update(Debate).values(updated_at=NOW))
    assert prune(path, 7, now=NOW)["rows"]["debates"] == []


async def test_prune_changed_rows_refuses_reviewed_plan(completed):
    path, app, client, data = completed
    preview = prune(path, 1, now=NOW)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).values(created_at=NOW - timedelta(days=40), updated_at=NOW - timedelta(days=40)))
        await session.execute(update(ProviderCallReservation).values(reserved_at=NOW - timedelta(days=40)))
    with pytest.raises(ValueError, match="changed"):
        prune(path, 1, confirmation=preview["confirmation"], now=NOW)
    with pytest.raises(ValueError, match="at least"):
        prune(path, 0, now=NOW)


@pytest.mark.parametrize("failure", ["disk", "timeout"])
async def test_failure_during_online_copy_cleans_partial_file(completed, tmp_path, monkeypatch, failure):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    good = service.run(now=NOW - timedelta(days=10))
    from app.ops import backup
    original_readonly = backup.readonly
    class Source:
        def __init__(self):
            self.connection = original_readonly(path)
        def close(self):
            self.connection.close()
        def backup(self, destination, **kwargs):
            destination.execute("CREATE TABLE partial (id INTEGER)")
            destination.commit()
            if failure == "disk":
                raise sqlite3.OperationalError("database or disk is full")
            monkeypatch.setattr(backup.time, "monotonic", lambda: float("inf"))
            kwargs["progress"](0, 1, 2)
    monkeypatch.setattr(backup, "readonly", lambda _: Source())
    assert service.run(now=NOW) is None
    assert good.exists()
    assert not list(service.directory.glob("*.tmp"))


async def test_shutdown_waits_for_thread_and_no_replayed_catchup(completed, tmp_path, monkeypatch):
    path, *_ = completed
    service = BackupService(path, tmp_path / "copies")
    entered, release = threading.Event(), threading.Event()
    def run():
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(service, "needs_catchup", lambda now: False)
    monkeypatch.setattr(service, "run", run)
    service.start(lambda: True)
    await service.catchup
    job = asyncio.create_task(service.job())
    assert await asyncio.to_thread(entered.wait, 5)
    close = asyncio.create_task(service.close())
    await asyncio.sleep(0.02)
    assert not close.done()
    release.set()
    await close
    await job


async def test_retention_rollback_if_child_delete_fails(completed):
    path, app, client, data = completed
    old = NOW - timedelta(days=30)
    async with app.state.sessions.begin() as session:
        await session.execute(update(Debate).values(created_at=old, updated_at=old))
        await session.execute(update(ProviderCallReservation).values(reserved_at=old))
    preview = prune(path, 7, now=NOW)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TRIGGER block_delete BEFORE DELETE ON debate_messages BEGIN SELECT RAISE(ABORT, 'test'); END")
    with pytest.raises(sqlite3.IntegrityError):
        prune(path, 7, confirmation=preview["confirmation"], now=NOW)
    assert prune(path, 7, now=NOW) == preview
