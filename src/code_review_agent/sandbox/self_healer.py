"""
Dynamic Test Self-Healing Engine for Synthesized Unit Tests.
Detects pytest failure diagnostics (syntax errors, missing third-party modules, fixture omissions),
and applies deterministic repairs and in-memory mock stubs to stabilize test execution.
"""

import ast
import re
import sys
from collections import Counter
from enum import Enum
from typing import Callable, List, Optional, Set, Tuple
from code_review_agent.config import logger
from code_review_agent.models import TestExecutionResult


class FailureCategory(str, Enum):
    SYNTAX_ERROR = "SYNTAX_ERROR"
    MISSING_MODULE = "MISSING_MODULE"
    FIXTURE_ERROR = "FIXTURE_ERROR"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    UNKNOWN = "UNKNOWN"


class EvidenceTrustGrade(str, Enum):
    EMPIRICAL_ORIGINAL_PASSED = "EMPIRICAL_ORIGINAL_PASSED"
    HEALED_SYNTAX_REPAIRED = "HEALED_SYNTAX_REPAIRED"
    HEALED_MOCK_STUBBED = "HEALED_MOCK_STUBBED"
    UNVERIFIED = "UNVERIFIED"


class AssertionIntegrityError(Exception):
    """Raised when an automated transformation alters or removes test assertions."""
    pass


class TestSelfHealer:
    """
    Automated self-healing engine for generated test suites.
    Repairs syntactical defects, stubs unavailable sandbox dependencies,
    and reconciles missing pytest fixtures.
    """
    __test__ = False

    @classmethod
    def diagnose_failure(cls, code: str, error_log: str) -> List[FailureCategory]:
        """Categorize failure causes from test code syntax and runtime error logs."""
        categories: List[FailureCategory] = []

        # Check Python syntax directly
        try:
            ast.parse(code)
        except SyntaxError:
            categories.append(FailureCategory.SYNTAX_ERROR)

        if not error_log:
            return categories or [FailureCategory.UNKNOWN]

        # Missing modules / imports
        if re.search(r"(?:ModuleNotFoundError|ImportError):\s+No module named ['\"]?([a-zA-Z0-9_\.]+)['\"]?", error_log) or \
           re.search(r"cannot import name ['\"]?([a-zA-Z0-9_]+)['\"]? from ['\"]?([a-zA-Z0-9_\.]+)['\"]?", error_log):
            categories.append(FailureCategory.MISSING_MODULE)

        # Missing fixtures
        if re.search(r"fixture ['\"]?([a-zA-Z0-9_]+)['\"]? not found", error_log):
            categories.append(FailureCategory.FIXTURE_ERROR)

        # Syntax error reported by runner / pytest
        if "SyntaxError:" in error_log and FailureCategory.SYNTAX_ERROR not in categories:
            categories.append(FailureCategory.SYNTAX_ERROR)

        # Assertion failure
        if "AssertionError" in error_log or re.search(r"\d+ failed", error_log):
            categories.append(FailureCategory.ASSERTION_FAILURE)

        if not categories:
            categories.append(FailureCategory.UNKNOWN)

        return categories

    @classmethod
    def heal_syntax(cls, code: str) -> str:
        """
        Clean common LLM code synthesis artifacts:
        - Strip markdown code block wrappers
        - Remove stray backticks
        - Normalize mixed tab/space indentation
        - Truncate or repair unclosed blocks / incomplete trailing statements
        """
        if not code:
            return ""

        clean = code.strip()

        # 1. Strip markdown fences if present
        if "```" in clean:
            parts = clean.split("```")
            if len(parts) >= 3:
                clean = parts[1].strip()
            elif len(parts) == 2:
                clean = parts[1].strip()
            if clean.startswith("python"):
                clean = re.sub(r"^python\s*\n?", "", clean).strip()

        # Remove any remaining lone backticks
        clean = clean.replace("```", "").strip()

        # 2. Check if valid syntax already
        try:
            ast.parse(clean)
            return clean
        except SyntaxError:
            pass

        # 3. Handle unclosed quotes or docstrings
        for quote in ('"""', "'''"):
            if clean.count(quote) % 2 != 0:
                clean += f"\n{quote}\n"

        try:
            ast.parse(clean)
            return clean
        except SyntaxError:
            pass

        # 4. Handle trailing incomplete statements (e.g. LLM truncated output)
        lines = clean.splitlines()
        while lines:
            test_candidate = "\n".join(lines)
            try:
                ast.parse(test_candidate)
                return test_candidate
            except SyntaxError:
                pass

            # Try appending a pass statement if the last line ends with a colon
            last_line = lines[-1].strip()
            if last_line.endswith(":"):
                indent = " " * (len(lines[-1]) - len(last_line) + 4)
                candidate_with_pass = "\n".join(lines) + f"\n{indent}pass\n"
                try:
                    ast.parse(candidate_with_pass)
                    return candidate_with_pass
                except SyntaxError:
                    pass

            lines.pop()

        return clean

    @classmethod
    def heal_missing_modules(cls, code: str, error_log: str) -> str:
        """
        Detect missing third-party modules or uninstalled packages and inject
        safe in-memory MagicMock stubs so tests can execute inside minimal sandboxes.
        """
        missing_modules: Set[str] = set()

        for match in re.finditer(r"(?:ModuleNotFoundError|ImportError):\s+No module named ['\"]?([a-zA-Z0-9_\.]+)['\"]?", error_log):
            mod = match.group(1).strip()
            if mod:
                missing_modules.add(mod)

        for match in re.finditer(r"cannot import name ['\"]?([a-zA-Z0-9_]+)['\"]? from ['\"]?([a-zA-Z0-9_\.]+)['\"]?", error_log):
            item = match.group(1).strip()
            parent = match.group(2).strip()
            if parent:
                missing_modules.add(parent)
                missing_modules.add(f"{parent}.{item}")

        if not missing_modules:
            return code

        all_stubs: Set[str] = set()
        for mod in missing_modules:
            parts = mod.split(".")
            for i in range(1, len(parts) + 1):
                all_stubs.add(".".join(parts[:i]))

        stub_lines = [
            "# --- Auto-Healed Dependency Stubs (TestSelfHealer) ---",
            "import sys",
            "from unittest.mock import MagicMock",
            "for _stub_mod in " + repr(sorted(list(all_stubs))) + ":",
            "    if _stub_mod not in sys.modules:",
            "        sys.modules[_stub_mod] = MagicMock()",
            "# -----------------------------------------------------\n"
        ]
        stub_header = "\n".join(stub_lines)

        return stub_header + "\n" + code

    @classmethod
    def heal_fixtures(cls, code: str, error_log: str) -> str:
        """
        Detect missing pytest fixtures and inject mock fixture definitions.
        """
        missing_fixtures: Set[str] = set()

        for match in re.finditer(r"fixture ['\"]?([a-zA-Z0-9_]+)['\"]? not found", error_log):
            fix = match.group(1).strip()
            if fix:
                missing_fixtures.add(fix)

        if not missing_fixtures:
            return code

        fixture_lines = [
            "\n# --- Auto-Healed Pytest Fixtures (TestSelfHealer) ---",
            "import pytest",
            "from unittest.mock import MagicMock"
        ]
        for fix in sorted(list(missing_fixtures)):
            fixture_lines.extend([
                f"@pytest.fixture",
                f"def {fix}():",
                f"    return MagicMock()",
                ""
            ])
        fixture_lines.append("# -----------------------------------------------------\n")
        fixture_header = "\n".join(fixture_lines)

        return fixture_header + "\n" + code

    @classmethod
    def verify_assertion_integrity(cls, original_code: str, candidate_code: str) -> bool:
        """
        Verify that test assertions have not been silently rewritten, weakened, or removed.
        Parses AST of both and ensures every assert node in original_code remains present in candidate_code.
        If original_code contains syntax defects that AST cannot parse, falls back to normalized line matching.
        """
        orig_clean = cls.heal_syntax(original_code)
        cand_clean = cls.heal_syntax(candidate_code)

        try:
            cand_tree = ast.parse(cand_clean)
        except SyntaxError:
            return False

        try:
            orig_tree = ast.parse(orig_clean)
            orig_asserts = [
                ast.dump(node) for node in ast.walk(orig_tree) if isinstance(node, ast.Assert)
            ]
            cand_asserts = [
                ast.dump(node) for node in ast.walk(cand_tree) if isinstance(node, ast.Assert)
            ]

            orig_counts = Counter(orig_asserts)
            cand_counts = Counter(cand_asserts)

            for a, count in orig_counts.items():
                if cand_counts.get(a, 0) < count:
                    logger.warning("🚫 Assertion integrity violation: an assert statement was altered or removed!")
                    return False
            return True
        except SyntaxError:
            # Fallback for when original code has syntax flaws being healed:
            # Verify that any assert lines in original code are preserved in candidate
            orig_assert_lines = [
                re.sub(r"\s+", " ", l.strip())
                for l in orig_clean.splitlines()
                if re.match(r"^\s*assert\b", l)
            ]
            cand_assert_lines = [
                re.sub(r"\s+", " ", l.strip())
                for l in cand_clean.splitlines()
                if re.match(r"^\s*assert\b", l)
            ]
            orig_line_counts = Counter(orig_assert_lines)
            cand_line_counts = Counter(cand_assert_lines)
            for l, count in orig_line_counts.items():
                if cand_line_counts.get(l, 0) < count:
                    logger.warning("🚫 Assertion integrity violation (syntax fallback): an assert line was altered or removed!")
                    return False
            return True

    @classmethod
    def heal_code(cls, code: str, error_log: str) -> str:
        """Apply all relevant healing operations to code given the error diagnostics."""
        healed = cls.heal_syntax(code)
        if error_log:
            healed = cls.heal_missing_modules(healed, error_log)
            healed = cls.heal_fixtures(healed, error_log)
            healed = cls.heal_syntax(healed)
        return healed

    @classmethod
    def heal_and_run(
        cls,
        runner_func: Callable[[str, Optional[str]], TestExecutionResult],
        test_code: str,
        pr_content: Optional[str] = None,
        max_attempts: int = 2
    ) -> Tuple[TestExecutionResult, str]:
        """
        Iterative healing loop:
        1. Run test code using the provided runner.
        2. If passed or skipped, return immediately.
        3. If failed/error, diagnose failure, apply targeted fixes, and re-run.
        4. Repeat up to max_attempts.
        Enforces four-tier trust grading and preserves assertion integrity.
        """
        initial_had_syntax_defect = False
        try:
            ast.parse(test_code)
            if "```" in test_code:
                initial_had_syntax_defect = True
        except SyntaxError:
            initial_had_syntax_defect = True

        clean_initial = cls.heal_syntax(test_code)
        result = runner_func(clean_initial, pr_content)
        result.original_test_suite = test_code
        result.healed_test_suite = clean_initial

        if result.status == "PASSED":
            if initial_had_syntax_defect:
                result.trust_grade = EvidenceTrustGrade.HEALED_SYNTAX_REPAIRED.value
                result.self_healed = True
            else:
                result.trust_grade = EvidenceTrustGrade.EMPIRICAL_ORIGINAL_PASSED.value
            return result, clean_initial
        elif result.status == "SKIPPED":
            result.trust_grade = EvidenceTrustGrade.UNVERIFIED.value
            return result, clean_initial

        current_code = clean_initial
        used_mock_stubs = False
        repaired_syntax_only = False

        for attempt in range(1, max_attempts + 1):
            error_log = f"{result.stdout}\n{result.stderr}\n{getattr(result, 'raw_output', '')}\n{result.summary_message}"
            categories = cls.diagnose_failure(current_code, error_log)

            logger.info(
                f"🩺 TestSelfHealer attempt {attempt}/{max_attempts}: "
                f"diagnosed categories: {[c.value for c in categories]}"
            )

            # Assertions must never be silently rewritten to force tests to pass
            if FailureCategory.ASSERTION_FAILURE in categories and len(categories) == 1:
                logger.info("🩺 TestSelfHealer: Failure is purely AssertionError; assertions must not be rewritten.")
                break

            healed_code = cls.heal_code(current_code, error_log)

            # Verify that assertions were not modified
            if not cls.verify_assertion_integrity(test_code, healed_code):
                logger.warning("🩺 TestSelfHealer: Candidate healed code altered assertions; rejecting transformation.")
                raise AssertionIntegrityError(
                    "Assertion integrity violation: an automated healing transformation attempted to alter or delete test assertions."
                )

            if FailureCategory.MISSING_MODULE in categories or FailureCategory.FIXTURE_ERROR in categories:
                used_mock_stubs = True
            elif FailureCategory.SYNTAX_ERROR in categories:
                repaired_syntax_only = True

            if healed_code == current_code:
                logger.info("🩺 TestSelfHealer: no further deterministic repairs possible.")
                break

            current_code = healed_code
            healed_result = runner_func(current_code, pr_content)
            healed_result.original_test_suite = test_code
            healed_result.healed_test_suite = current_code

            if healed_result.status == "PASSED":
                logger.info(f"✅ TestSelfHealer: test suite successfully healed on attempt {attempt}!")
                healed_result.self_healed = True
                healed_result.heal_attempts = attempt
                if used_mock_stubs:
                    healed_result.trust_grade = EvidenceTrustGrade.HEALED_MOCK_STUBBED.value
                    healed_result.mock_stub_warning = (
                        "Explicit warning: Dependencies or fixtures were stubbed in-memory with MagicMock. "
                        "Execution is simulated and behavior has not been validated against live third-party packages."
                    )
                else:
                    healed_result.trust_grade = EvidenceTrustGrade.HEALED_SYNTAX_REPAIRED.value

                healed_result.summary_message = (
                    f"Generated tests passed after self-healing (attempt {attempt}, grade={healed_result.trust_grade}): "
                    f"repaired {', '.join([c.value for c in categories])}."
                )
                return healed_result, current_code

            result = healed_result

        result.heal_attempts = max_attempts
        result.trust_grade = EvidenceTrustGrade.UNVERIFIED.value
        result.original_test_suite = test_code
        result.healed_test_suite = current_code
        return result, current_code
