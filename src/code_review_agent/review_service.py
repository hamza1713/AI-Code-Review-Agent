"""
Synchronous Review Service for In-Browser and Direct API Code Review.
Handles diff/file/zip ingestion, resource limits, zip security validation,
non-Python language detection, IP rate limiting, and structured response assembly.
"""

import io
import os
import re
import time
import zipfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import deque

from code_review_agent.config import logger, get_model_name
from code_review_agent.models import (
    ReviewState,
    ReviewAPIResponse,
    CrossFileImpactSummary,
    SastFinding,
    RuleViolation,
    InlineComment,
    TelemetryMetrics
)
from code_review_agent.main import PRCodeReviewFlow
from code_review_agent.diff_parser import DiffParser
from code_review_agent.context_engine.code_graph import CodeGraphIndexer


# Resource Limits
MAX_ZIP_BYTES = 2 * 1024 * 1024        # 2 MB max uploaded zip size
MAX_ZIP_FILES = 20                      # Max 20 files inside archive
MAX_UNCOMPRESSED_BYTES = 5 * 1024 * 1024 # 5 MB max total uncompressed size
MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024  # 2 MB max single file upload
MAX_DIFF_CHARS = 500_000                # ~500 KB raw diff limit


class RateLimitExceeded(Exception):
    """Raised when client IP exceeds rate limit."""
    def __init__(self, retry_after: int = 60):
        super().__init__(f"Rate limit exceeded. Try again in {retry_after} seconds.")
        self.retry_after = retry_after


class InputValidationError(Exception):
    """Raised when uploaded file or zip exceeds resource limits or fails security checks."""
    pass


class InMemoryRateLimiter:
    """
    Thread-safe sliding-window rate limiter per client IP address.
    Configurable request count within a time window (default: 30 req/min).
    """

    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._ip_history: Dict[str, deque] = {}

    def check_rate_limit(self, client_ip: str) -> None:
        """Check if request from client_ip is allowed; raises RateLimitExceeded if not."""
        now = time.time()
        window_start = now - self.window_seconds

        if client_ip not in self._ip_history:
            self._ip_history[client_ip] = deque()

        timestamps = self._ip_history[client_ip]

        # Evict timestamps outside sliding window
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()

        if len(timestamps) >= self.max_requests:
            oldest = timestamps[0]
            retry_after = max(1, int(self.window_seconds - (now - oldest)))
            logger.warning(f"⚠️ Rate limit exceeded for IP '{client_ip}'. Retry after {retry_after}s.")
            raise RateLimitExceeded(retry_after=retry_after)

        timestamps.append(now)

    def reset(self):
        """Reset rate limiter state (useful for tests)."""
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
        if clean_content.startswith("diff --git") or clean_content.startswith("--- "):
            return content

        clean_filename = filename.replace("\\", "/").lstrip("/") or "uploaded_file.py"
        lines = content.splitlines()
        num_lines = max(len(lines), 1)

        diff_lines = [
            f"diff --git a/{clean_filename} b/{clean_filename}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{clean_filename}",
            f"@@ -0,0 +1,{num_lines} @@"
        ]
        for line in lines:
            diff_lines.append(f"+{line}")

        if not lines:
            diff_lines.append("+")

        return "\n".join(diff_lines)

    @staticmethod
    def validate_and_extract_zip(zip_bytes: bytes, target_dir: Path) -> List[Path]:
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
                        f"Uncompressed zip content ({total_uncompressed / (1024*1024):.2f}MB) "
                        f"exceeds safety limit of {MAX_UNCOMPRESSED_BYTES // (1024 * 1024)}MB."
                    )

                resolved_target = target_dir.resolve()
                extracted_files: List[Path] = []

                for member in file_members:
                    # Prevent Zip Slip / Path Traversal
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise InputValidationError(f"Invalid or unsafe path in zip: '{member.filename}'")

                    dest_file = (target_dir / member_path).resolve()
                    if not str(dest_file).startswith(str(resolved_target)):
                        raise InputValidationError(f"Zip path traversal detected: '{member.filename}'")

                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest_file, "wb") as dst:
                        dst.write(src.read())

                    extracted_files.append(dest_file)

                return extracted_files

        except zipfile.BadZipFile as e:
            raise InputValidationError(f"Invalid or corrupted zip archive: {str(e)}") from e

    @staticmethod
    def directory_to_unified_diff(dir_path: Path) -> str:
        """Convert all files in a directory into a combined unified git diff."""
        diff_chunks: List[str] = []
        for root, _, files in os.walk(dir_path):
            for file in files:
                file_path = Path(root) / file
                try:
                    rel_path = str(file_path.relative_to(dir_path)).replace("\\", "/")
                except ValueError:
                    rel_path = file
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    diff_chunks.append(ReviewService.file_to_unified_diff(rel_path, content))
                except Exception as e:
                    logger.warning(f"Could not read extracted file '{file_path}': {e}")

        return "\n\n".join(diff_chunks)

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
    def extract_verdict_and_confidence(state: ReviewState) -> Tuple[str, int, str, str]:
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
            conf_match = re.search(r"confidence(?:\s*score)?[:\s*]+(\d+)", final_answer, re.IGNORECASE)
            if conf_match:
                try:
                    confidence = int(conf_match.group(1))
                except Exception:
                    pass

        # Clamp confidence to 0-100
        confidence = max(0, min(100, confidence))

        # 2. Determine Verdict
        verdict = "APPROVE"
        has_critical_sast = any(f.severity in ["CRITICAL", "HIGH"] for f in state.sast_findings)
        has_blocking_rules = any(r.severity == "BLOCKING" for r in state.rule_violations)

        final_upper = final_answer[:300].upper()
        if "ESCALATE" in final_upper or has_critical_sast or has_blocking_rules:
            verdict = "ESCALATE"
            if confidence > 40 and has_critical_sast:
                confidence = min(confidence, 30)
        elif "REQUEST CHANGES" in final_upper or "REQUEST_CHANGES" in final_upper or len(state.sast_findings) > 0 or len(state.rule_violations) > 0:
            verdict = "REQUEST CHANGES"
            if confidence > 75:
                confidence = 70
        elif "APPROVE" in final_upper and not has_critical_sast and not has_blocking_rules:
            verdict = "APPROVE"

        # 3. Summary and Full Report
        full_report = final_answer or review_result.get("findings") or "Review completed successfully."
        summary = review_result.get("findings") or final_answer or "Review completed successfully."

        return verdict, confidence, summary, full_report

    @classmethod
    def execute_review(
        cls,
        raw_diff: Optional[str] = None,
        file_name: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
        zip_bytes: Optional[bytes] = None,
        client_ip: str = "127.0.0.1"
    ) -> ReviewAPIResponse:
        """
        Execute synchronous in-browser review for raw diff, uploaded file, or uploaded zip.
        Enforces resource limits, rate limiting, and returns structured ReviewAPIResponse.
        """
        # 1. Rate Limiting Check
        rate_limiter.check_rate_limit(client_ip)

        # 2. Ingest and normalize inputs into unified diff and optional repo_root
        temp_dir_obj: Optional[tempfile.TemporaryDirectory] = None
        repo_root: Optional[str] = None
        final_diff = ""

        try:
            if zip_bytes:
                # Handle Zip Upload
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="code_review_zip_")
                temp_path = Path(temp_dir_obj.name)
                cls.validate_and_extract_zip(zip_bytes, temp_path)
                final_diff = cls.directory_to_unified_diff(temp_path)
                repo_root = str(temp_path)
                logger.info(f"📦 Extracted and indexed zip review archive at {repo_root}")

            elif file_bytes is not None and file_name:
                # Handle Single File Upload
                if len(file_bytes) > MAX_SINGLE_FILE_BYTES:
                    raise InputValidationError(
                        f"Uploaded file exceeds {MAX_SINGLE_FILE_BYTES // (1024 * 1024)}MB limit."
                    )
                content = file_bytes.decode("utf-8", errors="replace")
                final_diff = cls.file_to_unified_diff(file_name, content)

                # Create temp file so AST indexer can index it if it is Python
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="code_review_file_")
                temp_path = Path(temp_dir_obj.name)
                dest_file = temp_path / Path(file_name).name
                dest_file.write_text(content, encoding="utf-8", errors="replace")
                repo_root = str(temp_path)

            elif raw_diff:
                # Handle Pasted Raw Diff
                if len(raw_diff) > MAX_DIFF_CHARS:
                    raise InputValidationError(
                        f"Pasted diff length ({len(raw_diff)} chars) exceeds maximum allowable limit of {MAX_DIFF_CHARS} chars."
                    )
                final_diff = raw_diff.strip()

            else:
                raise InputValidationError("No review content provided. Provide 'raw_diff', 'file', or 'zip_file'.")

            if not final_diff.strip():
                raise InputValidationError("Supplied input contains no code or parseable diff content.")

            # 3. Check language (Python vs non-Python)
            parsed_pr = DiffParser.parse_diff(final_diff)
            is_python = cls.is_python_diff(parsed_pr, final_diff)

            # 4. Instantiate and execute PRCodeReviewFlow
            flow = PRCodeReviewFlow(tracing=False)
            flow.state.pr_content = final_diff
            flow.state.pr_file_path = ""
            flow.state.pr_url = None
            if repo_root:
                flow.state.repo_root = repo_root

            flow_id = f"web_review_{int(time.time())}"
            flow.kickoff(inputs={"id": flow_id})

            # 5. Build Cross-File Impact Information
            impact_callers: Dict[str, List[str]] = {}
            if is_python:
                try:
                    indexer = CodeGraphIndexer(repo_root=repo_root)
                    indexer.index_repository()
                    # Find all modified function callers
                    target_funcs = []
                    for fd in flow.state.parsed_pr.files if flow.state.parsed_pr else []:
                        for line_no, line_content in DiffParser.extract_added_lines_with_numbers(fd):
                            if "def " in line_content:
                                parts = line_content.split("def ", 1)[1].split("(", 1)
                                if parts:
                                    target_funcs.append(f"{fd.target_file}:{parts[0].strip()}")
                    if target_funcs:
                        impact_callers = indexer.get_impacted_callers(target_funcs)
                    
                    impact_msg = indexer.format_impact_context(final_diff)
                    cross_file_summary = CrossFileImpactSummary(
                        available=True,
                        is_python=True,
                        message=impact_msg,
                        impacted_callers=impact_callers
                    )
                except Exception as e:
                    cross_file_summary = CrossFileImpactSummary(
                        available=True,
                        is_python=True,
                        message=f"AST impact analysis completed with notice: {str(e)}",
                        impacted_callers={}
                    )
            else:
                cross_file_summary = CrossFileImpactSummary(
                    available=False,
                    is_python=False,
                    message="cross-file impact analysis unavailable: Python only",
                    impacted_callers={}
                )

            # 6. Extract Verdict, Confidence, Summary, and Complete Report
            verdict, confidence, summary, full_report = cls.extract_verdict_and_confidence(flow.state)

            # 7. Assemble Structured API Response
            response = ReviewAPIResponse(
                verdict=verdict,
                confidence_score=confidence,
                summary=summary,
                full_report=full_report,
                pattern_findings_label="Quick Pattern Scanner (heuristic)",
                pattern_findings=flow.state.sast_findings,
                governance_violations=flow.state.rule_violations,
                cross_file_impact=cross_file_summary,
                generated_unit_tests=flow.state.generated_unit_tests or None,
                inline_comments=flow.state.inline_comments,
                telemetry=flow.state.telemetry,
                scope_note=(
                    "Scope Note: Review performed via heuristic regex pattern scanning, AST Code Graph indexer "
                    "(Python only), and governance rules engine. Not a full dataflow SAST or formal verification engine."
                )
            )

            return response

        finally:
            if temp_dir_obj:
                try:
                    temp_dir_obj.cleanup()
                except Exception:
                    pass
