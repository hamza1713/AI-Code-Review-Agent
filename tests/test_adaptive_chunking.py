"""
Unit tests for adaptive chunking and token estimation in DiffParser.
"""

import pytest
from code_review_agent.diff_parser import DiffParser


class TestAdaptiveChunking:
    """Validate token estimation, hunk splitting, and adaptive chunking."""

    def test_token_estimation(self):
        text = "def hello_world():\n    print('hello world')\n"
        tokens = DiffParser.estimate_tokens(text)
        assert tokens > 0
        assert tokens == int(len(text) / 3.7)

    def test_adaptive_chunking_multi_file_pr(self):
        raw_diff = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,3 +1,3 @@
-old line 1
+new line 1

diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -1,3 +1,3 @@
-old line 2
+new line 2
"""
        chunks = DiffParser.chunk_diff_adaptively(raw_diff, max_tokens=1000)
        assert len(chunks) >= 1
        assert "chunk_index" in chunks[0]
        assert "estimated_tokens" in chunks[0]
        assert "diff_content" in chunks[0]

    def test_oversized_file_hunk_splitting(self):
        # Create diff with multiple hunks
        hunk1 = "@@ -1,10 +1,10 @@\n" + "\n".join([f"+line {i}" for i in range(50)])
        hunk2 = "@@ -20,10 +20,10 @@\n" + "\n".join([f"+line {i}" for i in range(50)])
        raw_diff = f"""diff --git a/big_file.py b/big_file.py
--- a/big_file.py
+++ b/big_file.py
{hunk1}
{hunk2}
"""
        parsed = DiffParser.parse_diff(raw_diff)
        # Force small chunk size
        chunks = DiffParser.chunk_diff_by_token_budget(parsed, max_chars_per_chunk=300)
        assert len(chunks) >= 2
        # Check header preservation
        assert all("diff --git a/big_file.py b/big_file.py" in c for c in chunks)
