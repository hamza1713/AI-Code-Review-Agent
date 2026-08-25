"""
Unit tests for Governance RulesEngine.
Verifies PyYAML parsing of multiline descriptions, inline comments, nested configs,
diff evaluation, and loud error raising on malformed YAML or invalid schemas.
"""

from pathlib import Path
import pytest

from code_review_agent.governance.rules_engine import RulesEngine, GovernanceParsingError, CustomRule


class TestGovernanceRulesEngine:
    """Test suite for .code-review.yaml parsing and rule enforcement."""

    def test_parse_valid_yaml_with_multiline_and_comments(self, tmp_path):
        """Verify rules with multiline strings and inline comments parse correctly."""
        yaml_content = """# Team Governance Config
version: "1.0"

rules:
  - id: "gov-custom-logging"
    name: "Enforce Structured Logging"
    severity: "BLOCKING"
    pattern: "print\\\\s*\\\\("
    description: >
      All direct print statements are strictly forbidden
      in production services because they corrupt stdout
      and bypass log aggregators.
    suggested_fix: "Use logger.info(...) instead of print."
    metadata:
      category: "observability"
      owner: "infra-team"
"""
        rule_file = tmp_path / ".code-review.yaml"
        rule_file.write_text(yaml_content, encoding="utf-8")

        engine = RulesEngine(rules_file_path=str(rule_file))
        assert len(engine.rules) == 1
        rule = engine.rules[0]
        assert rule.id == "gov-custom-logging"
        assert rule.severity == "BLOCKING"
        assert "strictly forbidden" in rule.description
        assert rule.metadata == {"category": "observability", "owner": "infra-team"}

    def test_fail_loudly_on_malformed_yaml(self, tmp_path):
        """Confirm that malformed YAML syntax fails loudly with GovernanceParsingError."""
        malformed_yaml = """
version: "1.0"
rules:
  - id: "broken-rule"
    name: [unclosed list
    severity: BLOCKING
"""
        rule_file = tmp_path / "broken_rules.yaml"
        rule_file.write_text(malformed_yaml, encoding="utf-8")

        with pytest.raises(GovernanceParsingError) as exc_info:
            RulesEngine(rules_file_path=str(rule_file))

        assert "Malformed YAML" in str(exc_info.value)

    def test_fail_loudly_on_schema_validation_error(self, tmp_path):
        """Confirm that missing mandatory fields fails loudly with GovernanceParsingError."""
        invalid_schema_yaml = """
version: "1.0"
rules:
  - name: "Missing ID and pattern"
    severity: "WARNING"
"""
        rule_file = tmp_path / "invalid_schema.yaml"
        rule_file.write_text(invalid_schema_yaml, encoding="utf-8")

        with pytest.raises(GovernanceParsingError) as exc_info:
            RulesEngine(rules_file_path=str(rule_file))

        assert "Schema validation error" in str(exc_info.value)

    def test_evaluate_diff_against_custom_rules(self, tmp_path):
        """Confirm evaluate_diff detects violations on added diff lines."""
        yaml_content = """
rules:
  - id: "gov-no-raw-sql"
    name: "No Raw SQL"
    severity: "BLOCKING"
    pattern: "db\\\\.execute\\\\s*\\\\(\\\\s*f"
    description: "Raw SQL f-strings are forbidden."
    suggested_fix: "Use parameterized queries."
"""
        rule_file = tmp_path / ".code-review.yaml"
        rule_file.write_text(yaml_content, encoding="utf-8")

        engine = RulesEngine(rules_file_path=str(rule_file))

        sample_diff = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,3 +10,4 @@
 def query_user(user_id):
+    result = db.execute(f"SELECT * FROM users WHERE id = {user_id}")
     return result
"""
        violations = engine.evaluate_diff(sample_diff)
        assert len(violations) == 1
        assert violations[0].rule_id == "gov-no-raw-sql"
        assert violations[0].severity == "BLOCKING"
        assert violations[0].line_number == 11
        assert violations[0].file_path == "app/db.py"

    def test_default_rules_loaded_when_file_not_found(self, tmp_path):
        """Confirm default baseline rules are loaded if file does not exist."""
        non_existent = tmp_path / "non_existent.yaml"
        engine = RulesEngine(rules_file_path=str(non_existent))
        assert len(engine.rules) > 0
        assert any(r.id == "default-no-print" for r in engine.rules)
