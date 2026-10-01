"""
Diff Parser and Token-Aware Chunking Engine.
Parses unified git diffs, computes line number mappings for GitHub inline comments,
and chunks large diffs into manageable batches.
"""

import ast
import re
from typing import Any

from code_review_agent.models import DiffHunk, FileDiff, ParsedPR
from code_review_agent.path_validation import UnsafeDiffPathError, validate_diff_path

DIFF_SPLIT_RE = re.compile(r"(?=diff --git )")
DIFF_HEADER_RE = re.compile(
    r'diff --git ("(?:[^"\\]|\\.)*"|a/.*?) ("(?:[^"\\]|\\.)*"|b/.*)$'
)
HUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")


def _header_path(value: str, prefix: str = "") -> str:
    if value.startswith('"'):
        try:
            value = ast.literal_eval(value)
        except (ValueError, SyntaxError) as error:
            raise UnsafeDiffPathError(
                "Unsafe diff path: malformed quoted filename"
            ) from error
        if not isinstance(value, str):
            raise UnsafeDiffPathError("Unsafe diff path: malformed filename")
        # Git quotes non-ASCII UTF-8 bytes using octal escapes.
        try:
            value = value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    if value == "/dev/null":
        return value
    if prefix and value.startswith(prefix):
        value = value[len(prefix) :]
    return validate_diff_path(value)


class DiffParser:
    """Parses raw unified git diffs into structured file and hunk objects."""

    @staticmethod
    def parse_diff(raw_diff: str) -> ParsedPR:
        """Parse raw git diff string into a structured ParsedPR model."""
        if not raw_diff or not raw_diff.strip():
            return ParsedPR(files=[], total_added=0, total_deleted=0, files_changed=0)

        file_diff_blocks = DIFF_SPLIT_RE.split(raw_diff)
        files: list[FileDiff] = []
        total_added = 0
        total_deleted = 0

        for block in file_diff_blocks:
            block = block.strip("\r\n")
            if not block:
                continue

            file_diff = DiffParser._parse_single_file_diff(block)
            if file_diff:
                files.append(file_diff)
                total_added += file_diff.added_lines_count
                total_deleted += file_diff.deleted_lines_count

        return ParsedPR(
            files=files,
            total_added=total_added,
            total_deleted=total_deleted,
            files_changed=len(files),
        )

    @staticmethod
    def _parse_single_file_diff(block: str) -> FileDiff | None:
        """Parse a single file's diff block."""
        lines = block.splitlines()
        if not lines:
            return None

        # Extract file paths from header
        source_file = ""
        target_file = ""
        is_new = False
        is_deleted = False

        first_line = lines[0]
        match = DIFF_HEADER_RE.search(first_line)
        if match:
            source_file = _header_path(match.group(1), "a/")
            target_file = _header_path(match.group(2), "b/")
        elif first_line.startswith("diff --git "):
            raise UnsafeDiffPathError("Unsafe diff path: malformed git header")

        for line in lines:
            if line.startswith("@@ "):
                break
            if line.startswith("new file mode"):
                is_new = True
            elif line.startswith("deleted file mode"):
                is_deleted = True
            elif line.startswith("--- "):
                source_file = _header_path(line[4:].split("\t", 1)[0], "a/")
                is_new = is_new or source_file == "/dev/null"
            elif line.startswith("+++ "):
                target_file = _header_path(line[4:].split("\t", 1)[0], "b/")
                is_deleted = is_deleted or target_file == "/dev/null"
            elif line.startswith(
                ("rename from ", "rename to ", "copy from ", "copy to ")
            ):
                _header_path(line.split(" ", 2)[2])

        if not target_file:
            target_file = source_file or "source_file.py"

        # Parse Hunks
        hunks: list[DiffHunk] = []
        current_hunk: DiffHunk | None = None
        added_count = 0
        deleted_count = 0

        for line in lines:
            if line.startswith("@@ "):
                hunk_match = HUNK_HEADER_RE.match(line)
                if hunk_match:
                    if current_hunk:
                        hunks.append(current_hunk)

                    old_start = int(hunk_match.group(1))
                    old_lines = int(hunk_match.group(2) or 1)
                    new_start = int(hunk_match.group(3))
                    new_lines = int(hunk_match.group(4) or 1)
                    header = line

                    current_hunk = DiffHunk(
                        old_start=old_start,
                        old_lines=old_lines,
                        new_start=new_start,
                        new_lines=new_lines,
                        header=header,
                        lines=[],
                    )
                    continue

            if current_hunk:
                current_hunk.lines.append(line)
                if line.startswith("+") and not line.startswith("+++"):
                    added_count += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deleted_count += 1

        if current_hunk:
            hunks.append(current_hunk)

        if not hunks and lines:
            code_lines = [
                line_item
                for line_item in lines
                if not line_item.startswith(
                    ("diff --git", "index ", "---", "+++", "new file", "deleted file")
                )
            ]
            if code_lines:
                formatted_lines = [
                    line_item if line_item.startswith(("+", "-")) else f"+{line_item}"
                    for line_item in code_lines
                ]
                added_count = sum(
                    1 for line_item in formatted_lines if line_item.startswith("+")
                )
                deleted_count = sum(
                    1 for line_item in formatted_lines if line_item.startswith("-")
                )
                hunks.append(
                    DiffHunk(
                        old_start=1,
                        old_lines=0,
                        new_start=1,
                        new_lines=len(formatted_lines),
                        header="@@ -0,0 +1 @@",
                        lines=formatted_lines,
                    )
                )

        return FileDiff(
            source_file=source_file,
            target_file=target_file,
            is_new_file=is_new,
            is_deleted_file=is_deleted,
            hunks=hunks,
            added_lines_count=added_count,
            deleted_lines_count=deleted_count,
            raw_patch=block,
        )

    @staticmethod
    def extract_added_lines_with_numbers(file_diff: FileDiff) -> list[tuple[int, str]]:
        """
        Extract added (+) lines along with their actual 1-indexed line numbers
        in the target new file for accurate GitHub inline commenting.
        """
        results: list[tuple[int, str]] = []

        for hunk in file_diff.hunks:
            current_new_line = hunk.new_start

            for line in hunk.lines:
                if line.startswith("+") and not line.startswith("+++"):
                    results.append((current_new_line, line[1:]))
                    current_new_line += 1
                elif line.startswith("-") and not line.startswith("---"):
                    # Deleted line: does not advance new line counter
                    pass
                else:
                    # Context line
                    current_new_line += 1

        return results

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        Heuristic token estimator for source code diffs (approx 3.7 chars per token).
        Fast, lightweight, and avoids heavy runtime tokenizer dependencies.
        """
        if not text:
            return 0
        return max(1, int(len(text) / 3.7))

    @classmethod
    def chunk_diff_by_token_budget(
        cls, parsed_pr: ParsedPR, max_chars_per_chunk: int = 12000
    ) -> list[str]:
        """
        Split a large PR diff into modular text chunks to avoid LLM context saturation.
        Ensures each file's changes remain contiguous where possible, and splits oversized
        single files across hunk boundaries with preserved headers.
        """
        if not parsed_pr or not parsed_pr.files:
            return [""]

        chunks: list[str] = []
        current_chunk_parts: list[str] = []
        current_length = 0

        for file_diff in parsed_pr.files:
            file_patch = file_diff.raw_patch.strip()
            patch_length = len(file_patch)

            # If a single file exceeds the max chunk budget, split it at hunk level
            if patch_length > max_chars_per_chunk:
                # Flush pending chunk if non-empty
                if current_chunk_parts:
                    chunks.append("\n\n".join(current_chunk_parts))
                    current_chunk_parts = []
                    current_length = 0

                # Split large file by hunks with header preservation
                file_subchunks = cls._chunk_large_file_by_hunks(
                    file_diff, max_chars_per_chunk
                )
                chunks.extend(file_subchunks)
                continue

            if (
                current_length + patch_length > max_chars_per_chunk
                and current_chunk_parts
            ):
                chunks.append("\n\n".join(current_chunk_parts))
                current_chunk_parts = [file_patch]
                current_length = patch_length
            else:
                current_chunk_parts.append(file_patch)
                current_length += patch_length

        if current_chunk_parts:
            chunks.append("\n\n".join(current_chunk_parts))

        return chunks or [""]

    @classmethod
    def _chunk_large_file_by_hunks(
        cls, file_diff: FileDiff, max_chars: int
    ) -> list[str]:
        """Splits an oversized single-file diff across hunk boundaries with header reproduction."""
        header_lines = [
            f"diff --git a/{file_diff.source_file if file_diff.source_file != '/dev/null' else file_diff.target_file} b/{file_diff.target_file if file_diff.target_file != '/dev/null' else file_diff.source_file}",
            "--- /dev/null"
            if file_diff.source_file == "/dev/null"
            else f"--- a/{file_diff.source_file}",
            "+++ /dev/null"
            if file_diff.target_file == "/dev/null"
            else f"+++ b/{file_diff.target_file}",
        ]
        header = "\n".join(header_lines)

        if not file_diff.hunks:
            return [file_diff.raw_patch]

        subchunks: list[str] = []
        current_hunks: list[str] = []
        current_len = len(header)

        for hunk in file_diff.hunks:
            hunk_text = hunk.header + "\n" + "\n".join(hunk.lines)
            if current_len + len(hunk_text) > max_chars and current_hunks:
                subchunks.append(header + "\n" + "\n".join(current_hunks))
                current_hunks = [hunk_text]
                current_len = len(header) + len(hunk_text)
            else:
                current_hunks.append(hunk_text)
                current_len += len(hunk_text)

        if current_hunks:
            subchunks.append(header + "\n" + "\n".join(current_hunks))

        return subchunks

    @classmethod
    def chunk_diff_adaptively(
        cls, raw_diff: str, max_tokens: int = 3000
    ) -> list[dict[str, Any]]:
        """
        Adaptive chunking that returns structured metadata for multi-segment reviews.
        """
        parsed = cls.parse_diff(raw_diff)
        max_chars = int(max_tokens * 3.7)
        raw_chunks = cls.chunk_diff_by_token_budget(
            parsed, max_chars_per_chunk=max_chars
        )

        result: list[dict[str, Any]] = []
        total = len(raw_chunks)
        for i, chunk_text in enumerate(raw_chunks):
            chunk_parsed = cls.parse_diff(chunk_text)
            files = [f.target_file for f in chunk_parsed.files]
            result.append(
                {
                    "chunk_index": i + 1,
                    "total_chunks": total,
                    "files": files,
                    "estimated_tokens": cls.estimate_tokens(chunk_text),
                    "diff_content": chunk_text,
                }
            )

        return result
