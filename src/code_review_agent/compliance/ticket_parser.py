"""
Ticket and Issue Key Parser for Pull Request titles, bodies, and branch names.
Supports GitHub issues, Jira tickets, and Linear identifiers.
"""

import re
from typing import List, Optional
from pydantic import BaseModel, Field


class TicketReference(BaseModel):
    """Represents an extracted ticket or issue reference."""
    ticket_type: str = Field(..., description="'github', 'jira', or 'linear'")
    ticket_id: str = Field(..., description="Canonical ticket identifier (e.g., '#42', 'PROJ-123', 'ENG-45')")
    raw_text: str = Field(..., description="Matched text substring in PR")
    source: str = Field(default="body", description="'title', 'body', or 'branch'")


class TicketParser:
    """Parses ticket references from PR titles, descriptions, and branch names."""

    # GitHub issue patterns: Fixes #123, Closes #45, Resolves #67, #89
    GITHUB_KEYWORD_PATTERN = re.compile(
        r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)",
        re.IGNORECASE
    )
    GITHUB_SHORTHAND_PATTERN = re.compile(r"(?:^|\s)#(\d+)(?:\b|$)")

    # Jira issue pattern: [ABC-123], ABC-1234 (uppercase prefix + hyphen + digits)
    JIRA_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

    # Linear issue pattern: ENG-123, LIN-456
    LINEAR_PATTERN = re.compile(r"\b((?:ENG|LIN|DEV|APP)-\d+)\b", re.IGNORECASE)

    # Prefixes that look like Jira keys but are standards/crypto/security identifiers.
    # This is a code-review tool, so PR text is full of CWE-89, CVE-2023-1234, SHA-256,
    # AES-256, etc. — none of which are tickets. Matched case-insensitively.
    _NON_TICKET_PREFIXES = (
        "UTF-", "ISO-", "SHA-", "PR-", "RFC-", "CWE-", "CVE-", "OWASP-", "AES-",
        "RSA-", "DES-", "MD-", "EC-", "GB-", "KB-", "MB-", "TB-", "HTTP-", "OAUTH-",
        "SSH-", "TLS-", "SSL-", "IPV-", "X-", "SOC-", "PEP-", "ES-", "CSS-", "HTML-",
    )

    @classmethod
    def extract_ticket_references(
        cls,
        title: str = "",
        body: str = "",
        branch: Optional[str] = None
    ) -> List[TicketReference]:
        """Extract all unique ticket references across title, body, and branch name."""
        references: List[TicketReference] = []
        seen_ids = set()

        def add_ref(ticket_type: str, ticket_id: str, raw_text: str, source: str):
            canonical = ticket_id.upper() if ticket_type in ("jira", "linear") else f"#{ticket_id.lstrip('#')}"
            if canonical not in seen_ids:
                seen_ids.add(canonical)
                references.append(TicketReference(
                    ticket_type=ticket_type,
                    ticket_id=canonical,
                    raw_text=raw_text,
                    source=source
                ))

        sources = [("title", title or ""), ("body", body or "")]
        if branch:
            sources.append(("branch", branch))

        for source_name, text in sources:
            if not text:
                continue

            # 1. GitHub keyword matches (e.g. Fixes #123)
            for m in cls.GITHUB_KEYWORD_PATTERN.finditer(text):
                add_ref("github", f"#{m.group(1)}", m.group(0), source_name)

            # 2. Jira matches (e.g. PROJ-123)
            for m in cls.JIRA_PATTERN.finditer(text):
                issue_key = m.group(1).upper()
                # Exclude standards/crypto/security identifiers that share the shape.
                if not issue_key.startswith(cls._NON_TICKET_PREFIXES):
                    add_ref("jira", issue_key, m.group(0), source_name)

            # 3. Linear matches (e.g. ENG-123)
            for m in cls.LINEAR_PATTERN.finditer(text):
                add_ref("linear", m.group(1).upper(), m.group(0), source_name)

            # 4. GitHub shorthand matches (#123)
            for m in cls.GITHUB_SHORTHAND_PATTERN.finditer(text):
                add_ref("github", f"#{m.group(1)}", m.group(0), source_name)

        return references
