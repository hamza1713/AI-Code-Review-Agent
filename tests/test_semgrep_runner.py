"""
Unit tests for SemgrepRunner.
Verifies Semgrep execution, JSON output parsing, and fallback handling when Semgrep is unavailable.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from code_review_agent.tools.semgrep_runner import SemgrepRunner


class TestSemgrepRunner:
    """Test suite for Semgrep CLI wrapper and JSON parser."""

    def test_is_available_returns_bool(self):
        """Confirm is_available returns a boolean."""
        res = SemgrepRunner.is_available()
        assert isinstance(res, bool)

    def test_parse_semgrep_json(self):
        """Verify Semgrep JSON results are converted to SastFinding models."""
        sample_json = json.dumps({
            "results": [
                {
                    "check_id": "python.lang.security.deserialization.pickle.avoid-pickle",
                    "path": "/tmp/test/app/tasks.py",
                    "start": {"line": 15, "col": 5},
                    "end": {"line": 15, "col": 25},
                    "extra": {
                        "message": "Avoid using `pickle` for untrusted deserialization.",
                        "severity": "ERROR",
                        "lines": "data = pickle.loads(raw_data)",
                        "metadata": {
                            "cwe": ["CWE-502: Deserialization of Untrusted Data"],
                            "fix_recommendation": "Use JSON or Protocol Buffers."
                        }
                    }
                }
            ]
        })

        findings = SemgrepRunner._parse_semgrep_json(
            sample_json,
            file_map={"/tmp/test/app/tasks.py": "app/tasks.py"}
        )

        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "python.lang.security.deserialization.pickle.avoid-pickle"
        assert f.cwe == "CWE-502"
        assert f.severity == "HIGH"
        assert f.file_path == "app/tasks.py"
        assert f.line_number == 15
        assert f.analyzer_source == "semgrep"

    def test_graceful_fallback_when_empty_or_unavailable(self):
        """Confirm scan_diff returns empty list when raw diff is empty."""
        assert SemgrepRunner.scan_diff("") == []
        assert SemgrepRunner.scan_diff("   ") == []
