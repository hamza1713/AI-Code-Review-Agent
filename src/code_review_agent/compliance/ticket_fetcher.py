"""
Ticket Content and Acceptance Criteria Fetcher.
Retrieves issue details from GitHub Issues API or PR body descriptions.
Extracts structured acceptance criteria for automated verification.
"""

import re
from typing import List, Optional
from pydantic import BaseModel, Field

from code_review_agent.config import logger
from code_review_agent.compliance.ticket_parser import TicketReference
from code_review_agent.github_client import GitHubClient


class TicketDetails(BaseModel):
    """Detailed ticket data including parsed acceptance criteria."""
    ticket_id: str = Field(..., description="Ticket key, e.g. '#42' or 'PROJ-101'")
    ticket_type: str = Field(default="github", description="'github', 'jira', or 'linear'")
    title: str = Field(default="", description="Issue or ticket title")
    description: str = Field(default="", description="Issue description or user story")
    acceptance_criteria: List[str] = Field(
        default_factory=list,
        description="Parsed acceptance criteria statements"
    )
    status: str = Field(default="open", description="Issue status, e.g. 'open', 'closed'")
    url: Optional[str] = Field(default=None, description="Direct URL to ticket")


class TicketFetcher:
    """Fetches ticket information and extracts acceptance criteria."""

    CHECKBOX_PATTERN = re.compile(r"[-*]\s*\[[ xX]\]\s*(.+)")
    BULLET_PATTERN = re.compile(r"[-*]\s+(.+)")

    @classmethod
    def extract_acceptance_criteria_from_text(cls, text: str) -> List[str]:
        """
        Extract acceptance criteria bullets or checkbox items from markdown text.
        Looks for checkboxes first; if none found, looks for bullet points under
        sections like 'Acceptance Criteria', 'Requirements', or 'Definition of Done'.
        """
        if not text:
            return []

        # 1. Look for explicit markdown checkboxes anywhere in text
        checkboxes = [m.group(1).strip() for m in cls.CHECKBOX_PATTERN.finditer(text)]
        if checkboxes:
            return checkboxes

        # 2. Look for section headers: Acceptance Criteria / Requirements / DoD
        section_pattern = re.compile(
            r"(?:###?\s*(?:Acceptance Criteria|Requirements|Definition of Done|Expected Behavior)[\s:]*)(.*?)(?:###|\Z)",
            re.IGNORECASE | re.DOTALL
        )
        match = section_pattern.search(text)
        if match:
            section_body = match.group(1).strip()
            bullets = [m.group(1).strip() for m in cls.BULLET_PATTERN.finditer(section_body)]
            if bullets:
                return bullets
            # If no bullets, split by non-empty lines
            lines = [line.strip() for line in section_body.splitlines() if line.strip()]
            if lines:
                return lines

        # 3. Fallback: extract any bullet points
        bullets = [m.group(1).strip() for m in cls.BULLET_PATTERN.finditer(text)]
        return bullets[:10]  # Cap at 10 items to prevent noise

    @classmethod
    def fetch_ticket(
        cls,
        ticket_ref: TicketReference,
        repo_full_name: Optional[str] = None,
        pr_body: Optional[str] = None,
        github_client: Optional[GitHubClient] = None
    ) -> TicketDetails:
        """
        Fetch ticket details from GitHub API if applicable, or parse from PR body.
        """
        ticket_id = ticket_ref.ticket_id

        # 1. If GitHub issue and repo is specified, attempt live GitHub fetch
        if ticket_ref.ticket_type == "github" and repo_full_name and "/" in repo_full_name:
            try:
                owner, repo = repo_full_name.split("/", 1)
                issue_num = int(ticket_id.lstrip("#"))
                client = github_client or GitHubClient()
                issue_data = client.fetch_issue(owner, repo, issue_num)
                if issue_data:
                    title = issue_data.get("title", f"Issue {ticket_id}")
                    body = issue_data.get("body") or ""
                    criteria = cls.extract_acceptance_criteria_from_text(body)
                    return TicketDetails(
                        ticket_id=ticket_id,
                        ticket_type="github",
                        title=title,
                        description=body,
                        acceptance_criteria=criteria,
                        status=issue_data.get("state", "open"),
                        url=issue_data.get("html_url")
                    )
            except Exception as e:
                logger.warning(f"Could not fetch GitHub issue {ticket_id} via API: {e}")

        # 2. Fallback: Parse criteria from PR body text
        criteria = cls.extract_acceptance_criteria_from_text(pr_body or "")
        title = f"Requirement for {ticket_id}"

        # Try to infer title from PR body if header exists
        if pr_body:
            for line in pr_body.splitlines():
                if ticket_id in line:
                    title = line.strip("#-* ")
                    break

        return TicketDetails(
            ticket_id=ticket_id,
            ticket_type=ticket_ref.ticket_type,
            title=title,
            description=pr_body or "",
            acceptance_criteria=criteria or [f"Implement functionality requested in {ticket_id}"],
            status="open",
            url=f"https://github.com/{repo_full_name}/issues/{ticket_id.lstrip('#')}" if repo_full_name and ticket_ref.ticket_type == "github" else None
        )
