"""
Synchronous Review Service for In-Browser and Direct API Code Review.
Handles diff/file/zip ingestion, resource limits, zip security validation,
non-Python language detection, IP rate limiting, and structured response assembly.
"""

import io
import os
import re
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Literal

from code_review_agent.config import logger
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.diff_parser import DiffParser
from code_review_agent.github_client import GitHubClient
from code_review_agent.main import PRCodeReviewFlow
from code_review_agent.models import (
    CrossFileImpactSummary,
    ReviewAPIResponse,
    ReviewState,
)

# Resource Limits
MAX_ZIP_BYTES = 2 * 1024 * 1024  # 2 MB max uploaded zip size
MAX_ZIP_FILES = 20  # Max 20 files inside archive
MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024  # 5 MB max total uncompressed size
MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024  # 2 MB max single file upload
MAX_DIFF_CHARS = 500_000  # ~500 KB raw diff limit


class RateLimitExceeded(Exception):
    """Raised when client IP exceeds rate limit."""

    def __init__(self, retry_after: int = 60):
        super().__init__(f"Rate limit exceeded. Try again in {retry_after} seconds.")
        self.retry_after = retry_after


class InputValidationError(Exception):
    """Raised when uploaded file or zip exceeds resource limits or fails security checks."""


class InMemoryRateLimiter:
    """
    Thread-safe sliding-window rate limiter per client IP address.
    Configurable request count within a time window (default: 30 req/min).
    Bounded to MAX_TRACKED_IPS with LRU eviction to prevent unbounded memory growth.
    """

    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: int = 60,
        max_tracked_ips: int = 10_000,
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_tracked_ips = max_tracked_ips
        self._ip_history: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def check_rate_limit(self, client_ip: str) -> None:
        """Check if request from client_ip is allowed; raises RateLimitExceeded if not."""
        now = time.time()
        window_start = now - self.window_seconds

        with self._lock:
            if client_ip in self._ip_history:
                self._ip_history.move_to_end(client_ip)
                timestamps = self._ip_history[client_ip]
            else:
                # Enforce capacity bound before inserting new IP
                if len(self._ip_history) >= self.max_tracked_ips:
                    self._ip_history.popitem(last=False)
                timestamps = deque()
                self._ip_history[client_ip] = timestamps

            # Evict timestamps outside sliding window
            while timestamps and timestamps[0] < window_start:
                timestamps.popleft()

            if len(timestamps) >= self.max_requests:
                oldest = timestamps[0]
                retry_after = max(1, int(self.window_seconds - (now - oldest)))
                logger.warning(
                    f"⚠️ Rate limit exceeded for IP '{client_ip}'. Retry after {retry_after}s."
                )
                raise RateLimitExceeded(retry_after=retry_after)

            timestamps.append(now)

    def reset(self):
        """Reset rate limiter state (useful for tests)."""
        with self._lock:
            self._ip_history.clear()


# Global rate limiter instance
rate_limiter = InMemoryRateLimiter(max_requests=30, window_seconds=60)


class ReviewService:
    """Orchestrates input validation, diff conversion, and synchronous Flow execution."""

    @staticmethod
    def file_to_unified_diff(filename: str, content: str) -> str:
        """
        Convert raw file text into a standard unified git diff format.
        If content is already a git diff, returns it as-is.
        """
        clean_content = content.strip()
        if clean_content.startswith(("diff --git", "--- ")):
            return content

        from code_review_agent.path_validation import validate_diff_path

        clean_filename = validate_diff_path(filename)
        lines = content.splitlines()
        num_lines = len(lines)

        diff_lines = [
            f"diff --git a/{clean_filename} b/{clean_filename}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{clean_filename}",
            f"@@ -0,0 +{1 if lines else 0},{num_lines} @@",
        ]
        for line in lines:
            diff_lines.append(f"+{line}")

        return "\n".join(diff_lines)

    @staticmethod
    def validate_and_extract_zip(zip_bytes: bytes, target_dir: Path) -> list[Path]:
        """
        Safely validate and extract a zip archive.
        Enforces max size, max file count, uncompressed limit, and path traversal guards.
        """
        if len(zip_bytes) > MAX_ZIP_BYTES:
            raise InputValidationError(
                f"Zip file exceeds maximum size limit of {MAX_ZIP_BYTES // (1024 * 1024)}MB "
                f"(received {len(zip_bytes) / (1024 * 1024):.2f}MB)."
            )

        try:
            zip_buffer = io.BytesIO(zip_bytes)
            with zipfile.ZipFile(zip_buffer, "r") as zf:
                infolist = zf.infolist()

                # Filter out directories
                file_members = [m for m in infolist if not m.is_dir()]

                if len(file_members) > MAX_ZIP_FILES:
                    raise InputValidationError(
                        f"Zip contains {len(file_members)} files, which exceeds maximum limit of {MAX_ZIP_FILES} files."
                    )

                if len(file_members) == 0:
                    raise InputValidationError("Uploaded zip archive is empty.")

                total_uncompressed = sum(m.file_size for m in file_members)
                if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                    raise InputValidationError(
                        f"Uncompressed zip content ({total_uncompressed / (1024 * 1024):.2f}MB) "
                        f"exceeds safety limit of {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB."
                    )

                extracted_files: list[Path] = []
                from code_review_agent.path_validation import safe_diff_path

                # Validate the entire archive before writing any member.
                destinations = [
                    (member, safe_diff_path(target_dir, member.filename))
                    for member in file_members
                ]

                if len(destinations) != len(
                    {
                        member.filename.replace("\\", "/").casefold()
                        for member, _ in destinations
                    }
                ):
                    raise InputValidationError("Zip repeats a portable source path")
                for member, dest_file in destinations:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest_file, "wb") as dst:
                        dst.write(src.read())

                    extracted_files.append(dest_file)

                return extracted_files

        except zipfile.BadZipFile as e:
            raise InputValidationError(
                f"Invalid or corrupted zip archive: {str(e)}"
            ) from e

    @staticmethod
    def directory_to_unified_diff(dir_path: Path) -> str:
        """Convert all files in a directory into a combined unified git diff using StringIO streaming."""
        buf = io.StringIO()
        first = True
        for root, _, files in os.walk(dir_path):
            for file in files:
                file_path = Path(root) / file
                try:
                    rel_path = str(file_path.relative_to(dir_path)).replace("\\", "/")
                except ValueError:
                    rel_path = file
                try:
                    from code_review_agent.review_contracts import SourceFile

                    content = SourceFile.from_bytes(
                        rel_path, file_path.read_bytes(), "UPLOAD"
                    ).text
                    chunk = ReviewService.file_to_unified_diff(rel_path, content)
                    if not first:
                        buf.write("\n\n")
                    buf.write(chunk)
                    first = False
                except Exception as e:
                    raise InputValidationError(
                        f"Cannot decode uploaded source {rel_path}"
                    ) from e

        return buf.getvalue()

    @staticmethod
    def is_python_diff(parsed_pr: Any, raw_diff: str) -> bool:
        """Check if modified files in the diff are Python source files."""
        if parsed_pr and parsed_pr.files:
            return any(
                f.target_file.endswith(".py") or f.source_file.endswith(".py")
                for f in parsed_pr.files
            )
        # Fallback check on diff headers
        return bool(re.search(r"\+\+\+ b/.*?\.py\b", raw_diff))

    @staticmethod
    def extract_verdict_and_confidence(state: ReviewState) -> tuple[str, int, str, str]:
        """
        Synthesize structured verdict, confidence score, executive summary, and complete report from flow state.
        Ensures consistent, honest verdict values ('APPROVE', 'REQUEST CHANGES', 'ESCALATE').
        """
        final_answer = state.final_answer or ""
        review_result = state.review_result or {}

        # 1. Determine Confidence
        confidence = 80
        if "confidence" in review_result:
            try:
                confidence = int(review_result["confidence"])
            except Exception:
                confidence = 80
        else:
            # Extract from markdown e.g. **Confidence Score**: 85/100
            conf_match = re.search(
                r"confidence(?:\s*score)?[:\s*]+(\d+)", final_answer, re.IGNORECASE
            )
            if conf_match:
                try:
                    confidence = int(conf_match.group(1))
                except Exception:
                    pass

        # Clamp confidence to 0-100
        confidence = max(0, min(100, confidence))

        # 2. Determine Verdict
        if "verdict" in review_result and review_result["verdict"]:
            verdict = str(review_result["verdict"]).upper()
            if "REQUEST" in verdict:
                verdict = "REQUEST CHANGES"
        else:
            final_upper = final_answer[:400].upper()
            if "ESCALATE" in final_upper:
                verdict = "ESCALATE"
            elif "REQUEST CHANGES" in final_upper or "REQUEST_CHANGES" in final_upper:
                verdict = "REQUEST CHANGES"
            elif "APPROVE" in final_upper:
                verdict = "APPROVE"
            else:
                has_critical_sast = any(
                    f.severity in ["CRITICAL", "HIGH"] for f in state.sast_findings
                )
                has_blocking_rules = any(
                    r.severity == "BLOCKING" for r in state.rule_violations
                )
                if has_critical_sast or has_blocking_rules:
                    verdict = "ESCALATE"
                elif state.sast_findings or state.rule_violations:
                    verdict = "REQUEST CHANGES"
                else:
                    verdict = "APPROVE"

        # 3. Summary and Full Report
        full_report = (
            final_answer
            or review_result.get("findings")
            or "Review completed successfully."
        )
        summary = (
            review_result.get("findings")
            or final_answer
            or "Review completed successfully."
        )

        return verdict, confidence, summary, full_report

    @classmethod
    def execute_review(
        cls,
        raw_diff: str | None = None,
        file_name: str | None = None,
        file_bytes: bytes | None = None,
        zip_bytes: bytes | None = None,
        pr_url: str | None = None,
        repo_root: str | None = None,
        client_ip: str = "127.0.0.1",
        bypass_limits: bool = False,
        rate_limit_checked: bool = False,
        input_snapshot: dict | None = None,
        analysis_mode: Literal["deterministic", "model", "combined"] = "combined",
        evaluation_mode: bool = False,
    ) -> ReviewAPIResponse:
        """
        Execute synchronous in-browser review for raw diff, uploaded file, uploaded zip, or live GitHub PR.
        Enforces resource limits, rate limiting, and returns structured ReviewAPIResponse.

        `repo_root`, when provided (e.g. the bot's temporary PR checkout), is used as the
        working tree for RAG/AST/governance context. Zip and single-file uploads override
        it with their own extracted temp directory.

        `bypass_limits` skips per-IP rate limiting and the pasted-diff size cap. It is for
        trusted internal callers only — the durable webhook worker, whose diffs come from
        an authenticated platform fetch, not untrusted user input — so a busy queue of
        real PRs is never throttled as if it were one abusive client.
        """
        # 1. Rate Limiting Check (skipped for trusted internal callers)
        if not bypass_limits and not rate_limit_checked:
            rate_limiter.check_rate_limit(client_ip)

        # 2. Ingest and normalize inputs into unified diff and optional repo_root
        temp_dir_obj: tempfile.TemporaryDirectory | None = None
        final_diff = ""

        try:
            if pr_url:
                # Handle Live GitHub PR URL
                clean_pr_url = pr_url.strip()
                owner, repo, pull_number = GitHubClient.parse_pr_identifier(
                    clean_pr_url
                )
                gh_client = GitHubClient()
                meta = gh_client.fetch_pull_request_metadata(owner, repo, pull_number)
                final_diff = gh_client.fetch_pull_request_diff(owner, repo, pull_number)
                if (
                    gh_client.fetch_pull_request_metadata(owner, repo, pull_number)[
                        "head_sha"
                    ]
                    != meta["head_sha"]
                ):
                    raise InputValidationError("PR head changed while retrieving diff")
                from code_review_agent.source_snapshot import build_snapshot

                source_repo = meta.get("head_repository") or f"{owner}/{repo}"
                source_owner, source_name = source_repo.split("/", 1)
                input_snapshot = build_snapshot(
                    final_diff,
                    meta["head_sha"],
                    f"github:github.com/{owner}/{repo}",
                    fetch=lambda path: gh_client.fetch_source_file(
                        source_owner, source_name, path, meta["head_sha"]
                    ),
                ).model_dump()
                logger.info(
                    f"🌐 Fetched live GitHub PR diff for {owner}/{repo}#{pull_number} ({len(final_diff)} bytes)"
                )

            elif zip_bytes:
                # Handle Zip Upload
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="code_review_zip_")
                temp_path = Path(temp_dir_obj.name)
                cls.validate_and_extract_zip(zip_bytes, temp_path)
                final_diff = cls.directory_to_unified_diff(temp_path)
                repo_root = str(temp_path)
                logger.info(
                    f"📦 Extracted and indexed zip review archive at {repo_root}"
                )

            elif file_bytes is not None and file_name:
                # Handle Single File Upload
                if len(file_bytes) > MAX_SINGLE_FILE_BYTES:
                    raise InputValidationError(
                        f"Uploaded file exceeds {MAX_SINGLE_FILE_BYTES // (1024 * 1024)}MB limit."
                    )
                from code_review_agent.sandbox.test_runner import safe_diff_path

                if any(character in file_name for character in ("\n", "\r", "\x00")):
                    raise InputValidationError("Invalid source filename.")
                normalized_name = file_name.replace("\\", "/").split("/")[-1]
                try:
                    safe_diff_path(Path(tempfile.gettempdir()), normalized_name)
                except ValueError:
                    raise InputValidationError("Invalid source filename.") from None
                from code_review_agent.review_contracts import SourceFile

                try:
                    content = SourceFile.from_bytes(
                        normalized_name, file_bytes, "UPLOAD"
                    ).text
                except (ValueError, SyntaxError, UnicodeError, LookupError) as error:
                    raise InputValidationError(
                        "Uploaded file is not valid text source"
                    ) from error
                file_name = normalized_name
                final_diff = cls.file_to_unified_diff(file_name, content)

                # Create temp file so AST indexer can index it if it is Python
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="code_review_file_")
                temp_path = Path(temp_dir_obj.name)
                dest_file = temp_path / Path(file_name).name
                dest_file.write_bytes(file_bytes)
                repo_root = str(temp_path)

            elif raw_diff:
                # Handle Pasted Raw Diff
                if not bypass_limits and len(raw_diff) > MAX_DIFF_CHARS:
                    raise InputValidationError(
                        f"Pasted diff length ({len(raw_diff)} chars) exceeds maximum allowable limit of {MAX_DIFF_CHARS} chars."
                    )
                clean_raw = raw_diff.strip()
                if (
                    not clean_raw.startswith("diff --git")
                    and not clean_raw.startswith("--- ")
                    and "@@ " not in clean_raw
                ):
                    final_diff = cls.file_to_unified_diff("source_file.py", clean_raw)
                else:
                    final_diff = raw_diff

            else:
                raise InputValidationError(
                    "No review content provided. Provide 'raw_diff', 'file', 'zip_file', or 'pr_url'."
                )

            if not final_diff.strip():
                raise InputValidationError(
                    "Supplied input contains no code or parseable diff content."
                )

            # 3. Check language (Python vs non-Python)
            parsed_pr = DiffParser.parse_diff(final_diff)
            is_python = cls.is_python_diff(parsed_pr, final_diff)

            # 4. Instantiate and execute PRCodeReviewFlow
            from code_review_agent.review_contracts import ReviewInputSnapshot
            from code_review_agent.source_snapshot import build_snapshot

            snapshot = (
                ReviewInputSnapshot.model_validate(input_snapshot)
                if input_snapshot
                else build_snapshot(
                    final_diff, root=repo_root, trusted_upload=temp_dir_obj is not None
                )
            )
            if snapshot.diff != final_diff:
                raise InputValidationError("Input snapshot does not match review diff")
            from code_review_agent.source_snapshot import verified_checkout

            if (
                repo_root
                and temp_dir_obj is None
                and not verified_checkout(repo_root, snapshot.analyzed_commit)
            ):
                repo_root = None
            if evaluation_mode:
                if temp_dir_obj is not None:
                    temp_dir_obj.cleanup()
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="frozen_evaluation_")
                repo_root = temp_dir_obj.name
                from code_review_agent.path_validation import safe_diff_path

                for source in snapshot.files:
                    target = safe_diff_path(Path(repo_root), source.path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.raw)
                policy = Path(__file__).resolve().parents[2] / ".code-review.yaml"
                if policy.is_file():
                    (Path(repo_root) / ".code-review.yaml").write_bytes(
                        policy.read_bytes()
                    )
            if repo_root is None:
                # Never index the server checkout when source context is absent.
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="review_context_")
                repo_root = temp_dir_obj.name
                from code_review_agent.path_validation import safe_diff_path

                for source in snapshot.files:
                    target = safe_diff_path(Path(repo_root), source.path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.raw)
            flow = PRCodeReviewFlow(tracing=False)
            flow.state.input_snapshot = snapshot
            flow.state.analysis_mode = analysis_mode
            flow.state.evaluation_mode = evaluation_mode
            flow.state.pr_content = final_diff
            flow.state.pr_file_path = ""
            flow.state.pr_url = None
            if repo_root:
                flow.state.repo_root = repo_root
            flow.state.context_verified = True

            import uuid

            flow_id = f"web_review_{uuid.uuid4().hex}"
            review_started = time.time()
            review_clock = time.monotonic()
            from code_review_agent.data_policy import repository_scope

            if snapshot.repository:
                with repository_scope(snapshot.repository):
                    flow.kickoff(inputs={"id": flow_id})
            else:
                flow.kickoff(inputs={"id": flow_id})
            review_ended = time.time()
            review_duration_ms = (time.monotonic() - review_clock) * 1000

            # 5. Build Cross-File Impact Information
            impact_callers: dict[str, list[str]] = {}
            if is_python:
                try:
                    indexer = CodeGraphIndexer(repo_root=repo_root)
                    indexer.index_repository()
                    indexer.index_diff(final_diff)
                    # Find all modified function callers
                    target_funcs = []
                    for fd in (
                        flow.state.parsed_pr.files if flow.state.parsed_pr else []
                    ):
                        for (
                            _line_no,
                            line_content,
                        ) in DiffParser.extract_added_lines_with_numbers(fd):
                            if "def " in line_content:
                                parts = line_content.split("def ", 1)[1].split("(", 1)
                                if parts:
                                    target_funcs.append(
                                        f"{fd.target_file}:{parts[0].strip()}"
                                    )
                    if target_funcs:
                        raw_impact = indexer.get_impacted_callers(target_funcs)
                        for tf in target_funcs:
                            fn_short = tf.split(":")[-1]
                            sym = (
                                indexer.get_symbol_definition(tf)
                                or (indexer.symbols_by_name.get(fn_short, [None])[0])
                            )
                            callers = raw_impact.get(tf, [])
                            if callers:
                                impact_callers[fn_short] = callers
                            elif sym and sym.callees:
                                impact_callers[fn_short] = [
                                    f"Calls: {c}()" for c in sym.callees
                                ]
                            else:
                                impact_callers[fn_short] = []

                    impact_msg = indexer.format_impact_context(final_diff)
                    cross_file_summary = CrossFileImpactSummary(
                        available=True,
                        is_python=True,
                        message=impact_msg,
                        impacted_callers=impact_callers,
                    )
                except Exception as e:
                    cross_file_summary = CrossFileImpactSummary(
                        available=True,
                        is_python=True,
                        message=f"AST impact analysis completed with notice: {str(e)}",
                        impacted_callers={},
                    )
            else:
                cross_file_summary = CrossFileImpactSummary(
                    available=False,
                    is_python=False,
                    message="cross-file impact analysis unavailable: Python only",
                    impacted_callers={},
                )

            # Ensure unit tests are available for Python changes if flow didn't populate them
            generated_tests = flow.state.generated_unit_tests
            if not generated_tests and is_python and final_diff:
                try:
                    from code_review_agent.tools.test_generator import TestGeneratorTool

                    generated_tests = TestGeneratorTool.generate_tests_for_diff(
                        final_diff
                    )
                except Exception as tg_err:
                    logger.warning(
                        f"Fallback unit test generation failed in review_service: {tg_err}"
                    )

            # 6. Extract Verdict, Confidence, Summary, and Complete Report
            verdict, confidence, summary, full_report = (
                cls.extract_verdict_and_confidence(flow.state)
            )

            # Only publish timing that was actually measured around the work.
            trace_data = {
                "trace_id": flow_id,
                "started_at": review_started,
                "duration_ms": review_duration_ms,
                "nodes": [
                    {
                        "id": "review",
                        "title": "Review analysis",
                        "stage": "REVIEW",
                        "status": "COMPLETED",
                        "started_at": review_started,
                        "ended_at": review_ended,
                        "duration_ms": review_duration_ms,
                        "details": {
                            "timing_scope": "End-to-end analysis; individual stages are not instrumented."
                        },
                        "children": [],
                    }
                ],
            }

            # 8. Assemble Structured API Response
            response = ReviewAPIResponse(
                reviewer_profile=flow.state.reviewer_profile,
                reviewer_profile_id=flow.state.reviewer_profile.profile_id
                if flow.state.reviewer_profile
                else None,
                input_snapshot_hash=snapshot.snapshot_hash,
                file_coverage=list(snapshot.coverage),
                analyzed_commit=snapshot.analyzed_commit,
                verdict=verdict if flow.state.analysis_complete else "ESCALATE",
                findings=flow.state.findings,
                analysis_complete=flow.state.analysis_complete,
                analyzer_status=flow.state.analyzer_status,
                confidence_score=confidence,
                summary=summary,
                full_report=full_report,
                pattern_findings_label="Static scanner candidates (heuristic evidence; see analyzer_status)",
                pattern_findings=flow.state.sast_findings,
                governance_violations=flow.state.rule_violations,
                cross_file_impact=cross_file_summary,
                generated_unit_tests=generated_tests or None,
                test_execution=flow.state.test_execution,
                ticket_compliance=flow.state.ticket_compliance,
                inline_comments=flow.state.inline_comments,
                telemetry=flow.state.telemetry,
                trace=trace_data,
                reviewed_diff=final_diff,
                scope_note=(
                    "Scope Note: Review performed via heuristic regex pattern scanning, AST Code Graph indexer "
                    "(Python only), and governance rules engine. Not a full dataflow SAST or formal verification engine."
                ),
            )

            from code_review_agent.eval.runtime import validate_response
            from code_review_agent.review_policy import apply_finding_policy

            validate_response(response)
            return apply_finding_policy(response)

        finally:
            if temp_dir_obj:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass

    @classmethod
    def build_review_response(cls, state: ReviewState) -> ReviewAPIResponse:
        """Construct standard ReviewAPIResponse directly from a completed ReviewState."""
        verdict, confidence, summary, full_report = cls.extract_verdict_and_confidence(
            state
        )

        impact_msg = ""
        try:
            indexer = CodeGraphIndexer(repo_root=state.repo_root)
            indexer.index_repository()
            if state.pr_content:
                indexer.index_diff(state.pr_content)
                impact_msg = indexer.format_impact_context(state.pr_content)
        except Exception:
            impact_msg = ""

        gen_tests: str | None = state.generated_unit_tests
        if not gen_tests and state.pr_content:
            try:
                from code_review_agent.tools.test_generator import TestGeneratorTool

                gen_tests = TestGeneratorTool.generate_tests_for_diff(state.pr_content)
            except Exception:
                gen_tests = None

        response = ReviewAPIResponse(
            verdict=verdict if state.analysis_complete else "ESCALATE",
            findings=state.findings,
            analysis_complete=state.analysis_complete,
            analyzer_status=state.analyzer_status,
            confidence_score=confidence,
            summary=summary,
            full_report=full_report,
            pattern_findings_label="Static scanner candidates (heuristic evidence; see analyzer_status)",
            pattern_findings=state.sast_findings,
            governance_violations=state.rule_violations,
            cross_file_impact=CrossFileImpactSummary(
                available=True, is_python=True, message=impact_msg
            ),
            generated_unit_tests=gen_tests or None,
            test_execution=state.test_execution,
            inline_comments=state.inline_comments,
            telemetry=state.telemetry,
            trace=None,
            reviewed_diff=state.pr_content,
            scope_note="Scope Note: Review performed via heuristic regex pattern scanning and AST Code Graph indexer.",
        )

        from code_review_agent.eval.runtime import validate_response
        from code_review_agent.review_policy import apply_finding_policy

        validate_response(response)
        return apply_finding_policy(response)
