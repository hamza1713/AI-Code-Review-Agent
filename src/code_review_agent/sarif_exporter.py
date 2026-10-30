"""
SARIF (Static Analysis Results Interchange Format) Exporter.
Serializes code review and SAST findings into standard OASIS SARIF 2.1.0 JSON format
for seamless integration with GitHub Code Scanning, SonarQube, and CI/CD security dashboards.
"""

import json
from pathlib import Path
from typing import Any


class SarifExporter:
    """Generates standard SARIF v2.1.0 JSON reports from agent findings."""

    @staticmethod
    def build_sarif(
        sast_findings: list[Any],
        inline_comments: list[Any] | None = None,
        governance_violations: list[Any] | None = None,
        tool_name: str = "AICodeReviewAgent",
        tool_version: str = "3.0.0",
    ) -> dict[str, Any]:
        """Construct a complete SARIF v2.1.0 report dictionary."""
        inline_comments = inline_comments or []
        governance_violations = governance_violations or []
        rules: dict[str, dict[str, Any]] = {}
        results: list[dict[str, Any]] = []

        def get_field(item: Any, key: str, default: Any = "") -> Any:
            if isinstance(item, dict):
                return item.get(key, default)
            return getattr(item, key, default)

        # 1. Process SAST & Reconciled Findings
        for finding in sast_findings:
            rule_id = str(get_field(finding, "rule_id", "SEC-UNKNOWN"))
            severity = str(get_field(finding, "severity", "MEDIUM")).upper()
            cwe = str(get_field(finding, "cwe", "CWE-General"))
            desc = str(get_field(finding, "description", ""))
            fix = str(get_field(finding, "fix_recommendation", ""))
            file_path = str(get_field(finding, "file_path", "unknown"))
            line_no = int(get_field(finding, "line_number", 1) or 1)
            snippet = str(get_field(finding, "snippet", ""))

            level = (
                "error"
                if severity in ["HIGH", "CRITICAL"]
                else ("warning" if severity == "MEDIUM" else "note")
            )

            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": rule_id,
                    "shortDescription": {"text": desc.split(":")[0] if desc else rule_id},
                    "fullDescription": {"text": desc or rule_id},
                    "defaultConfiguration": {"level": level},
                    "properties": {
                        "tags": ["security", cwe],
                        "precision": "high",
                    },
                    "help": {"text": f"Remediation: {fix}" if fix else "Review code at location."},
                }

            result_entry = {
                "ruleId": rule_id,
                "level": level,
                "message": {
                    "text": f"{desc}\n\nSuggested Fix: {fix}" if fix else desc
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": file_path,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": {
                                "startLine": max(1, line_no),
                                "snippet": {"text": snippet} if snippet else {},
                            },
                        }
                    }
                ],
            }
            results.append(result_entry)

        # 2. Process Governance Violations
        for gov in governance_violations:
            rule_id = str(get_field(gov, "rule_id", "GOV-001"))
            rule_name = str(get_field(gov, "rule_name", rule_id))
            severity = str(get_field(gov, "severity", "WARNING")).upper()
            desc = str(get_field(gov, "description", ""))
            fix = str(get_field(gov, "suggested_fix", ""))
            file_path = str(get_field(gov, "file_path", "unknown"))
            line_no = int(get_field(gov, "line_number", 1) or 1)
            snippet = str(get_field(gov, "snippet", ""))

            level = "error" if severity in ["BLOCKING", "CRITICAL"] else "warning"

            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": rule_name,
                    "shortDescription": {"text": rule_name},
                    "fullDescription": {"text": desc or rule_name},
                    "defaultConfiguration": {"level": level},
                    "properties": {
                        "tags": ["governance", "standards"],
                        "precision": "high",
                    },
                    "help": {"text": f"Suggested Fix: {fix}" if fix else "Follow team coding standards."},
                }

            result_entry = {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": f"Governance Violation: {desc}\n\nSuggested Fix: {fix}" if fix else desc},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": file_path,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": {
                                "startLine": max(1, line_no),
                                "snippet": {"text": snippet} if snippet else {},
                            },
                        }
                    }
                ],
            }
            results.append(result_entry)

        # 3. Process Inline Quality/Security Comments
        for comment in inline_comments:
            severity = str(get_field(comment, "severity", "INFO")).upper()
            rule_id = f"AI-REVIEW-{severity}"
            comment_body = str(get_field(comment, "comment_body", ""))
            suggestion = str(get_field(comment, "suggestion_code", ""))
            path = str(get_field(comment, "path", "unknown"))
            line = int(get_field(comment, "line", 1) or 1)

            level = (
                "error"
                if severity == "CRITICAL"
                else ("warning" if severity == "WARNING" else "note")
            )

            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": f"AI Code Review {severity}",
                    "shortDescription": {
                        "text": f"AI Code Review finding ({severity})"
                    },
                    "defaultConfiguration": {"level": level},
                }

            fix_text = (
                f"\n\nSuggested Replacement:\n{suggestion}"
                if suggestion
                else ""
            )
            result_entry = {
                "ruleId": rule_id,
                "level": level,
                "message": {"text": f"{comment_body}{fix_text}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": path,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": {"startLine": max(1, line)},
                        }
                    }
                ],
            }
            results.append(result_entry)

        # Assemble full SARIF schema
        sarif_doc = {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": tool_name,
                            "semanticVersion": tool_version,
                            "informationUri": "https://github.com/hamza1713/code-review-agent",
                            "rules": list(rules.values()),
                        }
                    },
                    "results": results,
                }
            ],
        }
        return sarif_doc

    @staticmethod
    def export_to_file(
        output_path: str,
        sast_findings: list[Any],
        inline_comments: list[Any] | None = None,
        governance_violations: list[Any] | None = None,
    ) -> str:
        """Write SARIF JSON report to target file path."""
        sarif_data = SarifExporter.build_sarif(
            sast_findings, inline_comments, governance_violations
        )
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(sarif_data, f, indent=2)
        return str(target.resolve())
