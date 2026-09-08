"""
Intent and Ticket Compliance Evaluation Engine.
Verifies pull request diffs against stated ticket requirements and acceptance criteria.
Detects unmet requirements, partial implementations, and unexpected scope creep.
"""

import json
import re
from typing import Callable, List, Any, Optional, Literal
from pydantic import BaseModel, Field

from code_review_agent.config import logger
from code_review_agent.compliance.ticket_fetcher import TicketDetails
from code_review_agent.llm_factory import LLMFactory


class CriterionEvaluation(BaseModel):
    """Evaluation result for an individual acceptance criterion."""
    criterion: str = Field(..., description="The acceptance criterion text")
    status: Literal["MET", "UNMET", "PARTIAL"] = Field(..., description="Evaluation status")
    evidence: str = Field(..., description="Code changes, functions, or reasons supporting this verdict")


class TicketComplianceReport(BaseModel):
    """Comprehensive ticket compliance and intent verification report."""
    ticket_id: str = Field(default="", description="Ticket ID, e.g. '#123' or 'PROJ-456'")
    ticket_title: str = Field(default="", description="Issue title")
    status: Literal["COMPLIANT", "PARTIALLY_COMPLIANT", "NON_COMPLIANT", "NO_TICKET"] = Field(
        default="NO_TICKET",
        description="Overall compliance verdict"
    )
    criteria_evaluations: List[CriterionEvaluation] = Field(default_factory=list)
    scope_creep_files: List[str] = Field(
        default_factory=list,
        description="Modified files that appear unrelated to the ticket requirements"
    )
    summary_markdown: str = Field(default="", description="GitHub-ready formatted compliance card")


class IntentComplianceEngine:
    """Evaluates PR diffs against ticket acceptance criteria."""

    @classmethod
    def evaluate(
        cls,
        ticket: Optional[TicketDetails],
        pr_diff: str,
        files_changed: Optional[List[str]] = None,
        llm_call: Optional[Callable[[str], str]] = None
    ) -> TicketComplianceReport:
        """
        Evaluate PR diff against ticket acceptance criteria and detect scope creep.

        `llm_call` lets the caller inject a bounded LLM invoker (e.g. the flow's
        timeout+telemetry wrapper); if omitted, a default un-timed LLM call is used.
        """
        if not ticket or not ticket.ticket_id:
            md = (
                "### 🎯 Ticket & Intent Compliance\n"
                "> ℹ️ **No Linked Ticket Detected**\n"
                "> No issue or ticket key was found in the PR title, body, or branch name. "
                "Include a reference like `Fixes #123` or `PROJ-456` in your PR description to enable automated acceptance criteria verification.\n"
            )
            return TicketComplianceReport(
                ticket_id="",
                ticket_title="",
                status="NO_TICKET",
                criteria_evaluations=[],
                scope_creep_files=[],
                summary_markdown=md
            )

        # Try LLM evaluation first
        evaluations: List[CriterionEvaluation] = []
        scope_creep: List[str] = []

        try:
            if llm_call is None:
                _llm = LLMFactory.create_llm()
                llm_call = lambda p: str(_llm.call(messages=p))
            evaluations, scope_creep = cls._llm_evaluate(llm_call, ticket, pr_diff, files_changed or [])
        except Exception as e:
            logger.warning(f"LLM ticket compliance evaluation failed ({e}), falling back to deterministic heuristic: {e}")
            evaluations, scope_creep = cls._heuristic_evaluate(ticket, pr_diff, files_changed or [])

        # Calculate overall status
        met_count = sum(1 for e in evaluations if e.status == "MET")
        partial_count = sum(1 for e in evaluations if e.status == "PARTIAL")
        total = len(evaluations)

        if total == 0 or met_count == total:
            overall_status = "COMPLIANT"
        elif met_count > 0 or partial_count > 0:
            overall_status = "PARTIALLY_COMPLIANT"
        else:
            overall_status = "NON_COMPLIANT"

        # Format markdown card
        summary_md = cls._render_markdown_card(ticket, overall_status, evaluations, scope_creep)

        return TicketComplianceReport(
            ticket_id=ticket.ticket_id,
            ticket_title=ticket.title,
            status=overall_status,
            criteria_evaluations=evaluations,
            scope_creep_files=scope_creep,
            summary_markdown=summary_md
        )

    @classmethod
    def _llm_evaluate(
        cls,
        llm_call: Callable[[str], str],
        ticket: TicketDetails,
        pr_diff: str,
        files_changed: List[str]
    ) -> tuple[List[CriterionEvaluation], List[str]]:
        """Run structured LLM prompt to verify criteria fulfillment."""
        truncated_diff = pr_diff[:12000] if len(pr_diff) > 12000 else pr_diff

        prompt = (
            f"You are a Quality & Release Assurance Lead auditing a Pull Request against ticket acceptance criteria.\n"
            f"Ticket: {ticket.ticket_id} - {ticket.title}\n"
            f"Description:\n{ticket.description[:1000]}\n\n"
            f"Stated Acceptance Criteria:\n"
            + "\n".join([f"- {c}" for c in ticket.acceptance_criteria]) + "\n\n"
            f"Modified Files: {', '.join(files_changed) if files_changed else 'See diff'}\n\n"
            f"PR Diff:\n```diff\n{truncated_diff}\n```\n\n"
            f"Task:\n"
            f"1. For each acceptance criterion, determine if the PR diff fulfills it ('MET', 'PARTIAL', or 'UNMET') and cite evidence from the diff.\n"
            f"2. Identify any modified files that are completely unrelated to the ticket requirements ('scope_creep').\n\n"
            f"Respond ONLY with a valid JSON object matching this schema:\n"
            f"{{\n"
            f'  "criteria": [\n'
            f'    {{"criterion": "string", "status": "MET"|"PARTIAL"|"UNMET", "evidence": "string"}}\n'
            f'  ],\n'
            f'  "scope_creep": ["filename.py"]\n'
            f"}}\n"
        )

        response = llm_call(prompt)
        raw_text = str(response).strip()

        # Clean JSON markdown fences
        if "```json" in raw_text:
            raw_text = raw_text.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```")[1].split("```")[0].strip()

        data = json.loads(raw_text)
        criteria_list = [CriterionEvaluation(**c) for c in data.get("criteria", [])]
        scope_creep = data.get("scope_creep", [])
        return criteria_list, scope_creep

    @classmethod
    def _heuristic_evaluate(
        cls,
        ticket: TicketDetails,
        pr_diff: str,
        files_changed: List[str]
    ) -> tuple[List[CriterionEvaluation], List[str]]:
        """Deterministic keyword and diff heuristic fallback when LLM is unavailable."""
        evaluations: List[CriterionEvaluation] = []
        diff_lower = pr_diff.lower()

        for criterion in ticket.acceptance_criteria:
            # Extract key tokens (length > 3, alphanumeric)
            tokens = [t.lower() for t in re.findall(r"\b[a-zA-Z]{4,}\b", criterion)]
            matches = [t for t in tokens if t in diff_lower]

            if not tokens or len(matches) >= max(1, len(tokens) // 2):
                status = "MET"
                evidence = f"Diff contains matching keywords: {', '.join(matches[:4])}"
            elif len(matches) > 0:
                status = "PARTIAL"
                evidence = f"Diff partially mentions keywords: {', '.join(matches)}"
            else:
                status = "UNMET"
                evidence = "No corresponding keywords or logic found in modified diff lines."

            evaluations.append(CriterionEvaluation(
                criterion=criterion,
                status=status,
                evidence=evidence
            ))

        return evaluations, []

    @classmethod
    def _render_markdown_card(
        cls,
        ticket: TicketDetails,
        status: str,
        evaluations: List[CriterionEvaluation],
        scope_creep: List[str]
    ) -> str:
        """Render beautiful GitHub markdown card for ticket compliance."""
        badge = (
            "🟢 **COMPLIANT**" if status == "COMPLIANT"
            else ("🟡 **PARTIALLY COMPLIANT**" if status == "PARTIALLY_COMPLIANT" else "🔴 **NON-COMPLIANT**")
        )

        ticket_link = f"[{ticket.ticket_id}]({ticket.url})" if ticket.url else f"`{ticket.ticket_id}`"
        lines = [
            f"### 🎯 Ticket & Intent Compliance: {badge}",
            f"**Linked Ticket**: {ticket_link} — *{ticket.title}*\n",
            "| Acceptance Criterion | Status | Evidence / Notes |",
            "| :--- | :---: | :--- |"
        ]

        for e in evaluations:
            icon = "✅ MET" if e.status == "MET" else ("⚠️ PARTIAL" if e.status == "PARTIAL" else "❌ UNMET")
            clean_criterion = e.criterion.replace("|", "\\|")
            clean_evidence = e.evidence.replace("|", "\\|")
            lines.append(f"| {clean_criterion} | `{icon}` | {clean_evidence} |")

        if scope_creep:
            lines.append("\n**⚠️ Potential Scope Creep Detected**:")
            for f in scope_creep:
                lines.append(f"- `{f}` (does not appear connected to the ticket requirements)")

        lines.append("")
        return "\n".join(lines)
