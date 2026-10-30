"""SQLite backup, offline restore, migration locking, and single-host ownership."""

import argparse
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def backup(database, destination):
    database, destination = Path(database).resolve(), Path(destination).resolve()
    if not database.is_file() or destination.exists() or database == destination:
        raise ValueError("Backup requires an existing database and a new destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    if temporary.exists():
        raise ValueError("Incomplete backup already exists; inspect it before retrying")
    try:
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        output = sqlite3.connect(temporary)
        try:
            source.backup(output, pages=256)
            if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
        finally:
            source.close()
            output.close()
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        temporary.replace(destination)
    except Exception:
        # Keep failed artifacts for diagnosis; they cannot be confused with a finished backup.
        raise
    return str(destination)


def migrate(database):
    from code_review_agent.calibration.persistence import FindingOutcomeStore
    from code_review_agent.pilot import PilotStore
    from code_review_agent.webhook_queue import WebhookJobQueue

    path = Path(database).resolve()
    with file_lock(str(path) + ".migration.lock"):
        backup_path = None
        if path.exists():
            with closing(sqlite3.connect(path)) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='schema_versions'"
                ).fetchone()
                version = (
                    conn.execute(
                        "SELECT version FROM schema_versions WHERE component='calibration'"
                    ).fetchone()
                    if table
                    else None
                )
            if version and version[0] > FindingOutcomeStore.VERSION:
                raise RuntimeError(
                    "Refusing incompatible schema; restore the matching backup"
                )
            queue_version = conn_version(path, "queue")
            if queue_version and queue_version > 2:
                raise RuntimeError("Refusing incompatible queue schema")
            if (
                not version
                or version[0] < FindingOutcomeStore.VERSION
                or (queue_version or 0) < 2
                or (conn_version(path, "pilot") or 0) < 2
            ):
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                backup_path = backup(
                    path, path.parent / "backups" / f"pre-migration-{stamp}.db"
                )
        store = FindingOutcomeStore(str(path))
        queue = WebhookJobQueue(str(path), _migration_lock_held=True)
        PilotStore(str(path))
        conn = store._get_connection()
        counts = dict(
            conn.execute(
                "SELECT publication_status,COUNT(*) FROM finding_outcomes GROUP BY publication_status"
            ).fetchall()
        )
        store.close()
        queue._get_connection().close()
        return {
            "schema": FindingOutcomeStore.VERSION,
            "queue_schema": 2,
            "pilot_schema": 2,
            "backup": backup_path,
            "records": counts,
            "unresolved": counts.get("LEGACY_UNVERIFIED", 0),
        }


def conn_version(database, component):
    with closing(sqlite3.connect(database)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_versions'"
        ).fetchone():
            return None
        row = conn.execute(
            "SELECT version FROM schema_versions WHERE component=?", (component,)
        ).fetchone()
        return row[0] if row else None


def restore(database, source):
    path = Path(database).resolve()
    with (
        file_lock(str(path) + ".application.lock"),
        file_lock(str(path) + ".migration.lock"),
    ):
        staged = path.with_suffix(".restore.db")
        backup(source, staged)
        if path.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup(path, path.parent / "backups" / f"pre-restore-{stamp}.db")
            with closing(sqlite3.connect(path)) as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0:
                    raise RuntimeError("Restore refused: database is still in use")
        staged.replace(path)
        return {"restored": str(path), "source": str(source)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore", "migrate"))
    parser.add_argument(
        "--database", default=os.getenv("QUEUE_DB_PATH", "/data/review.db")
    )
    parser.add_argument("--file")
    args = parser.parse_args()
    if args.operation != "migrate" and not args.file:
        parser.error("--file is required for backup and restore")
    result = (
        migrate(args.database)
        if args.operation == "migrate"
        else (
            backup(args.database, args.file)
            if args.operation == "backup"
            else restore(args.database, args.file)
        )
    )
    print(json.dumps(result))


def check_db_health(database):
    path = Path(database).resolve()
    if not path.is_file():
        return {"status": "missing", "error": f"Database file {path} does not exist"}
    with closing(sqlite3.connect(path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()
        is_ok = bool(integrity and integrity[0] == "ok")
        return {
            "status": "healthy" if is_ok else "corrupt",
            "integrity": integrity[0] if integrity else "unknown",
            "journal_mode": journal_mode[0] if journal_mode else "unknown",
            "busy_timeout_ms": busy_timeout[0] if busy_timeout else 0,
        }


if __name__ == "__main__":
    main()
