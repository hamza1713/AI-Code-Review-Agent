"""Canonical calibration values shared by storage, APIs, and UI metadata."""

# Lifecycle outcomes
CONFIRMED_REAL = "CONFIRMED_REAL"  # Verified true defect (by user /confirm, /apply, or fixed in code)
FALSE_POSITIVE = "FALSE_POSITIVE"  # Explicitly flagged false alarm (/fp, ðŸ‘Ž reaction, or audit marked false)
INVALID_REPRODUCTION = (
    "INVALID_REPRODUCTION"  # Defect might exist, but generated test failed to prove it
)
IGNORED_MERGED = "IGNORED_MERGED"  # Unaddressed finding when PR was merged
PENDING = "PENDING"  # Initial review finding awaiting developer triage / PR action

VALID_OUTCOMES: set[str] = {
    CONFIRMED_REAL,
    FALSE_POSITIVE,
    INVALID_REPRODUCTION,
    IGNORED_MERGED,
    PENDING,
}

# Outcome attribution sources
SOURCE_HUMAN_EXPLICIT = "HUMAN_EXPLICIT"
SOURCE_GENERATED_TEST_VALIDATED = "GENERATED_TEST_VALIDATED"
SOURCE_PR_RESOLUTION = "PR_RESOLUTION"
SOURCE_REMEDIATION_APPLIED = "REMEDIATION_APPLIED"
SOURCE_REACTION = "REACTION"

# Human audit statuses for REPRODUCED findings
AUDIT_STATUS_UNAUDITED = "UNAUDITED"
AUDIT_STATUS_VALID = "AUDITED_VALID"
AUDIT_STATUS_INVALID = "AUDITED_INVALID"

VALID_AUDIT_STATUSES: set[str] = {
    AUDIT_STATUS_UNAUDITED,
    AUDIT_STATUS_VALID,
    AUDIT_STATUS_INVALID,
}

# Standardized audit rejection/confirmation reason codes
AUDIT_REASON_CORRECT = "CORRECT_REPRODUCTION"
AUDIT_REASON_PROVES_SOMETHING_ELSE = "TEST_PROVES_SOMETHING_ELSE"
AUDIT_REASON_ASSERTION_INCORRECT = "ASSERTION_INCORRECT"
AUDIT_REASON_FLAKY_OR_MOCKED = "TEST_FLAKY_OR_MOCKED"
AUDIT_REASON_FINDING_FALSE = "FINDING_IS_FALSE"
AUDIT_REASON_OTHER = "OTHER"

VALID_AUDIT_REASONS: set[str] = {
    AUDIT_REASON_CORRECT,
    AUDIT_REASON_PROVES_SOMETHING_ELSE,
    AUDIT_REASON_ASSERTION_INCORRECT,
    AUDIT_REASON_FLAKY_OR_MOCKED,
    AUDIT_REASON_FINDING_FALSE,
    AUDIT_REASON_OTHER,
}

REASON_ALIASES: dict = {
    "REPRODUCTION_CORRECT": AUDIT_REASON_CORRECT,
    "PROVES_SOMETHING_ELSE": AUDIT_REASON_PROVES_SOMETHING_ELSE,
    "SOMETHING_ELSE": AUDIT_REASON_PROVES_SOMETHING_ELSE,
    "ASSERTION_INCORRECT": AUDIT_REASON_ASSERTION_INCORRECT,
    "WRONG_ASSERTION": AUDIT_REASON_ASSERTION_INCORRECT,
    "FLAKY_OR_MOCKED": AUDIT_REASON_FLAKY_OR_MOCKED,
    "FLAKY": AUDIT_REASON_FLAKY_OR_MOCKED,
    "FINDING_FALSE": AUDIT_REASON_FINDING_FALSE,
    "FINDING_IS_FALSE": AUDIT_REASON_FINDING_FALSE,
    "FALSE_ALARM": AUDIT_REASON_FINDING_FALSE,
}

# Evidence tags
REPRODUCED = (
    "REPRODUCED"  # Grounded empirical evidence (unit test sandbox reproduced defect)
)
HEURISTIC = "HEURISTIC"  # Static regex, AST rule, or heuristic inference

VALID_EVIDENCE_GRADES: set[str] = {
    REPRODUCED,
    HEURISTIC,
}

# Categories
CATEGORY_SECURITY = "SECURITY"
CATEGORY_QUALITY = "QUALITY"
CATEGORY_GOVERNANCE = "GOVERNANCE"
CATEGORY_ARCHITECTURE = "ARCHITECTURE"

VALID_CATEGORIES: set[str] = {
    CATEGORY_SECURITY,
    CATEGORY_QUALITY,
    CATEGORY_GOVERNANCE,
    CATEGORY_ARCHITECTURE,
}


def normalize_category(category: str | None) -> str:
    """Normalize finding category into one of the 4 canonical categories."""
    if not category:
        return CATEGORY_QUALITY
    cat_upper = category.strip().upper()
    if cat_upper in VALID_CATEGORIES:
        return cat_upper
    if any(
        k in cat_upper for k in ("SEC", "VULN", "CVE", "CWE", "INJECT", "XSS", "AUTH")
    ):
        return CATEGORY_SECURITY
    if any(k in cat_upper for k in ("GOV", "RULE", "POLICY", "COMPLIANCE", "TICKET")):
        return CATEGORY_GOVERNANCE
    if any(
        k in cat_upper
        for k in ("ARCH", "IMPORT", "CIRCULAR", "LAYER", "GRAPH", "BOUNDARY")
    ):
        return CATEGORY_ARCHITECTURE
    return CATEGORY_QUALITY


def normalize_evidence_grade(grade: str | None) -> str:
    """Normalize evidence grade to REPRODUCED or HEURISTIC."""
    if not grade:
        return HEURISTIC
    g_upper = grade.strip().upper()
    if g_upper == "REPRODUCED":
        return REPRODUCED
    return HEURISTIC


def clean_fingerprint(fingerprint: str | None) -> str:
    """Normalize and clean a finding fingerprint by stripping markdown formatting and punctuation."""
    if not fingerprint:
        return ""
    return fingerprint.strip().strip("`'\"#:, <>[]()").lower()
