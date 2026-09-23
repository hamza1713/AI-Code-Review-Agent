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
from typing import Any, Dict, List, Optional, Tuple

from code_review_agent.config import logger
from code_review_agent.models import SastFinding, StructuredPatch, RollbackManifest
from code_review_agent.platform import get_platform_client
from code_review_agent.platform.base import GitPlatformClient
from code_review_agent.suppression_store import SuppressionStore
from code_review_agent.tools.ast_security_scanner import AstSecurityScanner
from code_review_agent.tools.sast_scanner import SastEngine


class BranchGuardrailViolation(Exception):
    """Raised when an automated remediation targets a protected branch."""
    pass


class PatchMismatchError(Exception):
    """Raised when structured patch expected old text does not match target file."""
    pass


class StaleHeadShaError(Exception):
    """Raised when PR head SHA has advanced beyond the reviewed commit."""
    pass


class AutoRemediator:
    """
    Automates 1-click remediation commits directly to PR branches.
    Enforces branch guardrails, stale head SHA verification, structured AST patches,
    and persistent rollback manifests.
    """

    @classmethod
    def find_target_finding(
        cls,
        fingerprint: str,
        diff: str,
        repo_root: Optional[str] = None
    ) -> Optional[SastFinding]:
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
                repo_findings = SastEngine().scan_directory(repo_root)
                for f in repo_findings:
                    fp = (f.fingerprint or "").lower()
                    if fp == clean_fp or (len(clean_fp) >= 6 and fp.startswith(clean_fp)):
                        return f
            except Exception as e:
                logger.warning(f"Error scanning repo_root with SastEngine: {e}")

        return None

    @classmethod
    def extract_remediation_code(cls, fix_recommendation: str, snippet: str = "") -> Optional[str]:
        """
        Extract raw replacement code from fix recommendation text or suggestion blocks.
        """
        if not fix_recommendation:
            return None

        clean = fix_recommendation.strip()

        # Check for fenced code blocks (```suggestion, ```python, or ```)
        fence_match = re.search(r"```(?:suggestion|python)?\s*\n(.*?)\n```", clean, re.DOTALL)
        if fence_match:
            return fence_match.group(1).rstrip()

        # Check for "Replace with: `code`" or "Use: `code`"
        inline_code_match = re.search(r"(?:replace with|use|instead of [^:`]+):?\s*`([^`]+)`", clean, re.IGNORECASE)
        if inline_code_match:
            return inline_code_match.group(1).strip()

        # Check for lines after "Example:" or "Fix:"
        example_match = re.search(r"(?:example|fix|remediation):\s*\n([^\n]+)", clean, re.IGNORECASE)
        if example_match:
            candidate = example_match.group(1).strip()
            if not candidate.startswith("-") and not candidate.startswith("*"):
                return candidate

        # Fallback to single backticked code
        single_backtick = re.search(r"`([^`]+)`", clean)
        if single_backtick:
            return single_backtick.group(1).strip()

        return None

    @classmethod
    def apply_code_patch(
        cls,
        original_content: str,
        snippet: str,
        replacement: str,
        target_line: Optional[int] = None
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
                    indent = re.match(r"^\s*", old_line).group(0)
                    aligned_rep = cls._align_indentation(replacement, indent)
                    lines[target_line - 1] = aligned_rep + ("\n" if not aligned_rep.endswith("\n") else "")
                    return "".join(lines)
            return original_content

        clean_snippet = snippet.strip()
        clean_rep = replacement.strip()

        # Check for exact substring match
        if clean_snippet in original_content:
            occurrences = [m.start() for m in re.finditer(re.escape(clean_snippet), original_content)]
            if len(occurrences) == 1 or not target_line:
                # Determine leading indentation of snippet in original text
                idx = occurrences[0]
                line_start = original_content.rfind("\n", 0, idx)
                indent = ""
                if line_start != -1:
                    prefix = original_content[line_start + 1:idx]
                    if prefix.strip() == "":
                        indent = prefix
                aligned_rep = cls._align_indentation(clean_rep, indent)
                return original_content[:idx] + aligned_rep + original_content[idx + len(clean_snippet):]
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
                    prefix = original_content[line_start + 1:best_idx]
                    if prefix.strip() == "":
                        indent = prefix
                aligned_rep = cls._align_indentation(clean_rep, indent)
                return original_content[:best_idx] + aligned_rep + original_content[best_idx + len(clean_snippet):]

        # Whitespace-tolerant line-by-line matching
        orig_lines = original_content.splitlines(keepends=True)
        snip_lines = [l.strip() for l in clean_snippet.splitlines() if l.strip()]

        if snip_lines:
            for i in range(len(orig_lines) - len(snip_lines) + 1):
                match = True
                for j, s_line in enumerate(snip_lines):
                    if orig_lines[i + j].strip() != s_line:
                        match = False
                        break
                if match:
                    indent = re.match(r"^\s*", orig_lines[i]).group(0)
                    aligned_rep = cls._align_indentation(clean_rep, indent)
                    patched = (
                        orig_lines[:i] +
                        [aligned_rep + ("\n" if not aligned_rep.endswith("\n") else "")] +
                        orig_lines[i + len(snip_lines):]
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
    def is_branch_protected(cls, branch: str, custom_blocked: Optional[List[str]] = None) -> bool:
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
        cls,
        original_content: str,
        patch: StructuredPatch
    ) -> str:
        """
        Apply a structured patch ensuring expected_old_text matches before patching.
        Raises PatchMismatchError if expected_old_text does not match or replacement fails.
        """
        if patch.expected_old_text not in original_content:
            orig_stripped = "\n".join(l.strip() for l in original_content.splitlines())
            exp_stripped = "\n".join(l.strip() for l in patch.expected_old_text.splitlines())
            if exp_stripped not in orig_stripped:
                raise PatchMismatchError(
                    f"Structured patch mismatch for '{patch.file_path}': expected code block not found in target file."
                )

        patched = cls.apply_code_patch(
            original_content=original_content,
            snippet=patch.expected_old_text,
            replacement=patch.replacement_text,
            target_line=patch.target_line
        )
        if patch.expected_old_text != patch.replacement_text and patched == original_content:
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
            conn.execute("""
                INSERT INTO remediation_audit_trail (
                    remediation_id, repo_id, pr_id, branch, file_path,
                    blob_sha_before, commit_sha_after, actor, timestamp,
                    expected_old_text, replacement_text, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                manifest.remediation_id, manifest.repo_id, str(manifest.pr_id or ""),
                manifest.branch, manifest.file_path, manifest.blob_sha_before,
                manifest.commit_sha_after, manifest.actor, manifest.timestamp,
                manifest.expected_old_text, manifest.replacement_text, manifest.status
            ))
            conn.commit()

    @classmethod
    def get_remediation_manifest(cls, remediation_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve remediation rollback manifest by remediation ID."""
        with cls._get_db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_audit_trail WHERE remediation_id = ?",
                (remediation_id,)
            ).fetchone()
            return dict(row) if row else None

    @classmethod
    def list_remediations(cls, repo_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List historical remediation manifests, optionally filtered by repository."""
        with cls._get_db_connection() as conn:
            if repo_id:
                norm_repo = SuppressionStore.normalize_repo_id(repo_id)
                rows = conn.execute(
                    "SELECT * FROM remediation_audit_trail WHERE repo_id = ? ORDER BY timestamp DESC",
                    (norm_repo,)
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
        client: Optional[GitPlatformClient] = None,
        repo_root: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute deterministic rollback of a prior remediation:
        1. Fetch rollback manifest from audit trail
        2. Verify remediation has not already been rolled back
        3. Reverse the patch (re-applying expected_old_text)
        4. Commit the revert to the target branch
        5. Mark audit trail status as 'ROLLED_BACK'
        """
        manifest = cls.get_remediation_manifest(remediation_id)
        if not manifest:
            return {"status": "error", "message": f"Remediation manifest '{remediation_id}' not found"}
        if manifest["status"] == "ROLLED_BACK":
            return {"status": "error", "message": f"Remediation '{remediation_id}' has already been rolled back"}

        resolved_client = client
        owner, repo = "local", "local"
        if manifest.get("repo_id") and "/" in manifest["repo_id"]:
            owner, repo = manifest["repo_id"].split("/", 1)
        if not resolved_client and manifest.get("repo_id"):
            try:
                resolved_client, _ = get_platform_client(manifest["repo_id"])
            except Exception:
                pass

        file_path = manifest["file_path"]
        branch = manifest["branch"]
        current_content = ""
        if resolved_client and hasattr(resolved_client, "fetch_file_content"):
            try:
                current_content = resolved_client.fetch_file_content(owner, repo, file_path, ref=branch)
            except Exception as e:
                logger.warning(f"Could not fetch content for rollback: {e}")

        if not current_content and repo_root:
            loc = Path(repo_root) / file_path
            if loc.exists():
                current_content = loc.read_text(encoding="utf-8", errors="replace")

        if not current_content:
            return {"status": "error", "message": f"Cannot rollback: could not read current content for {file_path}"}

        # Apply reverse patch: replace replacement_text with expected_old_text
        rolled_back_content = cls.apply_code_patch(
            original_content=current_content,
            snippet=manifest["replacement_text"],
            replacement=manifest["expected_old_text"]
        )

        if manifest.get("replacement_text") != manifest.get("expected_old_text") and rolled_back_content == current_content:
            return {
                "status": "error",
                "message": f"Rollback failed: replacement text was not found in current content of '{file_path}'"
            }

        commit_message = f"revert: rollback remediation {remediation_id} on {file_path}"
        commit_result = resolved_client.commit_file_change(
            owner=owner,
            repo=repo,
            branch=branch,
            path=file_path,
            content=rolled_back_content,
            commit_message=commit_message
        ) if resolved_client else {"sha": f"rollback-{uuid.uuid4().hex[:8]}"}

        with cls._get_db_connection() as conn:
            conn.execute(
                "UPDATE remediation_audit_trail SET status = 'ROLLED_BACK' WHERE remediation_id = ?",
                (remediation_id,)
            )
            conn.commit()

        return {
            "status": "success",
            "remediation_id": remediation_id,
            "rollback_commit_sha": commit_result.get("sha", ""),
            "message": f"Successfully rolled back remediation '{remediation_id}'"
        }

    @classmethod
    def apply_remediation(
        cls,
        pr_identifier: str,
        fingerprint: str,
        client: Optional[GitPlatformClient] = None,
        custom_message: Optional[str] = None,
        replacement_code: Optional[str] = None,
        repo_root: Optional[str] = None,
        reviewed_head: Optional[str] = None,
        actor: str = "ai-code-review[bot]",
        structured_patch: Optional[StructuredPatch] = None,
        bypass_policy: bool = False
    ) -> Dict[str, Any]:
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
        resolved_client = client
        owner, repo, pull_number = None, None, None
        branch = "feature-remediation"

        if not (owner and repo and pull_number):
            try:
                candidate_client, ident = get_platform_client(pr_identifier)
                if not resolved_client:
                    resolved_client = candidate_client
                owner, repo, pull_number = ident.owner_or_project, ident.repo_or_slug, ident.pr_id
            except Exception as e:
                if not resolved_client:
                    return {"status": "error", "message": f"Could not initialize platform client for '{pr_identifier}': {e}"}

        # Fetch PR metadata
        pr_meta = None
        if owner and repo and pull_number and resolved_client:
            try:
                pr_meta = resolved_client.fetch_pull_request_metadata(owner, repo, pull_number)
                if pr_meta and pr_meta.head_ref:
                    branch = pr_meta.head_ref
            except Exception as e:
                logger.warning(f"Could not fetch PR metadata: {e}")

        # Guardrail 1: Strict Branch Guardrails
        if cls.is_branch_protected(branch) or (pr_meta and pr_meta.base_ref and branch == pr_meta.base_ref):
            return {
                "status": "error",
                "message": (
                    f"Strict branch guardrail violation: automated remediation commits to protected "
                    f"or default target branch '{branch}' are forbidden. Commits must target a feature or PR branch."
                ),
                "fingerprint": fingerprint,
                "branch": branch
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
                    "reviewed_head": reviewed_head
                }

        # Guardrail 3: Opt-in policy setting in .code-review.yaml
        env_allow_policy = os.getenv("REMEDIATION_ALLOW_DIRECT_COMMITS", "false").lower() in ("true", "1")
        if not bypass_policy and not env_allow_policy:
            from code_review_agent.governance.rules_engine import RulesEngine
            rules_engine = RulesEngine(repo_root=repo_root)
            allowed, policy_msg = rules_engine.is_remediation_commit_allowed(branch)
            if not allowed:
                return {
                    "status": "error",
                    "message": f"Remediation rejected by governance policy: {policy_msg}",
                    "fingerprint": fingerprint,
                    "branch": branch
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
                "message": f"Finding with fingerprint '{fingerprint}' was not found in the PR diff or workspace."
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
                "message": f"Could not determine automated code fix for finding '{fingerprint}' ({finding.rule_id}). Manual fix required: {finding.fix_recommendation}"
            }

        # Fetch original file content
        original_content = ""
        try:
            if resolved_client and hasattr(resolved_client, "fetch_file_content"):
                original_content = resolved_client.fetch_file_content(
                    owner=owner or "local",
                    repo=repo or "local",
                    path=finding.file_path,
                    ref=branch
                )
        except Exception as e:
            logger.warning(f"Could not fetch remote file content: {e}")

        if not original_content and repo_root:
            local_file = Path(repo_root) / finding.file_path
            if local_file.exists():
                original_content = local_file.read_text(encoding="utf-8", errors="replace")

        if not original_content:
            original_content = finding.snippet

        # Blob SHA before patch
        blob_sha_before = hashlib.sha256(original_content.encode("utf-8")).hexdigest()

        # Guardrail 4: Apply code patch with structured verification
        try:
            if structured_patch:
                patched_content = cls.apply_structured_patch(original_content, structured_patch)
            else:
                patch_spec = StructuredPatch(
                    file_path=finding.file_path,
                    expected_old_text=finding.snippet,
                    replacement_text=patch_code,
                    target_line=finding.line_number,
                    fingerprint=finding.fingerprint
                )
                patched_content = cls.apply_structured_patch(original_content, patch_spec)
        except PatchMismatchError as pe:
            return {
                "status": "error",
                "message": f"Structured patch mismatch: {pe}",
                "fingerprint": finding.fingerprint,
                "file_path": finding.file_path
            }
        except Exception:
            # Fallback to standard patch if structured format encountered edge layout
            patched_content = cls.apply_code_patch(
                original_content=original_content,
                snippet=finding.snippet,
                replacement=patch_code,
                target_line=finding.line_number
            )

        if patched_content == original_content:
            return {
                "status": "error",
                "message": f"Remediation patch produced no changes to '{finding.file_path}'.",
                "fingerprint": finding.fingerprint
            }

        repo_id = f"{owner}/{repo}" if owner and repo else (Path(repo_root).name if repo_root else "default")
        from code_review_agent.webhook_queue import WebhookJobQueue
        queue_instance = WebhookJobQueue()
        rem_op_key = f"remediation:{repo_id}#{pull_number or branch}:{finding.fingerprint}"
        if queue_instance.is_operation_executed(rem_op_key):
            existing_op = queue_instance.get_external_operation(rem_op_key)
            result_data = {}
            if existing_op and existing_op.get("result_json"):
                try:
                    result_data = json.loads(existing_op["result_json"])
                except Exception:
                    pass
            return {
                "status": "success",
                "remediation_id": f"rem_idempotent_{finding.fingerprint[:8]}",
                "commit_sha": result_data.get("sha", ""),
                "commit_url": "",
                "file_path": finding.file_path,
                "fingerprint": finding.fingerprint,
                "rule_id": finding.rule_id,
                "branch": branch,
                "message": "Remediation already applied (idempotent duplicate request)."
            }

        # Commit changes
        commit_message = custom_message or (
            f"fix({finding.category.lower()}): apply remediation for {finding.rule_id} [{finding.fingerprint}]"
        )

        try:
            commit_result = resolved_client.commit_file_change(
                owner=owner or "local",
                repo=repo or "local",
                branch=branch,
                path=finding.file_path,
                content=patched_content,
                commit_message=commit_message
            )
        except Exception as e:
            logger.error(f"Commit file change failed: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Failed to commit remediation to branch '{branch}': {e}",
                "fingerprint": finding.fingerprint,
                "rule_id": finding.rule_id
            }

        commit_sha_after = commit_result.get("sha", "")
        remediation_id = f"rem_{uuid.uuid4().hex[:12]}"

        queue_instance.record_external_operation(
            operation_key=rem_op_key,
            operation_type="apply_remediation",
            target_id=f"{repo_id}#{pull_number or branch}",
            payload={"fingerprint": finding.fingerprint, "file_path": finding.file_path, "branch": branch},
            status="SUCCESS",
            result={"sha": commit_sha_after}
        )

        expected_old_text = (
            (structured_patch.expected_old_text if structured_patch else None)
            or finding.snippet
        )

        # Guardrail 5: Record rollback manifest and audit trail
        manifest = RollbackManifest(
            remediation_id=remediation_id,
            repo_id=repo_id,
            pr_id=str(pull_number or ""),
            branch=branch,
            file_path=finding.file_path,
            blob_sha_before=blob_sha_before,
            commit_sha_after=commit_sha_after,
            actor=actor,
            timestamp=time.time(),
            expected_old_text=expected_old_text,
            replacement_text=patch_code,
            status="COMMITTED"
        )
        try:
            cls.record_remediation_audit(manifest)
        except Exception as e:
            logger.warning(f"Could not persist remediation audit record: {e}")

        # Mark finding as suppressed/resolved in SuppressionStore
        try:
            store = SuppressionStore()
            store.suppress(
                repo_id=repo_id,
                fingerprint=finding.fingerprint,
                reason=f"Auto-remediated via 1-click fix: {commit_sha_after[:8]} ({remediation_id})",
                author=actor,
                pr_id=str(pull_number or "")
            )
        except Exception as e:
            logger.warning(f"Could not record suppression after remediation: {e}")

        return {
            "status": "success",
            "remediation_id": remediation_id,
            "commit_sha": commit_sha_after,
            "commit_url": commit_result.get("html_url") or commit_result.get("url", ""),
            "file_path": finding.file_path,
            "fingerprint": finding.fingerprint,
            "rule_id": finding.rule_id,
            "branch": branch,
            "rollback_manifest": manifest.model_dump()
        }
