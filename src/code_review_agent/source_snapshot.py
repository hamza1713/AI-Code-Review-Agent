"""Acquire changed source without trusting a mutable or failed checkout."""

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import httpx

from code_review_agent.diff_parser import DiffParser
from code_review_agent.path_validation import safe_diff_path
from code_review_agent.review_contracts import (
    FileCoverage,
    ReviewInputSnapshot,
    SourceFile,
)


def verified_checkout(root: str | None, commit: str | None) -> bool:
    if not root or not commit:
        return False
    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != commit:
            return False
        status = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-C",
                root,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return status.returncode == 0 and not status.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False


def build_snapshot(
    diff: str,
    commit: str | None = None,
    repository: str | None = None,
    root: str | None = None,
    trusted_upload: bool = False,
    fetch: Callable[[str], bytes] | None = None,
) -> ReviewInputSnapshot:
    files, coverage, total = [], [], 0
    valid_root = trusted_upload or verified_checkout(root, commit)
    for index, fd in enumerate(DiffParser.parse_diff(diff).files):
        path = fd.target_file
        if index >= 100:
            coverage.append(
                FileCoverage(
                    path=fd.source_file if fd.is_deleted_file else path,
                    status="LIMIT_EXCEEDED",
                    reason="File count limit",
                )
            )
            continue
        if fd.is_deleted_file:
            coverage.append(FileCoverage(path=fd.source_file, status="DELETED"))
            continue
        if time.time() >= float(os.getenv("REVIEW_WORKER_DEADLINE", "inf")):
            coverage.append(
                FileCoverage(
                    path=path, status="FAILED", reason="Acquisition deadline expired"
                )
            )
            continue
        if re.search(
            r"^(?:new file mode|new mode|index [^\n]+) 120000$",
            fd.raw_patch,
            re.MULTILINE,
        ):
            coverage.append(
                FileCoverage(path=path, status="UNSUPPORTED", reason="Symlink source")
            )
            continue
        raw: bytes | None = None
        origin: Literal[
            "HEAD_BLOB", "VERIFIED_CHECKOUT", "UPLOAD", "NEW_FILE", "CORPUS"
        ] = "NEW_FILE"
        try:
            if fetch:
                raw, origin = fetch(path), "HEAD_BLOB"
            elif valid_root and root:
                # Inspect the unresolved components, before safe_diff_path resolves links.
                candidate = Path(root)
                for part in path.split("/"):
                    candidate = candidate / part
                    if candidate.is_symlink() or (
                        hasattr(candidate, "is_junction") and candidate.is_junction()
                    ):
                        raise ValueError("Source path contains a link")
                dest = safe_diff_path(Path(root), path)
                if dest.is_symlink() or not dest.is_file():
                    raise ValueError("Source is not a regular file")
                if dest.stat().st_size > 2 * 1024 * 1024:
                    raise OverflowError
                raw, origin = (
                    dest.read_bytes(),
                    "UPLOAD" if trusted_upload else "VERIFIED_CHECKOUT",
                )
            elif fd.is_new_file:
                numbered = dict(DiffParser.extract_added_lines_with_numbers(fd))
                expected = sum(h.new_lines for h in fd.hunks)
                if not numbered and expected == 0:
                    raw = b""
                if (
                    numbered
                    and sorted(numbered) == list(range(1, len(numbered) + 1))
                    and expected == len(numbered)
                ):
                    raw = ("\n".join(numbered.values()) + "\n").encode()
            if raw is None:
                coverage.append(
                    FileCoverage(
                        path=path, status="MISSING", reason="Full source unavailable"
                    )
                )
                continue
            if not isinstance(raw, bytes):
                raise ValueError("Source fetch did not return bytes")
            if len(raw) > 2 * 1024 * 1024 or total + len(raw) > 20 * 1024 * 1024:
                raise OverflowError
            source = SourceFile.from_bytes(path, raw, origin)
            lines = source.text.splitlines()
            # Check every target-side context/added line, not only flagged lines.
            for hunk in fd.hunks:
                cursor = hunk.new_start
                for line in hunk.lines:
                    if line.startswith(("+", " ")):
                        if (
                            cursor < 1
                            or cursor > len(lines)
                            or lines[cursor - 1] != line[1:]
                        ):
                            raise ValueError("Diff does not match head source")
                        cursor += 1
            files.append(source)
            total += len(raw)
            coverage.append(
                FileCoverage(path=path, status="AVAILABLE", sha256=source.sha256)
            )
        except OverflowError:
            coverage.append(
                FileCoverage(
                    path=path, status="LIMIT_EXCEEDED", reason="Source byte limit"
                )
            )
        except (
            OSError,
            RuntimeError,
            ValueError,
            UnicodeError,
            httpx.RequestError,
            LookupError,
            TypeError,
            AttributeError,
            SyntaxError,
        ) as error:
            coverage.append(
                FileCoverage(
                    path=path,
                    status="MISMATCH" if isinstance(error, ValueError) else "MISSING",
                    reason=type(error).__name__,
                )
            )
    return ReviewInputSnapshot(
        diff=diff,
        analyzed_commit=commit,
        repository=repository,
        files=tuple(files),
        coverage=tuple(coverage),
    )
