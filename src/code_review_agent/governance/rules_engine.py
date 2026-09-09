"""
Enterprise Governance Rules Engine.
Evaluates repository-specific coding standards, architecture constraints,
and AppSec policies defined in .code-review.yaml against PR diffs using PyYAML and Pydantic.
"""

import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Type
import yaml
from pydantic import BaseModel, Field, ValidationError
from crewai.tools import BaseTool

from code_review_agent.models import RuleViolation
from code_review_agent.diff_parser import DiffParser
from code_review_agent.config import logger


class GovernanceParsingError(Exception):
    """Raised when .code-review.yaml contains invalid YAML syntax or schema errors."""
    pass


class CustomRule(BaseModel):
    id: str = Field(..., description="Unique rule identifier (e.g. gov-no-raw-sql)")
    name: str = Field(..., description="Human-readable rule name")
    severity: str = Field(default="WARNING", description="'BLOCKING', 'WARNING', or 'INFO'")
    pattern: str = Field(..., description="Regex pattern to match against added code lines")
    description: str = Field(..., description="Detailed explanation of the rule requirement")
    suggested_fix: str = Field(..., description="Remediation steps or suggested replacement")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional custom metadata or tags")


class GovernanceConfigFile(BaseModel):
    version: Optional[str] = Field(default="1.0", description="Schema version of configuration")
    rules: List[CustomRule] = Field(default_factory=list, description="List of configured governance rules")


class RulesEngine:
    """Evaluates project-specific custom rules (.code-review.yaml) with PyYAML and Pydantic validation."""

    def __init__(self, rules_file_path: Optional[str] = None, repo_root: Optional[str] = None):
        if rules_file_path:
            self.rules_file = Path(rules_file_path)
        elif repo_root:
            self.rules_file = Path(repo_root) / ".code-review.yaml"
        else:
            self.rules_file = Path(".code-review.yaml")
        self.rules: List[CustomRule] = []
        self.load_rules()

    def load_rules(self):
        """Parse and validate rules from .code-review.yaml. Fails loudly on malformed syntax or schema."""
        self.rules.clear()
        if not self.rules_file.exists():
            logger.info(f"No custom rules file found at {self.rules_file}. Using default baseline governance.")
            self._load_default_rules()
            return

        try:
            with open(self.rules_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            raise GovernanceParsingError(f"Failed to read rules file at {self.rules_file}: {e}") from e

        self._parse_yaml_content(content)
        logger.info(f"📋 Rules Engine: Loaded {len(self.rules)} custom governance rule(s) from {self.rules_file}.")

    def _parse_yaml_content(self, content: str):
        """Parse and validate YAML content using PyYAML and Pydantic. Raises GovernanceParsingError on errors."""
        if not content.strip():
            logger.warning(f"Rules file {self.rules_file} is empty. Using default baseline governance.")
            self._load_default_rules()
            return

        try:
            parsed_data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise GovernanceParsingError(
                f"Malformed YAML in governance rules file '{self.rules_file}':\n{str(e)}"
            ) from e

        if parsed_data is None:
            self._load_default_rules()
            return

        # Normalize format: either dictionary with 'rules' key or top-level list
        try:
            if isinstance(parsed_data, dict):
                config = GovernanceConfigFile(**parsed_data)
                self.rules = config.rules
            elif isinstance(parsed_data, list):
                self.rules = [CustomRule(**item) for item in parsed_data]
            else:
                raise GovernanceParsingError(
                    f"Invalid YAML structure in '{self.rules_file}': expected mapping or list, got {type(parsed_data).__name__}"
                )
        except ValidationError as e:
            raise GovernanceParsingError(
                f"Schema validation error in governance rules file '{self.rules_file}':\n{str(e)}"
            ) from e

    def _load_default_rules(self):
        """Fallback baseline governance rules."""
        self.rules = [
            CustomRule(
                id="default-no-print",
                name="Prohibit print() statements in production",
                severity="WARNING",
                pattern=r"(?<!#)\bprint\s*\(",
                description="Direct stdout logging should be replaced with structured logger.",
                suggested_fix="Use logger.info(...) or logger.debug(...)"
            ),
            CustomRule(
                id="default-no-raw-queries",
                name="Prohibit raw SQL string formatting",
                severity="BLOCKING",
                pattern=r"db\.(?:query|execute)\s*\(\s*f[\"']",
                description="Raw queries with string interpolation risk SQL injection.",
                suggested_fix="Use parameterized queries."
            )
        ]

    def evaluate_diff(self, raw_diff: str) -> List[RuleViolation]:
        """Scan PR diff added lines against loaded custom rules."""
        parsed_pr = DiffParser.parse_diff(raw_diff)
        violations: List[RuleViolation] = []

        for file_diff in parsed_pr.files:
            file_path = file_diff.target_file or file_diff.source_file or ""
            norm_path = file_path.lower().replace("\\", "/")
            is_exempt_from_quality = any(
                p in norm_path for p in [
                    "test", "tests/", "/test_", "_test.py", "scripts/", "benchmarks/", "samples/", "conftest.py"
                ]
            )
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)

            for line_no, line_content in added_lines:
                for rule in self.rules:
                    # Exempt tests and scripts from non-blocking quality rules (e.g. print statements)
                    if is_exempt_from_quality and ("print" in rule.id.lower() or "sleep" in rule.id.lower()):
                        continue
                    if "__main__" in line_content and "print" in rule.id.lower():
                        continue

                    try:
                        if re.search(rule.pattern, line_content, re.IGNORECASE):
                            violations.append(
                                RuleViolation(
                                    rule_id=rule.id,
                                    rule_name=rule.name,
                                    severity=rule.severity.upper(),
                                    file_path=file_path,
                                    line_number=line_no,
                                    description=rule.description,
                                    suggested_fix=rule.suggested_fix
                                )
                            )
                    except Exception as e:
                        logger.debug(f"Regex matching error on rule {rule.id}: {e}")

        return violations


# --- CrewAI Tool Wrapper ---

class CustomRulesInput(BaseModel):
    """Input for CustomRulesTool."""
    diff_content: str = Field(..., description="The raw unified diff to validate against team rules")


class CustomRulesTool(BaseTool):
    """CrewAI Tool for validating PR changes against team governance rules (.code-review.yaml)."""
    name: str = "Enterprise Governance Rules Validator"
    description: str = (
        "Validates PR code against team coding standards, architecture constraints, "
        "and governance policies defined in .code-review.yaml."
    )
    args_schema: Type[BaseModel] = CustomRulesInput

    _engine: Optional[RulesEngine] = None

    def __init__(self, rules_path: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._engine = RulesEngine(rules_file_path=rules_path)

    def _run(self, diff_content: str) -> str:
        """Run governance checks and format output for agent."""
        if not self._engine:
            return "Rules Engine not initialized."

        try:
            violations = self._engine.evaluate_diff(diff_content)
        except Exception as e:
            return f"❌ Governance check failed with error: {str(e)}"

        if not violations:
            return "✅ All team governance rules and coding standards passed (.code-review.yaml)."

        lines = [f"⚠️ Found {len(violations)} Project Governance Violation(s):"]
        for i, v in enumerate(violations, 1):
            lines.append(
                f"{i}. [{v.severity}] {v.rule_id}: {v.rule_name} in `{v.file_path}`:L{v.line_number}\n"
                f"   - Requirement: {v.description}\n"
                f"   - Fix Action: {v.suggested_fix}"
            )
        return "\n".join(lines)

