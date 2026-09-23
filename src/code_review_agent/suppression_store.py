"""
Persistent Finding Suppression Store for AI Code Review Agent.
Stores and manages developer finding suppressions by repository and deterministic fingerprint
using SQLite WAL mode with ACID transactions.
"""

import os
import re
import time
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from code_review_agent.config import logger


class SuppressionStore:
    """
    Thread-safe persistent store for suppressed review findings.
    Shares the durable database file specified by QUEUE_DB_PATH.
    """

    def __init__(self, db_path: Optional[str] = None):
        target = db_path or os.getenv("QUEUE_DB_PATH") or "webhook_jobs.db"
        self.db_path = Path(target).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection with busy timeout and WAL journal mode."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
        return conn

    DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

    def _init_db(self):
        """Initialize suppression schema, TTL expiration, and audit indices."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS finding_suppressions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    author TEXT DEFAULT '',
                    suppressed_at REAL NOT NULL,
                    pr_id TEXT DEFAULT '',
                    expires_at REAL,
                    severity TEXT DEFAULT '',
                    critical_override INTEGER DEFAULT 0,
                    UNIQUE(repo_id, fingerprint)
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_suppression_lookup
                ON finding_suppressions(repo_id, fingerprint);
            """)

            # Migration: ensure new columns exist in older DB files
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(finding_suppressions);")
            columns = {row[1] for row in cursor.fetchall()}
            if "expires_at" not in columns:
                conn.execute("ALTER TABLE finding_suppressions ADD COLUMN expires_at REAL;")
            if "severity" not in columns:
                conn.execute("ALTER TABLE finding_suppressions ADD COLUMN severity TEXT DEFAULT '';")
            if "critical_override" not in columns:
                conn.execute("ALTER TABLE finding_suppressions ADD COLUMN critical_override INTEGER DEFAULT 0;")

            # Full audit log table for suppression lifecycle governance
            conn.execute("""
                CREATE TABLE IF NOT EXISTS suppression_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    severity TEXT DEFAULT '',
                    critical_override INTEGER DEFAULT 0,
                    pr_id TEXT DEFAULT '',
                    timestamp REAL NOT NULL,
                    details_json TEXT DEFAULT '{}'
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_audit_lookup
                ON suppression_audit_log(repo_id, fingerprint, timestamp);
            """)
            conn.commit()

    @staticmethod
    def normalize_repo_id(repo_id: str) -> str:
        """Normalize repository identifier into standard 'owner/repo' lowercase format."""
        if not repo_id:
            return "default"
        clean = repo_id.strip().lower()
        # Extract owner/repo from URL or git remote string
        match = re.search(r"(?:https?://[^/]+/|git@[^:]+:)?([^/]+)/([^/#?]+)", clean)
        if match:
            owner = match.group(1)
            repo = match.group(2)
            if repo.endswith(".git"):
                repo = repo[:-4]
            return f"{owner}/{repo}"
        return clean

    def suppress(
        self,
        repo_id: str,
        fingerprint: str,
        reason: str = "",
        author: str = "",
        pr_id: str = "",
        ttl_days: Optional[int] = 30,
        ttl_seconds: Optional[float] = None,
        expires_at: Optional[float] = None,
        severity: Optional[str] = None,
        critical_override: bool = False
    ) -> bool:
        """
        Record a finding suppression with TTL expiration, severity ceilings, and audit trail.
        """
        norm_repo = self.normalize_repo_id(repo_id)
        norm_fp = fingerprint.strip().lower()
        now = time.time()
        clean_reason = reason.strip()
        sev_clean = (severity or "").strip().upper()

        # Compute TTL expiration
        if expires_at is not None:
            exp_time = expires_at
        elif ttl_seconds is not None:
            exp_time = now + ttl_seconds
        elif ttl_days is not None and ttl_days > 0:
            exp_time = now + (ttl_days * 86400)
        else:
            exp_time = None

        # Determine if explicit critical override was granted
        has_override = critical_override
        if not has_override and clean_reason:
            if re.search(r"\b(override|accepted risk|approved|exception|waive|waiver)\b", clean_reason, re.IGNORECASE):
                has_override = True

        critical_flag = 1 if has_override else 0

        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO finding_suppressions (
                    repo_id, fingerprint, reason, author, suppressed_at, pr_id,
                    expires_at, severity, critical_override
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo_id, fingerprint) DO UPDATE SET
                    reason = excluded.reason,
                    author = excluded.author,
                    suppressed_at = excluded.suppressed_at,
                    pr_id = excluded.pr_id,
                    expires_at = excluded.expires_at,
                    severity = excluded.severity,
                    critical_override = excluded.critical_override;
            """, (norm_repo, norm_fp, clean_reason, author.strip(), now, str(pr_id), exp_time, sev_clean, critical_flag))

            # Audit log entry
            conn.execute("""
                INSERT INTO suppression_audit_log (
                    repo_id, fingerprint, action, author, reason, severity,
                    critical_override, pr_id, timestamp
                ) VALUES (?, ?, 'SUPPRESS', ?, ?, ?, ?, ?, ?);
            """, (norm_repo, norm_fp, author.strip(), clean_reason, sev_clean, critical_flag, str(pr_id), now))
            conn.commit()

        logger.info(
            f"🚫 Suppressed finding {norm_fp} for repo {norm_repo} by '{author}' "
            f"[expires in {ttl_days}d, override={has_override}] (reason: '{clean_reason}')"
        )
        return True

    def unsuppress(
        self,
        repo_id: str,
        fingerprint: str,
        author: str = "operator",
        reason: str = "Manual unsuppression"
    ) -> bool:
        """
        Remove a finding suppression and record unsuppress audit log.
        """
        norm_repo = self.normalize_repo_id(repo_id)
        norm_fp = fingerprint.strip().lower()
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                DELETE FROM finding_suppressions
                WHERE repo_id = ? AND fingerprint = ?;
            """, (norm_repo, norm_fp))
            deleted = cursor.rowcount > 0

            if deleted:
                conn.execute("""
                    INSERT INTO suppression_audit_log (
                        repo_id, fingerprint, action, author, reason, timestamp
                    ) VALUES (?, ?, 'UNSUPPRESS', ?, ?, ?);
                """, (norm_repo, norm_fp, author.strip(), reason.strip(), now))
                conn.commit()
                logger.info(f"🔓 Unsuppressed finding {norm_fp} for repo {norm_repo}")
            return deleted

    def is_suppressed(self, repo_id: str, fingerprint: str) -> bool:
        """
        Check if a fingerprint is actively suppressed for a repository.
        Excludes expired suppressions.
        """
        norm_repo = self.normalize_repo_id(repo_id)
        norm_fp = fingerprint.strip().lower()
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT 1 FROM finding_suppressions
                WHERE repo_id = ? AND fingerprint = ?
                  AND (expires_at IS NULL OR expires_at > ?);
            """, (norm_repo, norm_fp, now)).fetchone()
            return row is not None

    def has_critical_override(self, repo_id: str, fingerprint: str) -> bool:
        """
        Check if an active suppression possesses an explicit override for a CRITICAL finding.
        Enforces severity ceilings.
        """
        norm_repo = self.normalize_repo_id(repo_id)
        norm_fp = fingerprint.strip().lower()
        now = time.time()
        with self._get_connection() as conn:
            row = conn.execute("""
                SELECT critical_override FROM finding_suppressions
                WHERE repo_id = ? AND fingerprint = ?
                  AND (expires_at IS NULL OR expires_at > ?);
            """, (norm_repo, norm_fp, now)).fetchone()
            if not row:
                return False
            return bool(row["critical_override"])

    def get_suppressed_fingerprints(self, repo_id: str) -> Set[str]:
        """Retrieve all active, non-expired suppressed fingerprints for a repository."""
        norm_repo = self.normalize_repo_id(repo_id)
        now = time.time()
        with self._get_connection() as conn:
            rows = conn.execute("""
                SELECT fingerprint FROM finding_suppressions
                WHERE repo_id = ?
                  AND (expires_at IS NULL OR expires_at > ?);
            """, (norm_repo, now)).fetchall()
            return {row["fingerprint"] for row in rows}

    def list_suppressions(
        self,
        repo_id: Optional[str] = None,
        include_expired: bool = False
    ) -> List[Dict[str, Any]]:
        """List suppressions, optionally filtered by repository and active expiration status."""
        now = time.time()
        with self._get_connection() as conn:
            query = """
                SELECT id, repo_id, fingerprint, reason, author, suppressed_at, pr_id,
                       expires_at, severity, critical_override
                FROM finding_suppressions
            """
            params: List[Any] = []
            conditions = []
            if repo_id:
                conditions.append("repo_id = ?")
                params.append(self.normalize_repo_id(repo_id))
            if not include_expired:
                conditions.append("(expires_at IS NULL OR expires_at > ?)")
                params.append(now)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY suppressed_at DESC;"
            rows = conn.execute(query, tuple(params)).fetchall()

            return [
                {
                    "id": r["id"],
                    "repo_id": r["repo_id"],
                    "fingerprint": r["fingerprint"],
                    "reason": r["reason"],
                    "author": r["author"],
                    "suppressed_at": r["suppressed_at"],
                    "pr_id": r["pr_id"],
                    "expires_at": r["expires_at"],
                    "is_expired": bool(r["expires_at"] and r["expires_at"] <= now),
                    "severity": r["severity"],
                    "critical_override": bool(r["critical_override"])
                }
                for r in rows
            ]

    def get_audit_log(
        self,
        repo_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Retrieve historical suppression audit log entries."""
        with self._get_connection() as conn:
            query = """
                SELECT id, repo_id, fingerprint, action, author, reason, severity,
                       critical_override, pr_id, timestamp, details_json
                FROM suppression_audit_log
            """
            params: List[Any] = []
            conditions = []
            if repo_id:
                conditions.append("repo_id = ?")
                params.append(self.normalize_repo_id(repo_id))
            if fingerprint:
                conditions.append("fingerprint = ?")
                params.append(fingerprint.strip().lower())

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY timestamp DESC LIMIT ?;"
            params.append(limit)

            rows = conn.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]
