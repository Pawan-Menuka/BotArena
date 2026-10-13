from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path
from contextlib import closing

from app.ops.backup import readonly, verify


def pause_path(database: Path) -> Path:
    return Path(str(database) + ".restore-pause")


def restore(backup: Path, target: Path, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    verify(backup, deadline)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation refuses to replace any live or previously restored database.
    with target.open("xb"):
        pass
    marker = pause_path(target)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            handle.write("Paid execution paused after restore. Reconcile provider usage and the cost ledger, "
                         "or wait a full 24 hours without paid calls before removing this marker.\n")
            handle.flush()
            os.fsync(handle.fileno())
        with closing(readonly(backup)) as source, closing(sqlite3.connect(target)) as destination:
            def progress(_status, _remaining, _total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Restore deadline exceeded")
            source.backup(destination, pages=128, progress=progress, sleep=0.01)
        verify(target, deadline)
    except Exception:
        target.unlink(missing_ok=True)
        # Keep the pause marker on failure so retries cannot accidentally enable paid work.
        raise


def main():
    parser = argparse.ArgumentParser(description="Restore into a NEW isolated database with paid execution paused.")
    parser.add_argument("backup", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    restore(args.backup, args.target)
    print("Restore verified. Use DATABASE_PATH for isolated GET checks. Paid execution remains paused.")


if __name__ == "__main__":
    main()
