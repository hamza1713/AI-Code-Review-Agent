"""
Diff Parser and Token-Aware Chunking Engine.
Parses unified git diffs, computes line number mappings for GitHub inline comments,
and chunks large diffs into manageable batches.
"""

import re
from typing import List, Dict, Tuple, Optional
from code_review_agent.models import ParsedPR, FileDiff, DiffHunk


class DiffParser:
    """Parses raw unified git diffs into structured file and hunk objects."""

    @staticmethod
    def parse_diff(raw_diff: str) -> ParsedPR:
        """Parse raw git diff string into a structured ParsedPR model."""
        if not raw_diff or not raw_diff.strip():
            return ParsedPR(files=[], total_added=0, total_deleted=0, files_changed=0)

        file_diff_blocks = re.split(r"(?=diff --git )", raw_diff)
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
        match = re.search(r"diff --git a/(.*?) b/(.*)", first_line)
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

        hunk_header_regex = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")

        for line in lines:
            hunk_match = hunk_header_regex.match(line)
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
            elif current_hunk:
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
    def chunk_diff_by_token_budget(
        parsed_pr: ParsedPR,
        max_chars_per_chunk: int = 12000
    ) -> List[str]:
        """
        Split a large PR diff into modular text chunks to avoid LLM context saturation.
        Ensures each file's changes remain contiguous where possible.
        """
        chunks: List[str] = []
        current_chunk: List[str] = []
        current_length = 0

        for file_diff in parsed_pr.files:
            file_patch = file_diff.raw_patch
            patch_length = len(file_patch)

            if current_length + patch_length > max_chars_per_chunk and current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [file_patch]
                current_length = patch_length
            else:
                current_chunk.append(file_patch)
                current_length += patch_length

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        return chunks or [parsed_pr.files[0].raw_patch] if parsed_pr.files else [""]
