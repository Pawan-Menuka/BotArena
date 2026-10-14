from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from contextlib import closing

TABLE_KEYS = {"debates": "id", "debate_messages": "id", "judgments": "id",
              "execution_attempts": "run_id", "creation_quota_records": "id", "provider_call_reservations": "id"}


def plan(connection, days: int, now: datetime):
    if days < 1:
        raise ValueError("Retention must be at least one day")
    cutoff = (now - timedelta(days=days)).replace(tzinfo=None).isoformat(" ")
    window = (now - timedelta(hours=24)).replace(tzinfo=None).isoformat(" ")
    ids = [row[0] for row in connection.execute("""
        SELECT id FROM debates d WHERE created_at < ? AND updated_at < ?
        AND status NOT IN ('running','judging') AND active_run_id IS NULL
        AND NOT EXISTS (SELECT 1 FROM execution_attempts a WHERE a.debate_id=d.id AND a.status='active')
        AND NOT EXISTS (SELECT 1 FROM creation_quota_records q WHERE q.debate_id=d.id AND q.created_at >= ?)
        AND NOT EXISTS (SELECT 1 FROM provider_call_reservations r WHERE r.debate_id=d.id AND
          (r.reserved_at >= ? OR r.actual_input_tokens > r.input_token_cap OR r.actual_output_tokens > r.output_token_cap))
        ORDER BY id
    """, (cutoff, cutoff, window, window))]
    rows = {table: [] for table in TABLE_KEYS}
    rows["debates"] = ids
    # Avoid SQLite's variable limit while listing every affected child row.
    for debate_id in ids:
        for table, key in TABLE_KEYS.items():
            if table != "debates":
                rows[table].extend(row[0] for row in connection.execute(
                    f'SELECT "{key}" FROM "{table}" WHERE debate_id=?', (debate_id,)))
    rows = {table: sorted(values) for table, values in rows.items()}
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return {"rows": rows, "confirmation": digest}


def prune(database: Path, days: int, *, confirmation: str | None = None, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE" if confirmation is not None else "BEGIN")
        result = plan(connection, days, now)
        if confirmation is not None:
            if confirmation != result["confirmation"]:
                raise ValueError("Prune rows changed; run --dry-run and review the new plan")
            for table in ("provider_call_reservations", "creation_quota_records", "debate_messages",
                          "judgments", "execution_attempts", "debates"):
                key = TABLE_KEYS[table]
                connection.executemany(f'DELETE FROM "{table}" WHERE "{key}"=?',
                                       ((value,) for value in result["rows"][table]))
        return result


def main():
    from app.config import Settings
    parser = argparse.ArgumentParser(description="Manual retention. Preview exact row IDs first; no automatic deletion.")
    parser.add_argument("--older-than-days", type=int, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--confirm-plan", help="Apply the SHA-256 plan reviewed in a previous dry run")
    args = parser.parse_args()
    result = prune(Settings().database_path, args.older_than_days, confirmation=args.confirm_plan)
    print(json.dumps({"mode": "applied" if args.confirm_plan else "dry-run", **result}, indent=2))


if __name__ == "__main__":
    main()
