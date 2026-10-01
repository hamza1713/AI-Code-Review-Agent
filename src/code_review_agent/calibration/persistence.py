"""Occurrence storage, transactional feedback, and conservative legacy migration."""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from code_review_agent.calibration import contracts as c


class FindingNotFoundError(LookupError):
    pass


class AmbiguousFindingError(ValueError):
    pass


class FindingOutcomeStore:
    VERSION = 2

    def __init__(self, db_path=None):
        self.db_path = Path(
            db_path or os.getenv("QUEUE_DB_PATH", "webhook_jobs.db")
        ).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def transaction(self):
        conn = self._get_connection()
        depth = getattr(self._local, "depth", 0)
        self._local.depth = depth + 1
        name = f"feedback_{depth}"
        try:
            conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {name}")
            yield conn
            conn.commit() if depth == 0 else conn.execute(f"RELEASE {name}")
        except Exception:
            if depth == 0:
                conn.rollback()
            else:
                conn.execute(f"ROLLBACK TO {name}")
                conn.execute(f"RELEASE {name}")
            raise
        finally:
            self._local.depth = depth

    def _init_db(self):
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            version = conn.execute(
                "SELECT version FROM schema_versions WHERE component='calibration'"
            ).fetchone()
            if version and version[0] > self.VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application; restore a matching backup."
                )
            if version and version[0] == self.VERSION:
                return
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='finding_outcomes'"
            ).fetchone()
            if exists:
                conn.execute(
                    "ALTER TABLE finding_outcomes RENAME TO finding_outcomes_legacy_v1"
                )
            conn.execute("""CREATE TABLE finding_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, finding_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL DEFAULT 'github', host TEXT NOT NULL DEFAULT 'github.com',
                repo_id TEXT NOT NULL, pr_id TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'QUALITY', evidence_grade TEXT NOT NULL DEFAULT 'HEURISTIC',
                severity TEXT NOT NULL DEFAULT 'MEDIUM', outcome TEXT NOT NULL DEFAULT 'PENDING',
                source TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL, first_posted_at REAL,
                last_seen_at REAL NOT NULL, head_sha TEXT NOT NULL DEFAULT '',
                publication_status TEXT NOT NULL DEFAULT 'PUBLISHED', provider_review_id TEXT DEFAULT '',
                provider_comment_id TEXT DEFAULT '', pr_closed_at REAL, lifecycle_status TEXT DEFAULT 'OPEN',
                metadata_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '{}',
                audit_status TEXT NOT NULL DEFAULT 'UNAUDITED', audit_reason_code TEXT DEFAULT '',
                audit_notes TEXT DEFAULT '', audited_by TEXT DEFAULT '', audited_at REAL,
                generated_test_code TEXT DEFAULT '', test_status TEXT DEFAULT '', claim_title TEXT DEFAULT '',
                claim_description TEXT DEFAULT '', file_path TEXT DEFAULT '', line_number INTEGER DEFAULT 0,
                UNIQUE(provider,host,repo_id,pr_id,fingerprint))""")
            if exists:
                old = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(finding_outcomes_legacy_v1)"
                    )
                }
                new = {
                    r[1] for r in conn.execute("PRAGMA table_info(finding_outcomes)")
                }
                columns = sorted(
                    (old & new)
                    - {
                        "id",
                        "finding_id",
                        "first_posted_at",
                        "publication_status",
                        "last_seen_at",
                    }
                )
                # Names come exclusively from SQLite's schema, never request data.
                quoted = ",".join(f'"{col}"' for col in columns)
                conn.execute(f"""INSERT INTO finding_outcomes ({quoted},finding_id,last_seen_at,publication_status)
                    SELECT {quoted},lower(hex(randomblob(16))),updated_at,'LEGACY_UNVERIFIED'
                    FROM finding_outcomes_legacy_v1""")
            conn.execute(
                "CREATE INDEX occurrence_window ON finding_outcomes(provider,host,repo_id,first_posted_at)"
            )
            conn.execute("""CREATE TABLE feedback_events (
                event_id TEXT PRIMARY KEY, finding_id TEXT NOT NULL, kind TEXT NOT NULL,
                actor TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
                payload_json TEXT NOT NULL, FOREIGN KEY(finding_id) REFERENCES finding_outcomes(finding_id))""")
            conn.execute("""CREATE TRIGGER immutable_feedback_update BEFORE UPDATE ON feedback_events
                BEGIN SELECT RAISE(ABORT, 'Feedback history is immutable'); END""")
            conn.execute("""CREATE TABLE reaction_observations (
                finding_id TEXT NOT NULL, reaction_id TEXT NOT NULL, actor TEXT NOT NULL,
                content TEXT NOT NULL, active INTEGER NOT NULL, observed_at REAL NOT NULL,
                PRIMARY KEY(finding_id,reaction_id))""")
            conn.execute(
                "CREATE TABLE reaction_poll_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            # Old critical flags had no trustworthy provenance: require reauthorization.
            suppression = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='finding_suppressions'"
            ).fetchone()
            if suppression:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS finding_suppressions_legacy_v1 AS SELECT * FROM finding_suppressions"
                )
                cols = {
                    r[1]
                    for r in conn.execute("PRAGMA table_info(finding_suppressions)")
                }
                if "critical_override" in cols:
                    conn.execute("UPDATE finding_suppressions SET critical_override=0")
                conn.execute("""DELETE FROM finding_suppressions WHERE
                    reason LIKE 'False positive%' OR reason LIKE 'Flagged via % reaction'
                    OR reason LIKE 'Audit rejected%' OR reason LIKE 'Auto-remediated via%'""")
            conn.execute(
                "INSERT OR REPLACE INTO schema_versions VALUES ('calibration',?)",
                (self.VERSION,),
            )

    def _event(self, conn, finding_id, kind, actor, source, payload, event_id=None):
        event_id = event_id or uuid.uuid4().hex
        return (
            conn.execute(
                "INSERT OR IGNORE INTO feedback_events VALUES (?,?,?,?,?,?,?)",
                (
                    event_id,
                    finding_id,
                    kind,
                    actor,
                    source,
                    time.time(),
                    json.dumps(payload),
                ),
            ).rowcount
            > 0
        )

    def get_finding(
        self,
        repo_id="",
        fingerprint="",
        *,
        finding_id=None,
        pr_id=None,
        provider="github",
        host="github.com",
    ):
        query = "SELECT * FROM finding_outcomes WHERE provider=? AND host=?"
        args = [provider, host.lower()]
        if finding_id:
            query += " AND finding_id=?"
            args.append(finding_id)
            if repo_id:
                query += " AND repo_id=?"
                args.append(repo_id.strip().lower())
        else:
            query += " AND repo_id=? AND fingerprint=?"
            args.extend([repo_id.strip().lower(), c.clean_fingerprint(fingerprint)])
        if pr_id is not None:
            query += " AND pr_id=?"
            args.append(str(pr_id))
        rows = self._get_connection().execute(query, args).fetchall()
        if len(rows) > 1:
            raise AmbiguousFindingError(
                "Fingerprint matches multiple PRs; supply finding_id or PR scope."
            )
        return dict(rows[0]) if rows else None

    def require_finding(self, repo_id="", fingerprint="", **scope):
        row = self.get_finding(repo_id, fingerprint, **scope)
        if row is None:
            raise FindingNotFoundError("No recorded finding matches this scope.")
        return row

    def record_finding(
        self,
        repo_id,
        fingerprint,
        category=c.CATEGORY_QUALITY,
        evidence_grade=c.HEURISTIC,
        severity="MEDIUM",
        pr_id="",
        outcome=c.PENDING,
        reason="",
        source="review",
        created_at=None,
        metadata=None,
        generated_test_code="",
        test_status="",
        claim_title="",
        claim_description="",
        file_path="",
        line_number=0,
        *,
        provider="github",
        host="github.com",
        head_sha="",
        provider_review_id="",
        provider_comment_id="",
        publication_status="PUBLISHED",
        evidence_provenance=None,
    ):
        """Record an acknowledged publication. Analysis alone must not call this method."""
        fp = c.clean_fingerprint(fingerprint)
        if not fp:
            return {}
        if outcome not in c.VALID_OUTCOMES:
            raise ValueError("Invalid finding outcome")
        now = time.time()
        at = now if created_at is None else created_at
        meta = metadata or {}
        scope = {"pr_id": str(pr_id), "provider": provider, "host": host.lower()}
        repo_id = (repo_id or "default").strip().lower()
        grade = c.normalize_evidence_grade(evidence_grade)
        evidence = evidence_provenance or meta.get("evidence_provenance", {})
        if grade == c.REPRODUCED and not (
            evidence.get("finding_fingerprint") == fp
            and evidence.get("commit_sha") == head_sha
            and head_sha
            and evidence.get("test_id")
            and evidence.get("executed") is True
            and evidence.get("status") == "REPRODUCED"
        ):
            grade = c.HEURISTIC
        with self.transaction() as conn:
            old = self.get_finding(repo_id, fp, **scope)
            fid = old["finding_id"] if old else uuid.uuid4().hex
            data = {
                "finding_id": fid,
                "provider": provider,
                "host": host.lower(),
                "repo_id": repo_id,
                "pr_id": str(pr_id),
                "fingerprint": fp,
                "category": c.normalize_category(category),
                "evidence_grade": grade,
                "severity": (severity or "MEDIUM").upper(),
                "outcome": outcome,
                "reason": reason or "",
                "source": source or "review",
                "created_at": at,
                "updated_at": now,
                "last_seen_at": now,
                "head_sha": head_sha or "",
                "first_posted_at": at if publication_status == "PUBLISHED" else None,
                "publication_status": publication_status,
                "provider_review_id": str(provider_review_id or ""),
                "provider_comment_id": str(provider_comment_id or ""),
                "metadata_json": json.dumps(meta),
                "evidence_json": json.dumps(evidence),
                "generated_test_code": generated_test_code
                or meta.get("generated_test_code", ""),
                "test_status": test_status or meta.get("test_status", ""),
                "claim_title": claim_title or meta.get("title", ""),
                "claim_description": claim_description or meta.get("description", ""),
                "file_path": file_path or meta.get("file_path", ""),
                "line_number": line_number or meta.get("line_number", 0),
            }
            if old:
                for key in ("outcome", "source", "reason", "created_at"):
                    data[key] = old[key]
                data["first_posted_at"] = (
                    old["first_posted_at"] or data["first_posted_at"]
                )
                for key in (
                    "generated_test_code",
                    "test_status",
                    "provider_comment_id",
                    "provider_review_id",
                ):
                    data[key] = data[key] or old[key]
                assignments = ",".join(f"{key}=?" for key in data)
                conn.execute(
                    f"UPDATE finding_outcomes SET {assignments} WHERE finding_id=?",
                    [*data.values(), fid],
                )
            else:
                conn.execute(
                    f"INSERT INTO finding_outcomes ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                    list(data.values()),
                )
            self._event(conn, fid, "PUBLICATION", "system", source or "review", data)
        return self.require_finding(finding_id=fid, provider=provider, host=host)

    def record_findings_batch(
        self,
        repo_id,
        findings,
        pr_id="",
        default_evidence_grade=c.HEURISTIC,
        shared_test_code="",
        shared_test_status="",
        **scope,
    ):
        count = 0
        with self.transaction():
            for item in findings:
                if not item.get("fingerprint"):
                    continue
                self.record_finding(
                    repo_id,
                    item["fingerprint"],
                    pr_id=pr_id,
                    category=item.get("category"),
                    severity=item.get("severity", "MEDIUM"),
                    evidence_grade=item.get("evidence_grade", default_evidence_grade),
                    generated_test_code=item.get("generated_test_code")
                    or shared_test_code,
                    test_status=item.get("test_status") or shared_test_status,
                    claim_title=item.get("claim_title")
                    or item.get("name")
                    or item.get("title", ""),
                    claim_description=item.get("description", ""),
                    file_path=item.get("file_path", ""),
                    line_number=item.get("line_number", 0),
                    metadata=item.get("metadata"),
                    **scope,
                )
                count += 1
        return count

    def update_outcome(
        self,
        repo_id="",
        fingerprint="",
        outcome=c.PENDING,
        reason=None,
        source=None,
        updated_at=None,
        category=None,
        evidence_grade=None,
        *,
        actor="operator",
        event_id=None,
        **scope,
    ):
        if not scope.get("finding_id") and not c.clean_fingerprint(fingerprint):
            raise ValueError(
                "Invalid or empty fingerprint: Finding fingerprint is required"
            )
        outcome = outcome.strip().upper()
        if outcome not in c.VALID_OUTCOMES:
            raise ValueError("Invalid outcome")
        source = source or c.SOURCE_HUMAN_EXPLICIT
        inferred = source in {
            "comment_sync_resolved",
            c.SOURCE_PR_RESOLUTION,
            "apply_remediation",
            c.SOURCE_REMEDIATION_APPLIED,
        }
        with self.transaction() as conn:
            row = self.require_finding(repo_id, fingerprint, **scope)
            payload = {
                "outcome": outcome,
                "reason": reason,
                "previous_outcome": row["outcome"],
            }
            if not self._event(
                conn,
                row["finding_id"],
                "LIFECYCLE" if inferred else "FEEDBACK",
                actor,
                source,
                payload,
                event_id,
            ):
                return row
            if inferred:
                conn.execute(
                    "UPDATE finding_outcomes SET lifecycle_status=?,updated_at=? WHERE finding_id=?",
                    (
                        "NOT_OBSERVED"
                        if source in {"comment_sync_resolved", c.SOURCE_PR_RESOLUTION}
                        else "REMEDIATION_COMMITTED",
                        time.time(),
                        row["finding_id"],
                    ),
                )
            else:
                conn.execute(
                    "UPDATE finding_outcomes SET outcome=?,source=?,reason=?,updated_at=? WHERE finding_id=?",
                    (
                        outcome,
                        c.SOURCE_HUMAN_EXPLICIT,
                        reason or "",
                        updated_at or time.time(),
                        row["finding_id"],
                    ),
                )
        return self.require_finding(repo_id, fingerprint, **scope)

    def record_human_audit(
        self,
        repo_id="",
        fingerprint="",
        is_valid=True,
        reason_code=None,
        notes="",
        audited_by="operator",
        audited_at=None,
        **scope,
    ):
        if reason_code:
            reason_code = c.REASON_ALIASES.get(reason_code.strip().upper(), reason_code)
        reason = reason_code or (
            c.AUDIT_REASON_CORRECT if is_valid else c.AUDIT_REASON_OTHER
        )
        if reason not in c.VALID_AUDIT_REASONS or (
            is_valid != (reason == c.AUDIT_REASON_CORRECT)
        ):
            raise ValueError("Audit reason does not match the decision.")
        with self.transaction() as conn:
            row = self.require_finding(repo_id, fingerprint, **scope)
            if row["evidence_grade"] != c.REPRODUCED:
                raise ValueError(
                    "Only a reproduced finding can receive a reproduction audit."
                )
            outcome = (
                c.CONFIRMED_REAL
                if is_valid
                else (
                    c.FALSE_POSITIVE
                    if reason == c.AUDIT_REASON_FINDING_FALSE
                    else c.INVALID_REPRODUCTION
                )
            )
            audit = {
                "is_valid": is_valid,
                "reason_code": reason,
                "notes": notes,
                "evidence": json.loads(row["evidence_json"]),
                "head_sha": row["head_sha"],
                "generated_test_code": row["generated_test_code"],
            }
            self._event(
                conn,
                row["finding_id"],
                "AUDIT",
                audited_by,
                c.SOURCE_HUMAN_EXPLICIT,
                audit,
            )
            conn.execute(
                """UPDATE finding_outcomes SET outcome=?,audit_status=?,audit_reason_code=?,
                audit_notes=?,audited_by=?,audited_at=?,source=?,updated_at=? WHERE finding_id=?""",
                (
                    outcome,
                    c.AUDIT_STATUS_VALID if is_valid else c.AUDIT_STATUS_INVALID,
                    reason,
                    notes,
                    audited_by,
                    audited_at or time.time(),
                    c.SOURCE_HUMAN_EXPLICIT,
                    time.time(),
                    row["finding_id"],
                ),
            )
        return self.require_finding(repo_id, fingerprint, **scope)

    def list_findings(
        self,
        repo_id=None,
        start_time=None,
        end_time=None,
        category=None,
        evidence_grade=None,
        outcome=None,
        pr_id=None,
        audit_status=None,
        *,
        provider="github",
        host="github.com",
        include_unverified=False,
    ):
        where, args = ["provider=?", "host=?"], [provider, host.lower()]
        if not include_unverified:
            where.append("publication_status='PUBLISHED'")
        filters = {
            "repo_id": repo_id.strip().lower() if repo_id else None,
            "category": c.normalize_category(category) if category else None,
            "evidence_grade": c.normalize_evidence_grade(evidence_grade)
            if evidence_grade
            else None,
            "outcome": outcome,
            "pr_id": str(pr_id) if pr_id is not None else None,
            "audit_status": audit_status,
        }
        for key, value in filters.items():
            if value is not None:
                where.append(f"{key}=?")
                args.append(value)
        if start_time is not None:
            where.append("first_posted_at>=?")
            args.append(start_time)
        if end_time is not None:
            where.append("first_posted_at<=?")
            args.append(end_time)
        return [
            dict(r)
            for r in self._get_connection().execute(
                "SELECT * FROM finding_outcomes WHERE "
                + " AND ".join(where)
                + " ORDER BY first_posted_at DESC",
                args,
            )
        ]

    def aggregate(self, repo_id, start, end, provider="github", host="github.com"):
        """Return counts only: large generated tests never enter metrics memory."""
        return [
            dict(r)
            for r in self._get_connection().execute(
                """SELECT category,evidence_grade,severity,
            outcome,source,audit_status,audit_reason_code,COUNT(*) AS n FROM finding_outcomes
            WHERE provider=? AND host=? AND repo_id=? AND publication_status='PUBLISHED'
            AND first_posted_at>=? AND first_posted_at<=?
            GROUP BY category,evidence_grade,severity,outcome,source,audit_status,audit_reason_code""",
                (provider, host, repo_id.lower(), start, end),
            )
        ]

    def mark_pr_pending_as_ignored(
        self, repo_id, pr_id, *, provider="github", host="github.com"
    ):
        if not repo_id.strip() or not str(pr_id).strip():
            return 0
        with self.transaction() as conn:
            rows = self.list_findings(
                repo_id, pr_id=pr_id, provider=provider, host=host
            )
            for row in rows:
                self._event(conn, row["finding_id"], "MERGE", "system", "pr_merge", {})
            conn.execute(
                """UPDATE finding_outcomes SET pr_closed_at=?,lifecycle_status='MERGED',
                outcome=CASE WHEN outcome='PENDING' THEN 'IGNORED_MERGED' ELSE outcome END,
                updated_at=? WHERE provider=? AND host=? AND repo_id=? AND pr_id=?""",
                (time.time(), time.time(), provider, host, repo_id.lower(), str(pr_id)),
            )
            return sum(row["outcome"] == c.PENDING for row in rows)

    def reconcile_reactions(
        self, finding_id, reactions, *, provider="github", host="github.com"
    ):
        """Called only after a full, authorized reaction snapshot was fetched."""
        with self.transaction() as conn:
            row = self.require_finding(
                finding_id=finding_id, provider=provider, host=host
            )
            conn.execute(
                "UPDATE reaction_observations SET active=0 WHERE finding_id=?",
                (finding_id,),
            )
            for reaction in reactions:
                conn.execute(
                    """INSERT INTO reaction_observations VALUES (?,?,?,?,1,?)
                    ON CONFLICT(finding_id,reaction_id) DO UPDATE SET active=1,observed_at=excluded.observed_at""",
                    (
                        finding_id,
                        str(reaction["id"]),
                        reaction["actor"],
                        reaction["content"],
                        time.time(),
                    ),
                )
            signals = {r["content"] for r in reactions}
            outcome = (
                c.FALSE_POSITIVE
                if signals == {"-1"}
                else c.CONFIRMED_REAL
                if signals == {"+1"}
                else c.PENDING
            )
            previous = conn.execute(
                "SELECT payload_json FROM feedback_events WHERE finding_id=? AND kind='REACTIONS' ORDER BY created_at DESC LIMIT 1",
                (finding_id,),
            ).fetchone()
            payload = {
                "reactions": sorted(reactions, key=lambda r: str(r["id"])),
                "outcome": outcome,
            }
            if previous is None or json.loads(previous[0]) != payload:
                self._event(
                    conn, finding_id, "REACTIONS", "system", c.SOURCE_REACTION, payload
                )
            if row["source"] != c.SOURCE_HUMAN_EXPLICIT:
                conn.execute(
                    "UPDATE finding_outcomes SET outcome=?,source=?,updated_at=? WHERE finding_id=?",
                    (outcome, c.SOURCE_REACTION, time.time(), finding_id),
                )

    def clear(self, repo_id=None):
        with self.transaction() as conn:
            ids = [
                r[0]
                for r in conn.execute(
                    "SELECT finding_id FROM finding_outcomes"
                    + (" WHERE repo_id=?" if repo_id else ""),
                    (repo_id.lower(),) if repo_id else (),
                )
            ]
            for fid in ids:
                conn.execute("DELETE FROM feedback_events WHERE finding_id=?", (fid,))
                conn.execute(
                    "DELETE FROM reaction_observations WHERE finding_id=?", (fid,)
                )
                conn.execute("DELETE FROM finding_outcomes WHERE finding_id=?", (fid,))
            return len(ids)
