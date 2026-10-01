"""Backup-first migration preserving historical identities and dependent labels."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def migrate_pilot(database: str):
    path = Path(database)
    if not path.is_file():
        return
    from code_review_agent.operations import backup, file_lock

    with file_lock(str(path) + ".pilot-migration.lock"):
        conn = sqlite3.connect(path)
        try:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='schema_versions'"
            ).fetchone():
                version = conn.execute(
                    "SELECT version FROM schema_versions WHERE component='pilot'"
                ).fetchone()
                if version and version[0] > 2:
                    raise ValueError("Unsupported future pilot schema")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='pilot_runs'"
            ).fetchone():
                return
            columns = {r[1] for r in conn.execute("PRAGMA table_info(pilot_runs)")}
            if "reviewer_profile_id" in columns:
                return
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='webhook_jobs'"
            ).fetchone():
                if conn.execute(
                    "SELECT 1 FROM webhook_jobs WHERE status='PROCESSING' LIMIT 1"
                ).fetchone():
                    raise RuntimeError(
                        "Stop workers and reconcile active leases before pilot migration"
                    )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup(path, path.parent / "backups" / f"pre-pilot-v2-{stamp}.db")
            count = conn.execute("SELECT COUNT(*) FROM pilot_runs").fetchone()[0]
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE pilot_runs_next (
                run_id TEXT PRIMARY KEY, repository TEXT NOT NULL, pr TEXT NOT NULL,
                head TEXT NOT NULL, mode TEXT NOT NULL, complete INTEGER NOT NULL,
                snapshot TEXT NOT NULL, created_at REAL NOT NULL,
                reviewer_profile_id TEXT NOT NULL DEFAULT 'LEGACY', input_snapshot_hash TEXT,
                UNIQUE(repository,pr,head,mode,reviewer_profile_id))""")
            conn.execute(
                "INSERT INTO pilot_runs_next SELECT *, 'LEGACY', NULL FROM pilot_runs"
            )
            conn.execute("DROP TABLE pilot_runs")
            conn.execute("ALTER TABLE pilot_runs_next RENAME TO pilot_runs")
            conn.execute(
                """INSERT INTO pilot_control_events SELECT repository,'PAUSED','system:pilot-v2-migration',
                'Versioned evidence requires a new approval',? FROM pilot_controls WHERE mode='ADVISORY'""",
                (datetime.now(timezone.utc).timestamp(),),
            )
            conn.execute(
                "UPDATE pilot_controls SET mode='PAUSED',actor='system:pilot-v2-migration',reason='Versioned evidence requires a new approval' WHERE mode='ADVISORY'"
            )
            conn.execute("UPDATE schema_versions SET version=2 WHERE component='pilot'")
            if (
                conn.execute("SELECT COUNT(*) FROM pilot_runs").fetchone()[0] != count
                or conn.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise RuntimeError("Pilot migration integrity verification failed")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
