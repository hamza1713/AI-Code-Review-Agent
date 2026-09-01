"""
Shared fixtures and test helpers for AI Evaluation test suites.
"""

import pytest
from pathlib import Path
from code_review_agent.models import (
    SummarizedFindingsJSON,
    ReviewSecurityJSON,
    SecurityVulnerability,
    CodeQualityJSON,
    InlineComment,
    Fix,
    ReviewState,
)


@pytest.fixture
def sample_clean_summary() -> SummarizedFindingsJSON:
    """Fixture returning a clean, approved PR summary."""
    return SummarizedFindingsJSON(
        confidence=100,
        confidence_breakdown="100 (base) = 100",
        findings="Code Quality and Security checks passed. No blocking or critical issues.",
        coverage_gaps=[],
        fix=[],
        recommendations=["Follow standard code formatting guidelines."],
        inline_comments=[],
        suggested_unit_tests="""
import pytest
from app.math.calculator import calculate_average

def test_calculate_average_valid():
    assert calculate_average([10, 20, 30]) == 20.0

def test_calculate_average_empty():
    with pytest.raises(ValueError):
        calculate_average([])
"""
    )


@pytest.fixture
def sample_vulnerable_summary() -> SummarizedFindingsJSON:
    """Fixture returning a summary with SQLi and governance issues."""
    return SummarizedFindingsJSON(
        confidence=35,
        confidence_breakdown="100 (base) - 30 (critical SQLi) - 15 (high plaintext password) - 10 (critical issue: swallowed exception) - 10 (blocking rule: gov-no-raw-sql) = 35",
        findings="Critical SQL injection vulnerability detected along with plaintext authentication and bare exception handling.",
        coverage_gaps=["No call graph indexed for third-party dynamic plugins"],
        fix=[
            Fix(
                description="Fix SQL Injection in user lookup",
                solutions="Use parameterized queries with cursor.execute()",
                explanation="Unescaped user input in raw SQL enables arbitrary query execution.",
                file_path="app/services/user_service.py",
                line_number=10
            )
        ],
        recommendations=[
            "Migrate from raw queries to an ORM or parameterized query layer.",
            "Use bcrypt or argon2 for cryptographic password hashing."
        ],
        inline_comments=[
            InlineComment(
                path="app/services/user_service.py",
                line=10,
                side="RIGHT",
                severity="CRITICAL",
                comment_body="Raw SQL query formatted with user input.",
                why="User input flows directly into the SQL string allowing query manipulation.",
                suggestion_code="""
cursor = db_connection.cursor()
cursor.execute("SELECT id, username, email FROM users WHERE id = %s", (user_id,))
return cursor.fetchone()
"""
            )
        ],
        suggested_unit_tests="""
import pytest
from app.services.user_service import get_user_profile

def test_get_user_profile_executes():
    class DummyDB:
        def cursor(self):
            class DummyCursor:
                def execute(self, q, params=None): pass
                def fetchone(self): return {"id": "123", "username": "alice"}
            return DummyCursor()
    res = get_user_profile(DummyDB(), "123")
    assert res["username"] == "alice"
"""
    )


@pytest.fixture
def sample_security_review() -> ReviewSecurityJSON:
    """Fixture returning a Security Engineer output model."""
    return ReviewSecurityJSON(
        security_vulnerabilities=[
            SecurityVulnerability(
                description="SQL Injection via string formatting",
                risk_level="critical",
                evidence="query = f'SELECT * FROM users WHERE id = {user_id}'",
                matched_by=["bandit-B608", "regex-sqli-001"],
                line_number=10,
                file_path="app/services/user_service.py"
            )
        ],
        blocking=True,
        highest_risk="critical",
        security_recommendations=["Use parameterized SQL queries."],
        inline_security_comments=[]
    )
