"""Durable shadow evidence, independent labels, promotion gates, and posting control."""

import argparse
import hashlib
import json
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, TypedDict

from code_review_agent.models import ReviewAPIResponse
from code_review_agent.publication import valid_receipt


class PilotCounts(TypedDict):
    prs: set[str]
    clean: set[str]
    proposed: int
    actionable: int


class PilotStore:
    def __init__(self, database: str | None = None):
        self.database: str = (
            database or os.environ.get("QUEUE_DB_PATH") or "webhook_jobs.db"
        )
        from code_review_agent.pilot_migration import migrate_pilot

        migrate_pilot(self.database)
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            version = conn.execute(
                "SELECT version FROM schema_versions WHERE component='pilot'"
            ).fetchone()
            if version and version[0] > 2:
                raise RuntimeError("Pilot schema is newer than this image")
            conn.execute("INSERT OR IGNORE INTO schema_versions VALUES ('pilot',2)")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pilot_runs (
                    run_id TEXT PRIMARY KEY, repository TEXT NOT NULL, pr TEXT NOT NULL,
                    head TEXT NOT NULL, mode TEXT NOT NULL, complete INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, created_at REAL NOT NULL,
                    reviewer_profile_id TEXT NOT NULL DEFAULT 'LEGACY', input_snapshot_hash TEXT,
                    UNIQUE(repository,pr,head,mode,reviewer_profile_id));
                CREATE TABLE IF NOT EXISTS pilot_active_profiles (
                    repository TEXT PRIMARY KEY, reviewer_profile_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_labels (
                    run_id TEXT NOT NULL REFERENCES pilot_runs(run_id), reviewer TEXT NOT NULL,
                    labels TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(run_id,reviewer));
                CREATE TABLE IF NOT EXISTS pilot_publications (
                    run_id TEXT NOT NULL REFERENCES pilot_runs(run_id), fingerprint TEXT NOT NULL,
                    receipt TEXT NOT NULL, head TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(run_id,fingerprint));
                CREATE TABLE IF NOT EXISTS pilot_controls (
                    repository TEXT PRIMARY KEY, mode TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_control_events (
                    repository TEXT NOT NULL, mode TEXT NOT NULL, actor TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_release_approvals (
                    repository TEXT PRIMARY KEY, release_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pilot_adjudications (
                    run_id TEXT PRIMARY KEY REFERENCES pilot_runs(run_id), reviewer TEXT NOT NULL,
                    labels TEXT NOT NULL, created_at REAL NOT NULL);
            """)

    def connect(self):
        conn = sqlite3.connect(self.database, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return closing_connection(conn)

    def record(
        self,
        repository: str,
        pr: str,
        response: ReviewAPIResponse,
        mode: str,
        activate: bool = True,
    ) -> str:
        if mode not in {"SHADOW", "ADVISORY"} or not response.analyzed_commit:
            raise ValueError("Pilot runs require a mode and analyzed commit")
        profile = response.reviewer_profile_id or "LEGACY"
        if profile != "LEGACY" and (
            not response.reviewer_profile
            or response.reviewer_profile.profile_id != profile
            or not response.input_snapshot_hash
        ):
            raise ValueError("Reviewer profile or input snapshot identity mismatch")
        # Audit evidence is local protected data. Exclude the raw diff/full model report.
        snapshot = {
            "analysis_complete": response.analysis_complete,
            "analyzer_status": response.analyzer_status,
            "reviewer_profile": response.reviewer_profile.model_dump()
            if response.reviewer_profile
            else None,
            "input_snapshot_hash": response.input_snapshot_hash,
            "findings": [f.model_dump() for f in response.findings],
            "telemetry": response.telemetry.model_dump()
            if response.telemetry
            else None,
        }
        run_id = hashlib.sha256(
            f"{repository}:{pr}:{response.analyzed_commit}:{mode}:{profile}".encode()
        ).hexdigest()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT input_snapshot_hash FROM pilot_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing and existing[0] != response.input_snapshot_hash:
                raise ValueError("Existing pilot run has different input evidence")
            conn.execute(
                "INSERT OR IGNORE INTO pilot_runs VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    repository,
                    str(pr),
                    response.analyzed_commit,
                    mode,
                    response.analysis_complete,
                    json.dumps(snapshot),
                    time.time(),
                    profile,
                    response.input_snapshot_hash,
                ),
            )
            if activate and profile != "LEGACY":
                conn.execute(
                    "INSERT INTO pilot_active_profiles VALUES (?,?) ON CONFLICT(repository) DO UPDATE SET reviewer_profile_id=excluded.reviewer_profile_id",
                    (repository, profile),
                )
        return run_id

    def label(
        self,
        run_id: str,
        reviewer: str,
        cohort: str,
        outcomes: dict[str, str],
        rationale: str,
    ):
        if (
            not reviewer.strip()
            or not rationale.strip()
            or cohort not in {"CLEAN", "DEFECT", "HARD_NEGATIVE"}
        ):
            raise ValueError("Independent labels need reviewer, cohort, and rationale")
        if any(
            value not in {"ACTIONABLE", "NOT_ACTIONABLE"} for value in outcomes.values()
        ):
            raise ValueError("Invalid finding outcome")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT snapshot FROM pilot_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown pilot run")
            fingerprints = {
                f["fingerprint"]
                for f in json.loads(row[0])["findings"]
                if not f["publication_reasons"]
            }
            if set(outcomes) != fingerprints:
                raise ValueError("Label every proposed eligible finding exactly once")
            conn.execute(
                "INSERT INTO pilot_labels VALUES (?,?,?,?)",
                (
                    run_id,
                    reviewer.strip().lower(),
                    json.dumps(
                        {"cohort": cohort, "outcomes": outcomes, "rationale": rationale}
                    ),
                    time.time(),
                ),
            )

    def publication(
        self, run_id: str, fingerprints: list[str], receipt: dict, head: str
    ):
        if not valid_receipt(receipt):
            raise ValueError("A provider receipt is required")
        if (
            receipt.get("event", "COMMENT") != "COMMENT"
            or receipt.get("commit_id", head) != head
        ):
            raise ValueError(
                "Pilot publication must be advisory and match the analyzed commit"
            )
        with self.connect() as conn:
            row = conn.execute(
                "SELECT head,snapshot FROM pilot_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not row or head != row["head"]:
                raise ValueError("Wrong-commit pilot publication")
            eligible = {
                f["fingerprint"]
                for f in json.loads(row["snapshot"])["findings"]
                if not f["publication_reasons"]
            }
            if (
                len(fingerprints) != len(set(fingerprints))
                or not set(fingerprints) <= eligible
            ):
                raise ValueError("Duplicate or unknown published finding")
            for fingerprint in fingerprints:
                old = conn.execute(
                    "SELECT receipt FROM pilot_publications WHERE run_id=? AND fingerprint=?",
                    (run_id, fingerprint),
                ).fetchone()
                serialized = json.dumps({"id": str(receipt["id"])}, sort_keys=True)
                if old and old[0] != serialized:
                    raise ValueError(
                        "Duplicate pilot publication has a different receipt"
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO pilot_publications VALUES (?,?,?,?,?)",
                    (run_id, fingerprint, serialized, head, time.time()),
                )

    def active_profile(self, repository: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT reviewer_profile_id FROM pilot_active_profiles WHERE repository=?",
                (repository,),
            ).fetchone()
        return row[0] if row else "NONE"

    def report(self, repository: str, reviewer_profile_id: str | None = None) -> dict:
        profile = reviewer_profile_id or self.active_profile(repository)
        if profile == "LEGACY":
            raise ValueError("Legacy evidence cannot qualify a reviewer")
        counts: dict[str, PilotCounts] = {
            "SHADOW": {"prs": set(), "clean": set(), "proposed": 0, "actionable": 0},
            "ADVISORY": {"prs": set(), "clean": set(), "proposed": 0, "actionable": 0},
        }
        unresolved = 0
        durations, costs = [], []
        with self.connect() as conn:
            runs = conn.execute(
                "SELECT * FROM pilot_runs WHERE repository=? AND reviewer_profile_id=? ORDER BY created_at",
                (repository, profile),
            ).fetchall()
            # Count one revision per PR. Keep older records for audit, never inflate the sample.
            latest = {(r["mode"], r["pr"]): r["run_id"] for r in runs}
            for run in runs:
                if (
                    run["mode"] == "SHADOW"
                    and latest[(run["mode"], run["pr"])] != run["run_id"]
                ):
                    continue
                labels = [
                    json.loads(r[0])
                    for r in conn.execute(
                        "SELECT labels FROM pilot_labels WHERE run_id=?",
                        (run["run_id"],),
                    )
                ]
                if (
                    not run["complete"]
                    or len(labels) < 2
                    or any(
                        (v["cohort"], v["outcomes"])
                        != (labels[0]["cohort"], labels[0]["outcomes"])
                        for v in labels[1:]
                    )
                ):
                    decision = conn.execute(
                        "SELECT labels FROM pilot_adjudications WHERE run_id=?",
                        (run["run_id"],),
                    ).fetchone()
                    if not run["complete"] or len(labels) < 2 or not decision:
                        unresolved += 1
                        continue
                    labels = [json.loads(decision[0])]
                group = counts[run["mode"]]
                group["prs"].add(run["pr"])
                if labels[0]["cohort"] == "CLEAN":
                    group["clean"].add(run["pr"])
                outcomes = labels[0]["outcomes"]
                if run["mode"] == "ADVISORY":
                    published = {
                        r[0]
                        for r in conn.execute(
                            "SELECT fingerprint FROM pilot_publications WHERE run_id=?",
                            (run["run_id"],),
                        )
                    }
                    outcomes = {k: v for k, v in outcomes.items() if k in published}
                group["proposed"] += len(outcomes)
                group["actionable"] += sum(v == "ACTIONABLE" for v in outcomes.values())
                metrics = json.loads(run["snapshot"]).get("telemetry") or {}
                if metrics.get("duration_seconds") is not None:
                    durations.append(metrics["duration_seconds"])
                if metrics.get("estimated_cost_usd") is not None:
                    costs.append(metrics["estimated_cost_usd"])
            controls = conn.execute(
                "SELECT mode FROM pilot_controls WHERE repository=?", (repository,)
            ).fetchone()
        result: dict[str, Any] = {}
        for mode, group in counts.items():
            denominator = group["proposed"]
            result[mode.lower()] = {
                "prs": len(group["prs"]),
                "clean_prs": len(group["clean"]),
                "labeled_findings": denominator,
                "actionable": group["actionable"],
                "actionable_rate": group["actionable"] / denominator
                if denominator
                else None,
            }
        shadow, advisory = result["shadow"], result["advisory"]
        result.update(
            repository=repository,
            reviewer_profile_id=profile,
            unresolved_runs=unresolved,
            mode=controls[0] if controls else "SHADOW",
            shadow_gate=shadow["prs"] >= 100
            and shadow["clean_prs"] >= 25
            and unresolved == 0
            and (
                shadow["actionable_rate"] is not None
                and shadow["actionable_rate"] >= 0.85
            ),
            advisory_gate=advisory["labeled_findings"] >= 100
            and (advisory["actionable_rate"] or 0) >= 0.85
            and unresolved == 0,
            mean_latency_seconds=sum(durations) / len(durations) if durations else None,
            estimated_cost_usd=sum(costs) if costs else None,
            cost_note="Estimated; provider invoices and missing usage are not included",
        )
        return result

    def adjudicate(
        self,
        run_id: str,
        reviewer: str,
        cohort: str,
        outcomes: dict[str, str],
        rationale: str,
    ):
        """Two original reviews remain immutable; a third reviewer resolves a conflict."""
        with self.connect() as conn:
            reviewers = {
                r[0]
                for r in conn.execute(
                    "SELECT reviewer FROM pilot_labels WHERE run_id=?", (run_id,)
                )
            }
            row = conn.execute(
                "SELECT snapshot FROM pilot_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if (
                len(reviewers) < 2
                or not row
                or not reviewer.strip()
                or reviewer.lower().strip() in reviewers
            ):
                raise ValueError(
                    "Adjudication requires two independent reviews and a third reviewer"
                )
            fingerprints = {
                f["fingerprint"]
                for f in json.loads(row[0])["findings"]
                if not f["publication_reasons"]
            }
            if (
                cohort not in {"CLEAN", "DEFECT", "HARD_NEGATIVE"}
                or set(outcomes) != fingerprints
                or not rationale.strip()
                or any(
                    v not in {"ACTIONABLE", "NOT_ACTIONABLE"} for v in outcomes.values()
                )
            ):
                raise ValueError("Invalid adjudication evidence")
            conn.execute(
                "INSERT INTO pilot_adjudications VALUES (?,?,?,?)",
                (
                    run_id,
                    reviewer.lower().strip(),
                    json.dumps(
                        {"cohort": cohort, "outcomes": outcomes, "rationale": rationale}
                    ),
                    time.time(),
                ),
            )

    def weekly(
        self, repository: str, days: int = 7, reviewer_profile_id: str | None = None
    ) -> dict:
        profile = reviewer_profile_id or self.active_profile(repository)
        if not 1 <= days <= 365:
            raise ValueError("Report window must be 1 to 365 days")
        with self.connect() as conn:
            runs = conn.execute(
                "SELECT complete,snapshot FROM pilot_runs WHERE repository=? AND reviewer_profile_id=? AND created_at>=?",
                (repository, profile, time.time() - days * 86400),
            ).fetchall()
        durations, costs = [], []
        usage: dict[str, int] = {}
        proposed, withheld = 0, 0
        for run in runs:
            snapshot = json.loads(run["snapshot"])
            proposed += len(snapshot["findings"])
            withheld += sum(
                bool(f["publication_reasons"]) for f in snapshot["findings"]
            )
            metrics = snapshot.get("telemetry") or {}
            source = metrics.get("token_usage_source", "UNAVAILABLE")
            usage[source] = usage.get(source, 0) + 1
            if metrics.get("duration_seconds") is not None:
                durations.append(metrics["duration_seconds"])
            if metrics.get("estimated_cost_usd") is not None:
                costs.append(metrics["estimated_cost_usd"])
        durations.sort()
        return {
            "days": days,
            "runs": len(runs),
            "incomplete_runs": sum(not r["complete"] for r in runs),
            "latency_p50_seconds": statistics.median(durations) if durations else None,
            "latency_p95_seconds": durations[
                min(len(durations) - 1, int(len(durations) * 0.95))
            ]
            if durations
            else None,
            "estimated_cost_usd": sum(costs) if costs else None,
            "cost_coverage_runs": len(costs),
            "usage_sources": usage,
            "candidates": proposed,
            "withheld_candidates": withheld,
            "withheld_rate": withheld / proposed if proposed else None,
            "quality_and_gates": self.report(repository, profile),
        }

    def control(
        self,
        repository: str,
        mode: str,
        actor: str,
        reason: str,
        release: dict | None = None,
    ):
        if (
            mode not in {"SHADOW", "PAUSED", "ADVISORY"}
            or not actor.strip()
            or not reason.strip()
        ):
            raise ValueError("Control requires a mode, actor, and reason")
        if mode == "ADVISORY":
            from code_review_agent.release import validate_release

            validate_release(release or {})
            if (release or {}).get("reviewer_profile_id") != self.active_profile(
                repository
            ):
                raise ValueError("Release does not match the active reviewer profile")
            if not self.report(repository)["shadow_gate"]:
                raise ValueError("Independent shadow evidence has not passed")
        with self.connect() as conn:
            values = (repository, mode, actor, reason, time.time())
            conn.execute(
                "INSERT OR REPLACE INTO pilot_controls VALUES (?,?,?,?,?)", values
            )
            conn.execute("INSERT INTO pilot_control_events VALUES (?,?,?,?,?)", values)
            if mode == "ADVISORY":
                conn.execute(
                    "INSERT OR REPLACE INTO pilot_release_approvals VALUES (?,?)",
                    (repository, json.dumps(release)),
                )

    def require_advisory(self, repository: str, reviewer_profile_id: str | None = None):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT mode FROM pilot_controls WHERE repository=?", (repository,)
            ).fetchone()
        if not row or row[0] != "ADVISORY":
            raise PermissionError(
                "Pilot posting is paused or requires approved shadow and release evidence"
            )
        with self.connect() as conn:
            approval = conn.execute(
                "SELECT release_json FROM pilot_release_approvals WHERE repository=?",
                (repository,),
            ).fetchone()
        release = json.loads(approval[0]) if approval else {}
        active = self.active_profile(repository)
        if (
            active in {"NONE", "LEGACY"}
            or release.get("reviewer_profile_id") != active
            or (reviewer_profile_id and reviewer_profile_id != active)
        ):
            raise PermissionError("Pilot approval does not match this reviewer profile")
        from code_review_agent.release import validate_release

        try:
            validate_release(release)
        except ValueError as error:
            raise PermissionError(
                "Pilot release evidence no longer verifies"
            ) from error
        from code_review_agent.data_policy import production

        if production():
            with self.connect() as conn:
                approval = conn.execute(
                    "SELECT release_json FROM pilot_release_approvals WHERE repository=?",
                    (repository,),
                ).fetchone()
            release = json.loads(approval[0]) if approval else {}
            if (
                not os.getenv("REVIEW_DEPLOYED_COMMIT")
                or release.get("commit") != os.getenv("REVIEW_DEPLOYED_COMMIT")
                or release.get("image_digest") != os.getenv("REVIEW_IMAGE_DIGEST")
            ):
                raise PermissionError(
                    "Pilot approval does not match this deployed release"
                )
            from code_review_agent.review_contracts import ReviewerProfile
            from code_review_agent.reviewer_profile import same_implementation

            if not same_implementation(
                ReviewerProfile.model_validate(release.get("reviewer_profile"))
            ):
                raise PermissionError(
                    "Pilot approval does not match this reviewer implementation"
                )
            from code_review_agent.reviewer_profile import same_memory_revision

            if not same_memory_revision(
                ReviewerProfile.model_validate(release.get("reviewer_profile")),
                repository,
            ):
                raise PermissionError(
                    "Pilot approval does not match the current team-memory revision"
                )


class closing_connection:
    """SQLite context managers commit but do not close connections."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn.__enter__()

    def __exit__(self, *args):
        try:
            return self.conn.__exit__(*args)
        finally:
            self.conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["report", "weekly", "label", "adjudicate", "control", "export"],
    )
    parser.add_argument(
        "--database", default=os.getenv("QUEUE_DB_PATH", "webhook_jobs.db")
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--file", help="JSON label/control input, or export output")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--reviewer-profile-id")
    args = parser.parse_args()
    store = PilotStore(args.database)
    if args.operation in {"label", "adjudicate", "control"}:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        if args.operation == "label":
            store.label(**data)
        elif args.operation == "adjudicate":
            store.adjudicate(**data)
        else:
            store.control(args.repository, **data)
    if args.operation == "export":
        with store.connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM pilot_runs WHERE repository=? AND (? IS NULL OR reviewer_profile_id=?)",
                    (
                        args.repository,
                        args.reviewer_profile_id,
                        args.reviewer_profile_id,
                    ),
                )
            ]
        Path(args.file).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(
        json.dumps(
            store.weekly(args.repository, args.days, args.reviewer_profile_id)
            if args.operation == "weekly"
            else store.report(args.repository, args.reviewer_profile_id),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
