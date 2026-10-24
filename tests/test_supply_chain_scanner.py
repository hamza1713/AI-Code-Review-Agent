"""
Unit tests for SupplyChainAndSlopsquatScanner.
Verifies detection of slopsquatting (AI hallucinations), typosquatting, and malicious packages.
"""

from code_review_agent.tools.supply_chain_scanner import (
    SupplyChainAndSlopsquatScanner,
)


class TestSupplyChainScanner:
    """Test suite for supply chain dependency threats."""

    def test_detects_slopsquatting_hallucinations_in_requirements(self):
        diff = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,2 +1,3 @@
 pytest>=7.0.0
+fast_jwt_super_auth>=1.0.0
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "SEC-SLOPSQUAT-001"
        assert f.severity == "HIGH"
        assert (
            "fast-jwt-super-auth" in f.description
            or "fast_jwt_super_auth" in f.description
        )
        assert f.file_path == "requirements.txt"

    def test_detects_typosquatting_against_popular_packages(self):
        diff = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,1 +1,2 @@
 requests>=2.31.0
+coloarama==0.4.6
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) >= 1
        typo_finding = next(
            f for f in findings if f.rule_id == "SEC-TYPOSQUAT-002"
        )
        assert typo_finding.severity == "HIGH"
        assert "colorama" in typo_finding.description
        assert "coloarama" in typo_finding.description

    def test_detects_known_malicious_packages(self):
        diff = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -10,1 +10,2 @@
 dependencies = [
+    "colorama-v2>=0.1",
 ]
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) >= 1
        mal_finding = next(
            f for f in findings if f.rule_id == "SEC-MALICIOUS-PKG-001"
        )
        assert mal_finding.severity == "CRITICAL"
        assert "colorama-v2" in mal_finding.description

    def test_detects_npm_package_json_typosquats(self):
        diff = """diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -5,1 +5,2 @@
   "dependencies": {
+    "lodsh": "^4.17.21"
   }
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) >= 1
        f = findings[0]
        assert f.rule_id == "SEC-TYPOSQUAT-002"
        assert "lodash" in f.description

    def test_detects_slopsquat_in_python_source_imports(self):
        diff = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,2 +1,3 @@
 import os
+import fast_jwt_super_auth
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) >= 1
        f = findings[0]
        assert f.rule_id == "SEC-SLOPSQUAT-001"

    def test_clean_popular_packages_pass_without_findings(self):
        diff = """diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,1 +1,4 @@
+requests>=2.31.0
+pydantic>=2.0.0
+fastapi>=0.100.0
"""
        findings = SupplyChainAndSlopsquatScanner.scan_diff(diff)
        assert len(findings) == 0
