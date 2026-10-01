"""
Deterministic Auto-Remediation Engine for Pull Request Findings.
Extracts code suggestions, aligns whitespace/indentation, patches target files,
and commits fixes directly to pull request branches across Git platforms.
"""

import hashlib
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from code_review_agent.config import logger
from code_review_agent.models import RollbackManifest, SastFinding, StructuredPatch
from code_review_agent.path_validation import safe_diff_path, validate_diff_path
from code_review_agent.platform import get_platform_client
from code_review_agent.platform.base import GitPlatformClient
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.tools.ast_security_scanner import AstSecurityScanner
from code_review_agent.tools.sast_scanner import SastEngine


class BranchGuardrailViolation(Exception):
    """Raised when an automated remediation targets a protected branch."""


class PatchMismatchError(Exception):
    """Raised when structured patch expected old text does not match target file."""


class StaleHeadShaError(Exception):
    """Raised when PR head SHA has advanced beyond the reviewed commit."""


class AutoRemediator:
    """
    Automates 1-click remediation commits directly to PR branches.
    Enforces branch guardrails, stale head SHA verification, structured AST patches,
    and persistent rollback manifests.
    """

    @classmethod
    def find_target_finding(
        cls, fingerprint: str, diff: str, repo_root: str | None = None
    ) -> SastFinding | None:
        """
        Locate a finding by deterministic 16-character fingerprint (or prefix).
        Scans both SAST and AST security scanners.
        """
        clean_fp = (fingerprint or "").strip().lower()
        if not clean_fp:
            return None

        # 1. Scan diff using SastEngine
        try:
            sast_findings = SastEngine().scan_diff(diff)
            for f in sast_findings:
                fp = (f.fingerprint or "").lower()
                if fp == clean_fp or (len(clean_fp) >= 6 and fp.startswith(clean_fp)):
                    return f
        except Exception as e:
            logger.warning(f"Error scanning diff with SastEngine: {e}")

        # 2. Scan diff using AstSecurityScanner
        try:
            ast_findings = AstSecurityScanner.scan_diff(diff)
            for f in ast_findings:
                fp = (f.fingerprint or "").lower()
                if fp == clean_fp or (len(clean_fp) >= 6 and fp.startswith(clean_fp)):
                    return f
        except Exception as e:
            logger.warning(f"Error scanning diff with AstSecurityScanner: {e}")

        # 3. Optional repo_root scan
        if repo_root and Path(repo_root).exists():
            try:
                scan_dir = getattr(SastEngine, "scan_directory", None)
                if callable(scan_dir):
                    repo_findings = scan_dir(repo_root)
                    for f in repo_findings:
                        fp = (f.fingerprint or "").lower()
                        if fp == clean_fp or (
                            len(clean_fp) >= 6 and fp.startswith(clean_fp)
                        ):
                            return f
            except Exception as e:
                logger.warning(f"Error scanning repo_root with SastEngine: {e}")

        return None

    @classmethod
    def extract_remediation_code(
        cls, fix_recommendation: str, snippet: str = ""
    ) -> str | None:
        """
        Extract raw replacement code from fix recommendation text or suggestion blocks.
        """
        if not fix_recommendation:
            return None

        clean = fix_recommendation.strip()

        # Check for fenced code blocks (```suggestion, ```python, or ```)
        fence_match = re.search(
            r"```(?:suggestion|python)?\s*\n(.*?)\n```", clean, re.DOTALL
        )
        if fence_match:
            return fence_match.group(1).rstrip()

        # Check for "Replace with: `code`" or "Use: `code`"
        inline_code_match = re.search(
            r"(?:replace with|use|instead of [^:`]+):?\s*`([^`]+)`",
            clean,
            re.IGNORECASE,
        )
        if inline_code_match:
            return inline_code_match.group(1).strip()

        # Fallback to single backticked code
        single_backtick = re.search(r"`([^`]+)`", clean)
        if single_backtick:
            return single_backtick.group(1).strip()

        # Check for code after colon (e.g. "Use parameterized queries: db.query(...)")
        code_call_match = re.search(
            r":\s*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\(.*?\))", clean
        )
        if code_call_match:
            return code_call_match.group(1).strip()

        # Check for code statement after colon (e.g. "Use logger: logger.info(...)")
        colon_split = clean.split(":", 1)
        if len(colon_split) == 2:
            candidate = colon_split[1].strip()
            # If candidate looks like valid python expression/statement
            if any(
                candidate.startswith(pfx)
                for pfx in (
                    "db.",
                    "cursor.",
                    "logger.",
                    "bcrypt.",
                    "subprocess.",
                    "os.",
                    "hashlib.",
                    "json.",
                    "from ",
                    "import ",
                )
            ):
                return candidate

        # Check for lines after "Example:" or "Fix:"
        example_match = re.search(
            r"(?:example|fix|remediation):\s*\n?([^\n]+)", clean, re.IGNORECASE
        )
        if example_match:
            candidate = example_match.group(1).strip()
            if not candidate.startswith("-") and not candidate.startswith("*"):
                return candidate

        # ── Deterministic snippet-based remediation synthesis ──
        if snippet and snippet.strip():
            snip = snippet.strip()
            # 1. SQL injection via f-string
            f_sql = re.search(
                r"(?:(?:db\.(?:query|execute)|cursor\.execute)\s*\(\s*f[\"']|(?:query|sql|stmt)\s*=\s*f[\"'])(.*?)(?:SELECT|INSERT|UPDATE|DELETE)(.*?)[\"']",
                snip,
                re.IGNORECASE,
            )
            if f_sql or ("SELECT" in snip and "{" in snip and "}" in snip):
                # Extract variable inside {}
                var_match = re.search(r"\{([A-Za-z0-9_]+)\}", snip)
                var_name = var_match.group(1) if var_match else "param"
                clean_stmt = re.sub(r"\{[A-Za-z0-9_]+\}", "%s", snip)
                clean_stmt = re.sub(r"['\"]%s['\"]", "%s", clean_stmt)
                clean_stmt = clean_stmt.replace('= f"', '= "').replace("= f'", "= '")
                if (
                    "db.query(" in clean_stmt
                    or "db.execute(" in clean_stmt
                    or "cursor.execute(" in clean_stmt
                ):
                    clean_stmt = re.sub(r"\)\s*$", f", ({var_name},))", clean_stmt)
                else:
                    clean_stmt += f"\n# Pass ({var_name},) to query execution"
                return clean_stmt

            # 2. Plaintext password comparison
            if re.search(
                r"password\s*==\s*user\.password|user\.password\s*==\s*password", snip
            ):
                return "bcrypt.checkpw(password.encode('utf-8'), user.password_hash.encode('utf-8'))"

            # 3. Hardcoded secret / API key
            secret_m = re.search(
                r"([A-Za-z0-9_]*key|[A-Za-z0-9_]*secret|[A-Za-z0-9_]*token|[A-Za-z0-9_]*password)\s*=\s*['\"][^'\"]+['\"]",
                snip,
                re.IGNORECASE,
            )
            if secret_m:
                var = secret_m.group(1)
                env_name = var.upper()
                return f"{var} = os.getenv('{env_name}')"

            # 4. Print statements
            if re.search(r"\bprint\s*\(", snip):
                return re.sub(r"\bprint\s*\(", "logger.info(", snip)

            # 5. OS command injection
            if "shell=True" in snip:
                return snip.replace("shell=True", "shell=False, check=True")

            # 6. Insecure pickle
            if "pickle.loads(" in snip:
                return snip.replace("pickle.loads(", "json.loads(")

            # 7. Insecure MD5 / SHA1
            if "hashlib.md5(" in snip or "hashlib.sha1(" in snip:
                return re.sub(r"hashlib\.(?:md5|sha1)\(", "hashlib.sha256(", snip)

            # 8. Disabled TLS
            if "verify=False" in snip or "verify = False" in snip:
                return re.sub(r"verify\s*=\s*False", "verify=True", snip)
            if "check_hostname=False" in snip or "check_hostname = False" in snip:
                return re.sub(
                    r"check_hostname\s*=\s*False", "check_hostname=True", snip
                )

        return None

    @classmethod
    def apply_code_patch(
        cls,
        original_content: str,
        snippet: str,
        replacement: str,
        target_line: int | None = None,
    ) -> str:
        """
        Patch original content by replacing the target snippet with replacement code.
        Preserves indentation and handles multi-line blocks.
        """
        if not original_content:
            return replacement

        if not snippet or not snippet.strip():
            # If no snippet provided, replace line at target_line
            if target_line and target_line > 0:
                lines = original_content.splitlines(keepends=True)
                if 1 <= target_line <= len(lines):
                    old_line = lines[target_line - 1]
                    m = re.match(r"^\s*", old_line)
                    indent = m.group(0) if m else ""
                    aligned_rep = cls._align_indentation(replacement, indent)
                    lines[target_line - 1] = aligned_rep + (
                        "\n" if not aligned_rep.endswith("\n") else ""
                    )
                    return "".join(lines)
            return original_content

        clean_snippet = snippet.strip()
        clean_rep = replacement.strip()

        # Check for exact substring match
        if clean_snippet in original_content:
            occurrences = [
                m.start()
                for m in re.finditer(re.escape(clean_snippet), original_content)
            ]
            if len(occurrences) == 1 or not target_line:
                # Determine leading indentation of snippet in original text
                idx = occurrences[0]
                line_start = original_content.rfind("\n", 0, idx)
                indent = ""
                if line_start != -1:
                    prefix = original_content[line_start + 1 : idx]
                    if prefix.strip() == "":
                        indent = prefix
                aligned_rep = cls._align_indentation(clean_rep, indent)
                return (
                    original_content[:idx]
                    + aligned_rep
                    + original_content[idx + len(clean_snippet) :]
                )
            else:
                # Find occurrence closest to target_line
                lines = original_content.splitlines(keepends=True)
                line_offsets = []
                curr = 0
                for line in lines:
                    line_offsets.append(curr)
                    curr += len(line)

                best_idx = occurrences[0]
                best_dist = 999999
                for occ in occurrences:
                    # Estimate line number of occ
                    occ_line = 1
                    for l_no, offset in enumerate(line_offsets, 1):
                        if offset <= occ:
                            occ_line = l_no
                        else:
                            break
                    dist = abs(occ_line - target_line)
                    if dist < best_dist:
                        best_dist = dist
                        best_idx = occ

                line_start = original_content.rfind("\n", 0, best_idx)
                indent = ""
                if line_start != -1:
                    prefix = original_content[line_start + 1 : best_idx]
                    if prefix.strip() == "":
                        indent = prefix
                aligned_rep = cls._align_indentation(clean_rep, indent)
                return (
                    original_content[:best_idx]
                    + aligned_rep
                    + original_content[best_idx + len(clean_snippet) :]
                )

        # Whitespace-tolerant line-by-line matching
        orig_lines = original_content.splitlines(keepends=True)
        snip_lines = [
            line_item.strip()
            for line_item in clean_snippet.splitlines()
            if line_item.strip()
        ]

        if snip_lines:
            for i in range(len(orig_lines) - len(snip_lines) + 1):
                match = True
                for j, s_line in enumerate(snip_lines):
                    if orig_lines[i + j].strip() != s_line:
                        match = False
                        break
                if match:
                    m = re.match(r"^\s*", orig_lines[i])
                    indent = m.group(0) if m else ""
                    aligned_rep = cls._align_indentation(clean_rep, indent)
                    patched = (
                        orig_lines[:i]
                        + [
                            aligned_rep
                            + ("\n" if not aligned_rep.endswith("\n") else "")
                        ]
                        + orig_lines[i + len(snip_lines) :]
                    )
                    return "".join(patched)

        return original_content

    @classmethod
    def _align_indentation(cls, text: str, base_indent: str) -> str:
        """Align multiline replacement text with target base indentation."""
        lines = text.splitlines()
        if not lines:
            return ""
        # If first line has indentation, keep relative indentation
        first_indent = len(lines[0]) - len(lines[0].lstrip())
        aligned = []
        for line in lines:
            if not line.strip():
                aligned.append("")
                continue
            curr_indent = len(line) - len(line.lstrip())
            rel_indent = max(0, curr_indent - first_indent)
            aligned.append(base_indent + (" " * rel_indent) + line.lstrip())
        return "\n".join(aligned)

    PROTECTED_BRANCH_PATTERNS = [
        r"^main$",
        r"^master$",
        r"^release(?:/.*|$)",
        r"^prod(?:uction)?(?:/.*|$)",
        r"^default$",
        r"^trunk$",
    ]

    @classmethod
    def is_branch_protected(
        cls, branch: str, custom_blocked: list[str] | None = None
    ) -> bool:
        """Check if target branch is protected from direct automated remediation commits."""
        clean = (branch or "").strip()
        if not clean:
            return True
        patterns = list(cls.PROTECTED_BRANCH_PATTERNS)
        if custom_blocked:
            for b in custom_blocked:
                patterns.append("^" + b.replace("*", ".*") + "$")
        return any(re.match(p, clean, re.IGNORECASE) for p in patterns)

    @classmethod
    def apply_structured_patch(
        cls, original_content: str, patch: StructuredPatch
    ) -> str:
        """
        Apply a structured patch ensuring expected_old_text matches before patching.
        Raises PatchMismatchError if expected_old_text does not match or replacement fails.
        """
        if patch.expected_old_text not in original_content:
            orig_stripped = "\n".join(
                line_item.strip() for line_item in original_content.splitlines()
            )
            exp_stripped = "\n".join(
                line_item.strip() for line_item in patch.expected_old_text.splitlines()
            )
            if exp_stripped not in orig_stripped:
                raise PatchMismatchError(
                    f"Structured patch mismatch for '{patch.file_path}': expected code block not found in target file."
                )

        patched = cls.apply_code_patch(
            original_content=original_content,
            snippet=patch.expected_old_text,
            replacement=patch.replacement_text,
            target_line=patch.target_line,
        )
        if (
            patch.expected_old_text != patch.replacement_text
            and patched == original_content
        ):
            raise PatchMismatchError(
                f"Structured patch for '{patch.file_path}' could not be matched/applied to target content."
            )
        return patched

    @classmethod
    def _get_db_connection(cls) -> sqlite3.Connection:
        db_path = Path(os.getenv("QUEUE_DB_PATH", "webhook_jobs.db")).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS remediation_audit_trail (
                remediation_id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                pr_id TEXT DEFAULT '',
                branch TEXT NOT NULL,
                file_path TEXT NOT NULL,
                blob_sha_before TEXT NOT NULL,
                commit_sha_after TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp REAL NOT NULL,
                expected_old_text TEXT NOT NULL,
                replacement_text TEXT NOT NULL,
                status TEXT NOT NULL
            );
        """)
        conn.commit()
        return conn

    @classmethod
    def record_remediation_audit(cls, manifest: RollbackManifest):
        """Persist remediation audit record and rollback manifest to SQLite WAL."""
        with cls._get_db_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO remediation_audit_trail (
                    remediation_id, repo_id, pr_id, branch, file_path,
                    blob_sha_before, commit_sha_after, actor, timestamp,
                    expected_old_text, replacement_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
                (
                    manifest.remediation_id,
                    manifest.repo_id,
                    str(manifest.pr_id or ""),
                    manifest.branch,
                    manifest.file_path,
                    manifest.blob_sha_before,
                    manifest.commit_sha_after,
                    manifest.actor,
                    manifest.timestamp,
                    manifest.expected_old_text,
                    manifest.replacement_text,
                    manifest.status,
                ),
            )
            conn.commit()

    @classmethod
    def _restore_committed_result(cls, operation, duplicate=False):
        """Finish local bookkeeping using the acknowledged, original commit intent."""
        record = operation["result"]
        intent, receipt = record["intent"], record["receipt"]
        manifest = RollbackManifest.model_validate(
            {
                **intent["audit_draft"],
                "commit_sha_after": receipt["sha"],
                "status": "COMMITTED",
            }
        )
        cls.record_remediation_audit(manifest)
        return {
            **intent["response"],
            "commit_sha": receipt["sha"],
            "commit_url": receipt.get("html_url") or receipt.get("url", ""),
            "rollback_manifest": manifest.model_dump(),
            "message": "Remediation already applied (idempotent duplicate request)."
            if duplicate
            else "Remediation committed.",
        }

    @classmethod
    def get_remediation_manifest(cls, remediation_id: str) -> dict[str, Any] | None:
        """Retrieve remediation rollback manifest by remediation ID."""
        with cls._get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_audit_trail WHERE remediation_id = ?",
                (remediation_id,),
            ).fetchone()
            return dict(row) if row else None

    @classmethod
    def list_remediations(cls, repo_id: str | None = None) -> list[dict[str, Any]]:
        """List historical remediation manifests, optionally filtered by repository."""
        with cls._get_db_connection() as conn:
            if repo_id:
                norm_repo = SuppressionStore.normalize_repo_id(repo_id)
                rows = conn.execute(
                    "SELECT * FROM remediation_audit_trail WHERE repo_id = ? ORDER BY timestamp DESC",
                    (norm_repo,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM remediation_audit_trail ORDER BY timestamp DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    @classmethod
    def rollback_remediation(
        cls,
        remediation_id: str,
        client: GitPlatformClient | None = None,
        repo_root: str | None = None,
        *,
        job_id: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute deterministic rollback of a prior remediation:
        1. Fetch rollback manifest from audit trail
        2. Verify remediation has not already been rolled back
        3. Reverse the patch (re-applying expected_old_text)
        4. Commit the revert to the target branch
        5. Mark audit trail status as 'ROLLED_BACK'
        """
        # Rollback changes provider state as well; it must share the same explicit
        # deployment gate as forward remediation.  This prevents a disabled
        # remediation deployment from being bypassed through a direct method call.
        from code_review_agent.access_policy import require_remediation_enabled

        require_remediation_enabled()
        manifest = cls.get_remediation_manifest(remediation_id)
        if not manifest:
            return {
                "status": "error",
                "message": f"Remediation manifest '{remediation_id}' not found",
            }
        if manifest["status"] == "ROLLED_BACK":
            return {
                "status": "error",
                "message": f"Remediation '{remediation_id}' has already been rolled back",
            }

        resolved_client = client
        owner, repo = "local", "local"
        if manifest.get("repo_id") and "/" in manifest["repo_id"]:
            owner, repo = manifest["repo_id"].split("/", 1)
        if not resolved_client and manifest.get("repo_id"):
            try:
                resolved_client, _ = get_platform_client(manifest["repo_id"])
            except Exception:
                pass

        file_path = validate_diff_path(manifest["file_path"])
        branch = manifest["branch"]
        current_content = ""
        if resolved_client and hasattr(resolved_client, "fetch_file_content"):
            try:
                current_content = resolved_client.fetch_file_content(
                    owner, repo, file_path, ref=branch
                )
            except Exception as e:
                logger.warning(f"Could not fetch content for rollback: {e}")

        if not current_content and repo_root:
            loc = safe_diff_path(Path(repo_root), file_path)
            if loc.exists():
                current_content = loc.read_text(encoding="utf-8", errors="replace")

        if not current_content:
            return {
                "status": "error",
                "message": f"Cannot rollback: could not read current content for {file_path}",
            }

        # Apply reverse patch: replace replacement_text with expected_old_text
        rolled_back_content = cls.apply_code_patch(
            original_content=current_content,
            snippet=manifest["replacement_text"],
            replacement=manifest["expected_old_text"],
        )

        if (
            manifest.get("replacement_text") != manifest.get("expected_old_text")
            and rolled_back_content == current_content
        ):
            return {
                "status": "error",
                "message": f"Rollback failed: replacement text was not found in current content of '{file_path}'",
            }

        commit_message = f"revert: rollback remediation {remediation_id} on {file_path}"
        if not resolved_client:
            return {
                "status": "error",
                "message": "A provider client is required for rollback",
            }
        from code_review_agent.publication import publish
        from code_review_agent.webhook_queue import WebhookJobQueue

        queue = WebhookJobQueue()
        commit_result = publish(
            queue,
            job_id=job_id,
            claim_token=claim_token,
            key=f"rollback:{remediation_id}",
            target=manifest["repo_id"],
            payload={
                "path": file_path,
                "branch": branch,
                "content": rolled_back_content,
            },
            receipt_key="sha",
            send=lambda marker: resolved_client.commit_file_change(
                owner=owner,
                repo=repo,
                branch=branch,
                path=file_path,
                content=rolled_back_content,
                commit_message=commit_message + "\n" + marker,
            ),
        )

        with cls._get_db_connection() as conn:
            conn.execute(
                "UPDATE remediation_audit_trail SET status = 'ROLLED_BACK' WHERE remediation_id = ?",
                (remediation_id,),
            )
            conn.commit()

        return {
            "status": "success",
            "remediation_id": remediation_id,
            "rollback_commit_sha": commit_result.get("sha", ""),
            "message": f"Successfully rolled back remediation '{remediation_id}'",
        }

    @classmethod
    def apply_remediation(
        cls,
        pr_identifier: str,
        fingerprint: str,
        client: GitPlatformClient | None = None,
        custom_message: str | None = None,
        replacement_code: str | None = None,
        repo_root: str | None = None,
        reviewed_head: str | None = None,
        actor: str = "ai-code-review[bot]",
        structured_patch: StructuredPatch | None = None,
        bypass_policy: bool = False,
        *,
        job_id: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Execute full auto-remediation pipeline with Phase 5 Trust and Safety guardrails:
        1. Resolve platform client and fetch PR diff & metadata
        2. Enforce strict branch guardrails (reject main, master, release/*, prod/*)
        3. Enforce Stale Head SHA check (current_head == reviewed_head)
        4. Enforce opt-in governance policy in .code-review.yaml
        5. Verify and apply structured patch format
        6. Commit changes to PR branch
        7. Record rollback manifest and audit trail in SQLite WAL
        8. Record finding suppression in SuppressionStore
        """
        from code_review_agent.access_policy import (
            require_remediation_enabled,
            validate_pr_target,
        )

        require_remediation_enabled()
        _, identifier, host = validate_pr_target(pr_identifier)
        from code_review_agent.publication import ReconciliationRequired
        from code_review_agent.webhook_queue import WebhookJobQueue

        queue_instance = WebhookJobQueue()
        if job_id:
            queue_instance.assert_claim(job_id, claim_token)
            previous = (
                queue_instance._get_connection()
                .execute(
                    """SELECT o.operation_key, o.status FROM external_operations_log o
                JOIN external_operation_jobs j USING(operation_key)
                WHERE j.job_id=? AND o.operation_key LIKE 'remediation:%' """,
                    (job_id,),
                )
                .fetchone()
            )
            if previous:
                if previous["status"] != "SUCCESS":
                    raise ReconciliationRequired(
                        "RECONCILIATION_REQUIRED: remediation commit requires confirmation"
                    )
                return cls._restore_committed_result(
                    queue_instance.get_external_operation(previous["operation_key"]),
                    duplicate=True,
                )
        resolved_client = client
        owner, repo, pull_number = None, None, None
        branch = "feature-remediation"

        if not (owner and repo and pull_number):
            try:
                candidate_client, ident = get_platform_client(pr_identifier)
                if not resolved_client:
                    resolved_client = candidate_client
                owner, repo, pull_number = (
                    ident.owner_or_project,
                    ident.repo_or_slug,
                    ident.pr_id,
                )
            except Exception as e:
                if not resolved_client:
                    return {
                        "status": "error",
                        "message": f"Could not initialize platform client for '{pr_identifier}': {e}",
                    }

        # Fetch PR metadata
        pr_meta = None
        if owner and repo and pull_number and resolved_client:
            try:
                pr_meta = resolved_client.fetch_pull_request_metadata(
                    owner, repo, pull_number
                )
                if pr_meta and pr_meta.head_ref:
                    branch = pr_meta.head_ref
            except Exception as e:
                logger.warning(f"Could not fetch PR metadata: {e}")

        # Guardrail 1: Strict Branch Guardrails
        if cls.is_branch_protected(branch) or (
            pr_meta and pr_meta.base_ref and branch == pr_meta.base_ref
        ):
            return {
                "status": "error",
                "message": (
                    f"Strict branch guardrail violation: automated remediation commits to protected "
                    f"or default target branch '{branch}' are forbidden. Commits must target a feature or PR branch."
                ),
                "fingerprint": fingerprint,
                "branch": branch,
            }

        # Guardrail 2: Stale Head SHA Check
        if reviewed_head and pr_meta and pr_meta.head_sha:
            current_head = pr_meta.head_sha.strip().lower()
            expected_head = reviewed_head.strip().lower()
            if current_head != expected_head:
                return {
                    "status": "error",
                    "message": (
                        f"Stale Head SHA check failed: current PR head '{current_head[:8]}' "
                        f"does not match reviewed head '{expected_head[:8]}'. Remediation aborted to prevent race conditions."
                    ),
                    "fingerprint": fingerprint,
                    "current_head": current_head,
                    "reviewed_head": reviewed_head,
                }

        # Guardrail 3: Opt-in policy setting in .code-review.yaml
        env_allow_policy = os.getenv(
            "REMEDIATION_ALLOW_DIRECT_COMMITS", "false"
        ).lower() in ("true", "1")
        if not bypass_policy and not env_allow_policy:
            from code_review_agent.governance.rules_engine import RulesEngine

            rules_engine = RulesEngine(repo_root=repo_root)
            allowed, policy_msg = rules_engine.is_remediation_commit_allowed(branch)
            if not allowed:
                return {
                    "status": "error",
                    "message": f"Remediation rejected by governance policy: {policy_msg}",
                    "fingerprint": fingerprint,
                    "branch": branch,
                }

        # Fetch PR diff
        diff = ""
        if owner and repo and pull_number and resolved_client:
            try:
                diff = resolved_client.fetch_pull_request_diff(owner, repo, pull_number)
            except Exception as e:
                logger.warning(f"Could not fetch PR diff: {e}")

        # Locate finding
        finding = cls.find_target_finding(fingerprint, diff, repo_root=repo_root)
        if not finding:
            return {
                "status": "error",
                "message": f"Finding with fingerprint '{fingerprint}' was not found in the PR diff or workspace.",
            }

        # Determine replacement code
        patch_code = (
            (structured_patch.replacement_text if structured_patch else None)
            or replacement_code
            or cls.extract_remediation_code(finding.fix_recommendation, finding.snippet)
        )
        if not patch_code:
            return {
                "status": "error",
                "message": f"Could not determine automated code fix for finding '{fingerprint}' ({finding.rule_id}). Manual fix required: {finding.fix_recommendation}",
            }

        # Fetch original file content
        original_content = ""
        try:
            if resolved_client and hasattr(resolved_client, "fetch_file_content"):
                original_content = resolved_client.fetch_file_content(
                    owner=owner or "local",
                    repo=repo or "local",
                    path=finding.file_path,
                    ref=branch,
                )
        except Exception as e:
            logger.warning(f"Could not fetch remote file content: {e}")

        if not original_content and repo_root:
            local_file = safe_diff_path(Path(repo_root), finding.file_path)
            if local_file.exists():
                original_content = local_file.read_text(
                    encoding="utf-8", errors="replace"
                )

        if not original_content:
            original_content = finding.snippet

        # Blob SHA before patch
        blob_sha_before = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

        # Guardrail 4: Apply code patch with structured verification
        try:
            if structured_patch:
                patched_content = cls.apply_structured_patch(
                    original_content, structured_patch
                )
            else:
                patch_spec = StructuredPatch(
                    file_path=finding.file_path,
                    expected_old_text=finding.snippet,
                    replacement_text=patch_code,
                    target_line=finding.line_number,
                    fingerprint=finding.fingerprint,
                )
                patched_content = cls.apply_structured_patch(
                    original_content, patch_spec
                )
        except PatchMismatchError as pe:
            return {
                "status": "error",
                "message": f"Structured patch mismatch: {pe}",
                "fingerprint": finding.fingerprint,
                "file_path": finding.file_path,
            }
        except Exception:
            # Fallback to standard patch if structured format encountered edge layout
            patched_content = cls.apply_code_patch(
                original_content=original_content,
                snippet=finding.snippet,
                replacement=patch_code,
                target_line=finding.line_number,
            )

        if patched_content == original_content:
            return {
                "status": "error",
                "message": f"Remediation patch produced no changes to '{finding.file_path}'.",
                "fingerprint": finding.fingerprint,
            }

        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (Path(repo_root).name if repo_root else "default")
        )
        if not pr_meta or not isinstance(pr_meta.head_sha, str) or not pr_meta.head_sha:
            raise ValueError("Remediation requires an immutable PR head")
        queue_instance.assert_claim(job_id, claim_token)
        rem_op_key = f"remediation:{identifier.platform}:{host}:{repo_id}#{pull_number}:{pr_meta.head_sha}:{finding.fingerprint}"
        if queue_instance.is_operation_executed(rem_op_key):
            return cls._restore_committed_result(
                queue_instance.get_external_operation(rem_op_key), duplicate=True
            )

        remediation_id = f"rem_{uuid.uuid4().hex[:12]}"
        manifest = RollbackManifest(
            remediation_id=remediation_id,
            repo_id=repo_id,
            pr_id=str(pull_number),
            branch=branch,
            file_path=finding.file_path,
            blob_sha_before=blob_sha_before,
            commit_sha_after="PENDING",
            actor=actor,
            timestamp=time.time(),
            expected_old_text=(
                structured_patch.expected_old_text
                if structured_patch
                else finding.snippet
            ),
            replacement_text=patch_code,
            status="PENDING",
        )

        # Commit changes
        commit_message = custom_message or (
            f"fix({finding.category.lower()}): apply remediation for {finding.rule_id} [{finding.fingerprint}]"
        )

        # Intent precedes the remote commit. An uncertain commit requires operator
        # reconciliation; there is no automatic replay of branch mutations.
        from code_review_agent.publication import publish

        commit_result = publish(
            queue_instance,
            job_id=job_id,
            claim_token=claim_token,
            key=rem_op_key,
            target=f"{identifier.platform}:{host}:{repo_id}#{pull_number}",
            receipt_key="sha",
            payload={
                "fingerprint": finding.fingerprint,
                "file_path": finding.file_path,
                "branch": branch,
                "head_sha": pr_meta.head_sha,
                "content": patched_content,
                "audit_draft": manifest.model_dump(),
                "response": {
                    "status": "success",
                    "remediation_id": remediation_id,
                    "file_path": finding.file_path,
                    "fingerprint": finding.fingerprint,
                    "rule_id": finding.rule_id,
                    "branch": branch,
                },
            },
            send=lambda marker: resolved_client.commit_file_change(
                owner=owner or "local",
                repo=repo or "local",
                branch=branch,
                path=finding.file_path,
                content=patched_content,
                commit_message=commit_message + "\n" + marker,
            ),
        )

        commit_sha_after = commit_result.get("sha", "")
        result = cls._restore_committed_result(
            queue_instance.get_external_operation(rem_op_key)
        )

        # A committed fix is a lifecycle signal, never proof of a defect.
        try:
            from code_review_agent.calibration import (
                CONFIRMED_REAL,
                FindingOutcomeStore,
            )

            outcome_store = FindingOutcomeStore()
            outcome_store.update_outcome(
                repo_id=repo_id,
                fingerprint=finding.fingerprint,
                outcome=CONFIRMED_REAL,
                source="apply_remediation",
                pr_id=str(pull_number),
                actor=actor,
                reason=f"Remediation committed via 1-click /apply: {commit_sha_after[:8]}",
                category=getattr(finding, "category", "QUALITY"),
            )
        except Exception as e:
            logger.warning(f"Could not record outcome after remediation: {e}")

        return result
