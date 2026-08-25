"""
SARIF (Static Analysis Results Interchange Format) Exporter.
Serializes code review and SAST findings into standard OASIS SARIF 2.1.0 JSON format
for seamless integration with GitHub Code Scanning, SonarQube, and CI/CD security dashboards.
"""

import json
from typing import List, Dict, Any, Optional
from pathlib import Path
from code_review_agent.models import SastFinding, InlineComment


class SarifExporter:
    """Generates standard SARIF v2.1.0 JSON reports from agent findings."""

    @staticmethod
    def build_sarif(
        sast_findings: List[SastFinding],
        inline_comments: Optional[List[InlineComment]] = None,
        tool_name: str = "AICodeReviewAgent",
        tool_version: str = "2.0.0"
    ) -> Dict[str, Any]:
        """Construct a complete SARIF v2.1.0 report dictionary."""
        inline_comments = inline_comments or []
        rules: Dict[str, Dict[str, Any]] = {}
        results: List[Dict[str, Any]] = []

        # 1. Process SAST Findings
        for finding in sast_findings:
            rule_id = finding.rule_id
            level = "error" if finding.severity in ["HIGH", "CRITICAL"] else ("warning" if finding.severity == "MEDIUM" else "note")

            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": rule_id,
                    "shortDescription": {"text": finding.description.split(":")[0]},
                    "fullDescription": {"text": finding.description},
                    "defaultConfiguration": {"level": level},
                    "properties": {
                        "tags": ["security", finding.cwe],
                        "precision": "high",
                    },
                    "help": {"text": f"Remediation: {finding.fix_recommendation}"}
                }

            result_entry = {
                "ruleId": rule_id,
                "level": level,
                "message": {
                    "text": f"{finding.description}\n\nSuggested Fix: {finding.fix_recommendation}"
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": finding.file_path,
                                "uriBaseId": "%SRCROOT%"
                            },
                            "region": {
                                "startLine": max(1, finding.line_number),
                                "snippet": {
                                    "text": finding.snippet
                                }
                            }
                        }
                    }
                ]
            }
            results.append(result_entry)

        # 2. Process Inline Quality/Security Comments
        for comment in inline_comments:
            rule_id = f"AI-REVIEW-{comment.severity}"
            level = "error" if comment.severity == "CRITICAL" else ("warning" if comment.severity == "WARNING" else "note")

            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": f"AI Code Review {comment.severity}",
                    "shortDescription": {"text": f"AI Code Review finding ({comment.severity})"},
                    "defaultConfiguration": {"level": level}
                }

            fix_text = f"\n\nSuggested Replacement:\n{comment.suggestion_code}" if comment.suggestion_code else ""
            result_entry = {
                "ruleId": rule_id,
                "level": level,
                "message": {
                    "text": f"{comment.comment_body}{fix_text}"
                },
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": comment.path,
                                "uriBaseId": "%SRCROOT%"
                            },
                            "region": {
                                "startLine": max(1, comment.line)
                            }
                        }
                    }
                ]
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
                            "rules": list(rules.values())
                        }
                    },
                    "results": results
                }
            ]
        }
        return sarif_doc

    @staticmethod
    def export_to_file(
        output_path: str,
        sast_findings: List[SastFinding],
        inline_comments: Optional[List[InlineComment]] = None
    ) -> str:
        """Write SARIF JSON report to target file path."""
        sarif_data = SarifExporter.build_sarif(sast_findings, inline_comments)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(sarif_data, f, indent=2)
        return str(target.resolve())
