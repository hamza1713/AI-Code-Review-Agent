"""
Supply Chain & Slopsquatting Scanner.
Detects dependency hallucination (slopsquatting), typosquatting, and known malicious
packages in dependency manifests (requirements.txt, pyproject.toml, package.json, go.mod, Cargo.toml)
and newly added source code imports.
"""

import re
import sys
from pathlib import Path
from typing import Any

from code_review_agent.diff_parser import DiffParser
from code_review_agent.models import SastFinding

# Standard library module names in Python
_PY_STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "os",
    "sys",
    "time",
    "re",
    "json",
    "math",
    "random",
    "datetime",
    "pathlib",
    "typing",
    "collections",
    "itertools",
    "functools",
    "subprocess",
    "threading",
    "asyncio",
    "logging",
    "hashlib",
    "hmac",
    "base64",
    "secrets",
    "socket",
    "ssl",
    "http",
    "urllib",
    "tempfile",
    "shutil",
    "unittest",
    "copy",
    "uuid",
    "sqlite3",
    "csv",
    "io",
    "contextlib",
    "dataclasses",
    "enum",
    "abc",
    "struct",
}

# Standard Node.js built-ins
_NODE_BUILTINS = {
    "fs",
    "path",
    "http",
    "https",
    "crypto",
    "events",
    "stream",
    "util",
    "os",
    "url",
    "child_process",
    "buffer",
    "process",
    "zlib",
    "net",
    "tls",
    "dns",
    "assert",
    "cluster",
    "readline",
    "perf_hooks",
}

# Top popular packages on PyPI and npm for typosquatting / distance comparison
_POPULAR_PACKAGES: dict[str, set[str]] = {
    "pypi": {
        "requests",
        "urllib3",
        "numpy",
        "pandas",
        "flask",
        "django",
        "fastapi",
        "pydantic",
        "pytest",
        "httpx",
        "cryptography",
        "boto3",
        "click",
        "rich",
        "sqlalchemy",
        "aiohttp",
        "uvicorn",
        "celery",
        "redis",
        "scipy",
        "scikit-learn",
        "matplotlib",
        "torch",
        "transformers",
        "pillow",
        "beautifulsoup4",
        "jinja2",
        "pyyaml",
        "wheel",
        "setuptools",
        "pip",
        "certifi",
        "idna",
        "six",
        "packaging",
        "typing-extensions",
        "attrs",
        "pytz",
        "colorama",
        "tqdm",
        "greenlet",
        "starlette",
        "werkzeug",
        "gunicorn",
        "alembic",
        "psycopg2",
        "pymongo",
        "coverage",
        "black",
        "ruff",
        "mypy",
        "flake8",
    },
    "npm": {
        "express",
        "lodash",
        "axios",
        "react",
        "react-dom",
        "next",
        "vue",
        "svelte",
        "typescript",
        "eslint",
        "prettier",
        "chalk",
        "commander",
        "winston",
        "cors",
        "dotenv",
        "zod",
        "tailwindcss",
        "vite",
        "jest",
        "mocha",
        "webpack",
        "rxjs",
        "tslib",
        "debug",
        "semver",
        "uuid",
        "moment",
        "dayjs",
    },
}

# Known malicious, hijacked, or revoked packages (historical CVEs / malware campaigns)
_KNOWN_MALICIOUS_PACKAGES = {
    "colorama-v2",
    "python3-dateutil",
    "nodetest123",
    "ctx",
    "py-coord",
    "requests-oauth",
    "boto3-async",
}

# Suspicious affix patterns frequently hallucinated by LLMs (slopsquatting heuristics)
_SLOPSQUAT_PREFIXES = (
    "fast-",
    "fast_",
    "super-",
    "super_",
    "auto-",
    "auto_",
    "magic-",
    "magic_",
    "ultra-",
    "ultra_",
    "smart-",
    "smart_",
    "easy-",
    "easy_",
    "simple-",
    "simple_",
    "hyper-",
    "hyper_",
)
_SLOPSQUAT_SUFFIXES = (
    "-helper",
    "_helper",
    "-super-auth",
    "_super_auth",
    "-auto-orm",
    "_auto_orm",
    "-v2",
    "_v2",
    "-official-api",
    "_official_api",
    "-wrapper",
    "_wrapper",
)


def _damerau_levenshtein(s1: str, s2: str) -> int:
    """Compute Damerau-Levenshtein distance between two strings."""
    d: dict[tuple[int, int], int] = {}
    len1, len2 = len(s1), len(s2)
    for i in range(-1, len1 + 1):
        d[(i, -1)] = i + 1
    for j in range(-1, len2 + 1):
        d[(-1, j)] = j + 1

    for i in range(len1):
        for j in range(len2):
            cost = 0 if s1[i] == s2[j] else 1
            d[(i, j)] = min(
                d[(i - 1, j)] + 1,  # deletion
                d[(i, j - 1)] + 1,  # insertion
                d[(i - 1, j - 1)] + cost,  # substitution
            )
            if i > 0 and j > 0 and s1[i] == s2[j - 1] and s1[i - 1] == s2[j]:
                d[(i, j)] = min(d[(i, j)], d[(i - 2, j - 2)] + 1)  # transposition

    return d[(len1 - 1, len2 - 1)]


class SupplyChainAndSlopsquatScanner:
    """
    Analyzes pull requests for supply chain vulnerabilities:
    - Slopsquatting: AI-hallucinated packages that do not exist or are registered by attackers.
    - Typosquatting: Packages with subtle 1-2 character edits from popular libraries.
    - Malicious Packages: Known compromised or revoked packages.
    """

    @classmethod
    def is_available(cls) -> bool:
        return True

    @classmethod
    def scan_diff(cls, raw_diff: str) -> list[SastFinding]:
        """Scan unified git diff for supply-chain and dependency threats."""
        if not raw_diff or not raw_diff.strip():
            return []

        findings: list[SastFinding] = []
        parsed = DiffParser.parse_diff(raw_diff)

        for file_diff in parsed.files:
            target_path = (file_diff.target_file or "").replace("\\", "/")
            if file_diff.is_deleted_file:
                continue

            base_name = Path(target_path).name.lower()
            added_lines = DiffParser.extract_added_lines_with_numbers(file_diff)

            # 1. Dependency manifest files
            if base_name in (
                "requirements.txt",
                "requirements-dev.txt",
                "requirements.in",
            ) or base_name.startswith("requirements"):
                cls._scan_requirements_txt(target_path, added_lines, findings)
            elif base_name == "pyproject.toml":
                cls._scan_pyproject_toml(target_path, added_lines, findings)
            elif base_name == "package.json":
                cls._scan_package_json(target_path, added_lines, findings)
            elif base_name == "go.mod":
                cls._scan_go_mod(target_path, added_lines, findings)
            elif base_name == "cargo.toml":
                cls._scan_cargo_toml(target_path, added_lines, findings)

            # 2. Source code new imports (Python)
            elif target_path.endswith(".py"):
                cls._scan_python_imports(target_path, added_lines, findings)

        return findings

    @classmethod
    def _check_package(
        cls,
        pkg_name: str,
        ecosystem: str,
        file_path: str,
        line_number: int,
        raw_line: str,
        findings: list[SastFinding],
    ):
        """Validate package name against known malicious, typosquat, and slopsquat rules."""
        norm_pkg = pkg_name.strip().lower()
        if not norm_pkg or len(norm_pkg) < 2:
            return

        # 1. Known malicious or revoked packages
        if norm_pkg in _KNOWN_MALICIOUS_PACKAGES:
            findings.append(
                SastFinding(
                    rule_id="SEC-MALICIOUS-PKG-001",
                    cwe="CWE-1357",
                    category="SECURITY",
                    name="Known Malicious or Revoked Dependency Added",
                    description=(
                        f"The package '{pkg_name}' is a known malicious, compromised, or "
                        "revoked package on public registries."
                    ),
                    severity="CRITICAL",
                    file_path=file_path,
                    line_number=line_number,
                    snippet=raw_line,
                    fix_recommendation=(
                        f"Immediately remove dependency '{pkg_name}' from {file_path}. "
                        "Verify whether legitimate alternatives exist."
                    ),
                    analyzer_source="supply_chain_scanner",
                    evidence_grade="HEURISTIC",
                    evidence_provenance={
                        "rule": "known_malicious_blocklist",
                        "package": pkg_name,
                        "ecosystem": ecosystem,
                    },
                )
            )
            return

        popular_set = _POPULAR_PACKAGES.get(ecosystem, set())

        # Exact match with popular package -> Safe!
        if norm_pkg in popular_set:
            return

        # 2. Typosquatting detection against popular packages
        for pop_pkg in popular_set:
            # Skip if packages share no letters or length difference > 2
            if abs(len(norm_pkg) - len(pop_pkg)) > 2:
                continue
            dist = _damerau_levenshtein(norm_pkg, pop_pkg)
            if 1 <= dist <= 2 and norm_pkg != pop_pkg:
                findings.append(
                    SastFinding(
                        rule_id="SEC-TYPOSQUAT-002",
                        cwe="CWE-1357",
                        category="SECURITY",
                        name="Potential Dependency Typosquatting Detected",
                        description=(
                            f"The package '{pkg_name}' has an edit distance of {dist} from popular package '{pop_pkg}'. "
                            "This is a common indicator of a typosquatting supply-chain attack."
                        ),
                        severity="HIGH",
                        file_path=file_path,
                        line_number=line_number,
                        snippet=raw_line,
                        fix_recommendation=(
                            f"Confirm if you intended to install the official package '{pop_pkg}' "
                            f"instead of '{pkg_name}'."
                        ),
                        analyzer_source="supply_chain_scanner",
                        evidence_grade="HEURISTIC",
                        evidence_provenance={
                            "rule": "damerau_levenshtein_typosquat",
                            "candidate": pkg_name,
                            "target_popular": pop_pkg,
                            "distance": dist,
                        },
                    )
                )
                return

        # 3. Slopsquatting / AI Hallucination detection
        is_slopsquat = False
        reason = ""
        for prefix in _SLOPSQUAT_PREFIXES:
            if norm_pkg.startswith(prefix) and len(norm_pkg) > len(prefix) + 4:
                is_slopsquat = True
                reason = f"matches LLM-hallucinated prefix '{prefix}'"
                break
        if not is_slopsquat:
            for suffix in _SLOPSQUAT_SUFFIXES:
                if norm_pkg.endswith(suffix):
                    is_slopsquat = True
                    reason = f"matches LLM-hallucinated suffix '{suffix}'"
                    break

        if is_slopsquat:
            findings.append(
                SastFinding(
                    rule_id="SEC-SLOPSQUAT-001",
                    cwe="CWE-1357",
                    category="SECURITY",
                    name="Suspected AI-Hallucinated Package (Slopsquatting)",
                    description=(
                        f"The package '{pkg_name}' {reason}. AI code generators frequently hallucinate "
                        "package names with these patterns. If unregistered or newly registered, "
                        "installing it poses an immediate remote code execution risk."
                    ),
                    severity="HIGH",
                    file_path=file_path,
                    line_number=line_number,
                    snippet=raw_line,
                    fix_recommendation=(
                        f"Verify on official package registries (e.g. pypi.org / npmjs.com) that '{pkg_name}' "
                        "is a verified, actively maintained library before importing or installing."
                    ),
                    analyzer_source="supply_chain_scanner",
                    evidence_grade="HEURISTIC",
                    evidence_provenance={
                        "rule": "slopsquat_hallucination_pattern",
                        "package": pkg_name,
                        "ecosystem": ecosystem,
                    },
                )
            )

    @classmethod
    def _scan_requirements_txt(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            if not cleaned or cleaned.startswith("#") or cleaned.startswith("-"):
                continue
            # Extract package name up to comparison operators or comments
            m = re.match(r"^([A-Za-z0-9_.\-]+)", cleaned)
            if m:
                pkg_name = m.group(1).replace("_", "-")
                cls._check_package(
                    pkg_name, "pypi", file_path, line_no, text, findings
                )

    @classmethod
    def _scan_pyproject_toml(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            # Match dependencies strings like "requests>=2.0" or 'fast-jwt'
            m = re.search(r'["\']([A-Za-z0-9_.\-]+)(?:[<>=!~].*)?["\']', cleaned)
            if m:
                pkg_name = m.group(1).replace("_", "-")
                cls._check_package(
                    pkg_name, "pypi", file_path, line_no, text, findings
                )

    @classmethod
    def _scan_package_json(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            # Match "package-name": "^1.0.0"
            m = re.search(r'"(@?[A-Za-z0-9_.\-\/]+)"\s*:\s*"[^"]+"', cleaned)
            if m:
                pkg_name = m.group(1)
                cls._check_package(
                    pkg_name, "npm", file_path, line_no, text, findings
                )

    @classmethod
    def _scan_go_mod(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            if cleaned.startswith("require "):
                parts = cleaned.split()
                if len(parts) >= 2:
                    cls._check_package(
                        parts[1], "go", file_path, line_no, text, findings
                    )

    @classmethod
    def _scan_cargo_toml(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*=", cleaned)
            if m:
                cls._check_package(
                    m.group(1), "cargo", file_path, line_no, text, findings
                )

    @classmethod
    def _scan_python_imports(
        cls,
        file_path: str,
        lines: list[tuple[int, str]],
        findings: list[SastFinding],
    ):
        for line_no, text in lines:
            cleaned = text.strip()
            if not (cleaned.startswith("import ") or cleaned.startswith("from ")):
                continue

            pkg_name = None
            if cleaned.startswith("import "):
                match = re.match(r"^import\s+([A-Za-z0-9_]+)", cleaned)
                if match:
                    pkg_name = match.group(1)
            elif cleaned.startswith("from "):
                match = re.match(r"^from\s+([A-Za-z0-9_]+)", cleaned)
                if match:
                    pkg_name = match.group(1)

            if pkg_name and pkg_name not in _PY_STDLIB:
                norm_name = pkg_name.replace("_", "-")
                # Check for known malicious or slopsquats introduced via source code imports
                if (
                    norm_name in _KNOWN_MALICIOUS_PACKAGES
                    or any(norm_name.startswith(p) for p in _SLOPSQUAT_PREFIXES)
                    or any(norm_name.endswith(s) for s in _SLOPSQUAT_SUFFIXES)
                ):
                    cls._check_package(
                        norm_name, "pypi", file_path, line_no, text, findings
                    )
