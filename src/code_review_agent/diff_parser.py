"""
Diff Parser and Token-Aware Chunking Engine.
Parses unified git diffs, computes line number mappings for GitHub inline comments,
and chunks large diffs into manageable batches.
"""

import re
from typing import List, Dict, Tuple, Optional, Any
from code_review_agent.models import ParsedPR, FileDiff, DiffHunk


DIFF_SPLIT_RE = re.compile(r"(?=diff --git )")
DIFF_HEADER_RE = re.compile(r"diff --git a/(.*?) b/(.*)")
HUNK_HEADER_RE = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")


class DiffParser:
    """Parses raw unified git diffs into structured file and hunk objects."""

    @staticmethod
    def parse_diff(raw_diff: str) -> ParsedPR:
        """Parse raw git diff string into a structured ParsedPR model."""
        if not raw_diff or not raw_diff.strip():
            return ParsedPR(files=[], total_added=0, total_deleted=0, files_changed=0)

        file_diff_blocks = DIFF_SPLIT_RE.split(raw_diff)
        files: List[FileDiff] = []
        total_added = 0
        total_deleted = 0

        for block in file_diff_blocks:
            block = block.strip()
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
    def _parse_single_file_diff(block: str) -> Optional[FileDiff]:
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
            source_file = match.group(1)
            target_file = match.group(2)

        for line in lines[:10]:
            if line.startswith("new file mode"):
                is_new = True
            elif line.startswith("deleted file mode"):
                is_deleted = True
            elif line.startswith("--- a/"):
                source_file = line[6:]
            elif line.startswith("+++ b/"):
                target_file = line[6:]

        if not target_file:
            target_file = source_file or "unknown_file"

        # Parse Hunks
        hunks: List[DiffHunk] = []
        current_hunk: Optional[DiffHunk] = None
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
                        lines=[]
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

        return FileDiff(
            source_file=source_file,
            target_file=target_file,
            is_new_file=is_new,
            is_deleted_file=is_deleted,
            hunks=hunks,
            added_lines_count=added_count,
            deleted_lines_count=deleted_count,
            raw_patch=block
        )

    @staticmethod
    def extract_added_lines_with_numbers(file_diff: FileDiff) -> List[Tuple[int, str]]:
        """
        Extract added (+) lines along with their actual 1-indexed line numbers
        in the target new file for accurate GitHub inline commenting.
        """
        results: List[Tuple[int, str]] = []

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
        cls,
        parsed_pr: ParsedPR,
        max_chars_per_chunk: int = 12000
    ) -> List[str]:
        """
        Split a large PR diff into modular text chunks to avoid LLM context saturation.
        Ensures each file's changes remain contiguous where possible, and splits oversized
        single files across hunk boundaries with preserved headers.
        """
        if not parsed_pr or not parsed_pr.files:
            return [""]

        chunks: List[str] = []
        current_chunk_parts: List[str] = []
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
                file_subchunks = cls._chunk_large_file_by_hunks(file_diff, max_chars_per_chunk)
                chunks.extend(file_subchunks)
                continue

            if current_length + patch_length > max_chars_per_chunk and current_chunk_parts:
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
    def _chunk_large_file_by_hunks(cls, file_diff: FileDiff, max_chars: int) -> List[str]:
        """Splits an oversized single-file diff across hunk boundaries with header reproduction."""
        header_lines = [
            f"diff --git a/{file_diff.source_file} b/{file_diff.target_file}",
            f"--- a/{file_diff.source_file}",
            f"+++ b/{file_diff.target_file}"
        ]
        header = "\n".join(header_lines)

        if not file_diff.hunks:
            return [file_diff.raw_patch]

        subchunks: List[str] = []
        current_hunks: List[str] = []
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
        cls,
        raw_diff: str,
        max_tokens: int = 3000
    ) -> List[Dict[str, Any]]:
        """
        Adaptive chunking that returns structured metadata for multi-segment reviews.
        """
        parsed = cls.parse_diff(raw_diff)
        max_chars = int(max_tokens * 3.7)
        raw_chunks = cls.chunk_diff_by_token_budget(parsed, max_chars_per_chunk=max_chars)

        result: List[Dict[str, Any]] = []
        total = len(raw_chunks)
        for i, chunk_text in enumerate(raw_chunks):
            chunk_parsed = cls.parse_diff(chunk_text)
            files = [f.target_file for f in chunk_parsed.files]
            result.append({
                "chunk_index": i + 1,
                "total_chunks": total,
                "files": files,
                "estimated_tokens": cls.estimate_tokens(chunk_text),
                "diff_content": chunk_text
            })

        return result

