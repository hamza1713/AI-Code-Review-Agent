"""
Unit tests for DiffParser.
Verifies line number mapping accuracy, single and multi-hunk parsing,
added line extraction, and token-aware diff chunking.
"""

from code_review_agent.diff_parser import DiffParser


class TestDiffParser:
    """Test suite for unified diff parsing and line number mappings."""

    def test_single_hunk_line_number_mapping(self):
        """Confirm accurate 1-indexed target line calculation for a single hunk."""
        diff = """diff --git a/app/service.py b/app/service.py
--- a/app/service.py
+++ b/app/service.py
@@ -10,4 +10,5 @@
 context_line_1
 context_line_2
+added_line_1
+added_line_2
 context_line_3
"""
        parsed_pr = DiffParser.parse_diff(diff)
        assert parsed_pr.files_changed == 1
        file_diff = parsed_pr.files[0]
        assert file_diff.target_file == "app/service.py"

        added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)
        assert len(added_lines) == 2
        # Lines 10 and 11 are context, so line 12 and 13 are added lines
        assert added_lines[0] == (12, "added_line_1")
        assert added_lines[1] == (13, "added_line_2")

    def test_multi_hunk_line_number_mapping(self):
        """Confirm accurate line calculation across multiple hunks."""
        diff = """diff --git a/app/utils.py b/app/utils.py
--- a/app/utils.py
+++ b/app/utils.py
@@ -1,3 +1,4 @@
 line_1
+added_at_top
 line_2
 line_3
@@ -20,3 +21,4 @@
 line_20
+added_at_bottom
 line_21
"""
        parsed_pr = DiffParser.parse_diff(diff)
        file_diff = parsed_pr.files[0]
        added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)
        assert len(added_lines) == 2
        assert added_lines[0] == (2, "added_at_top")
        assert added_lines[1] == (22, "added_at_bottom")

    def test_empty_or_whitespace_diff(self):
        """Confirm empty or whitespace diff returns clean empty ParsedPR."""
        parsed_pr = DiffParser.parse_diff("   \n\n  ")
        assert parsed_pr.files_changed == 0
        assert parsed_pr.total_added == 0
        assert parsed_pr.total_deleted == 0

    def test_chunk_diff_by_budget(self):
        """Confirm chunking keeps diff blocks under character budget."""
        diff = """diff --git a/file1.py b/file1.py
--- a/file1.py
+++ b/file1.py
@@ -1,2 +1,2 @@
-old
+new1
diff --git a/file2.py b/file2.py
--- a/file2.py
+++ b/file2.py
@@ -1,2 +1,2 @@
-old
+new2
"""
        parsed_pr = DiffParser.parse_diff(diff)
        chunks = DiffParser.chunk_diff_by_token_budget(
            parsed_pr, max_chars_per_chunk=50
        )
        assert len(chunks) >= 1

    def test_filter_noise_files_removes_lockfiles_and_build_artifacts(self):
        """Confirm package-lock.json, poetry.lock, and minified bundles are filtered from review."""
        mixed_diff = """diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,5 +1,5 @@
-{"version": "1.0.0"}
+{"version": "1.0.1"}
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -10,2 +10,3 @@
 def login():
+    token = request.headers.get("Authorization")
     return True
diff --git a/dist/bundle.min.js b/dist/bundle.min.js
--- a/dist/bundle.min.js
+++ b/dist/bundle.min.js
@@ -1,1 +1,1 @@
-var x=1;
+var x=2;
"""
        filtered_diff, skipped_files = DiffParser.filter_noise_files(mixed_diff)
        assert "src/auth.py" in filtered_diff
        assert "package-lock.json" not in filtered_diff
        assert "bundle.min.js" not in filtered_diff
        assert "package-lock.json" in skipped_files
        assert "dist/bundle.min.js" in skipped_files

    def test_rank_chunks_by_risk_prioritizes_vulnerable_and_security_files(self):
        """Confirm risk-ranked diff chunking sorts chunks with SAST findings and sensitive paths first."""
        from code_review_agent.models import SastFinding

        chunk_low = """diff --git a/docs/readme.txt b/docs/readme.txt
--- a/docs/readme.txt
+++ b/docs/readme.txt
@@ -1,2 +1,2 @@
-old info
+new info
"""
        chunk_high = """diff --git a/services/auth_service.py b/services/auth_service.py
--- a/services/auth_service.py
+++ b/services/auth_service.py
@@ -1,3 +1,4 @@
 def verify(token):
+    cursor.execute(f"SELECT * FROM users WHERE token = '{token}'")
"""
        sast_findings = [
            SastFinding(
                file_path="services/auth_service.py",
                line_number=2,
                tool="semgrep",
                rule_id="sql_injection",
                severity="HIGH",
                description="Potential SQL Injection detected",
                fix_recommendation="Use parameterized queries",
            )
        ]

        ranked = DiffParser.rank_chunks_by_risk(
            [chunk_low, chunk_high], sast_findings=sast_findings
        )
        assert len(ranked) == 2
        # chunk_high must be placed ahead of chunk_low
        assert "services/auth_service.py" in ranked[0]
        assert "docs/readme.txt" in ranked[1]
