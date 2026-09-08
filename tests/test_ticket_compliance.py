"""
Unit tests for TicketParser, TicketFetcher, IntentComplianceEngine, and /ticket command.
"""

from unittest.mock import MagicMock, patch

from code_review_agent.compliance.ticket_parser import TicketParser
from code_review_agent.compliance.ticket_fetcher import TicketFetcher, TicketDetails
from code_review_agent.compliance.intent_engine import IntentComplianceEngine
from code_review_agent.bot.command_router import CommandRouter


SAMPLE_AUTH_DIFF = """diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,3 +10,6 @@
+def hash_password(password: str) -> str:
+    import bcrypt
+    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
"""

SAMPLE_UNRELATED_DIFF = """diff --git a/docs/readme.md b/docs/readme.md
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1,2 +1,3 @@
+# Project Documentation
"""


class TestTicketParser:
    """Test extracting ticket keys across GitHub, Jira, and Linear."""

    def test_extract_github_keywords(self):
        body = "This pull request Fixes #123 and closes #456. Also Resolves #789."
        refs = TicketParser.extract_ticket_references(title="Fix auth bug", body=body)
        ids = [r.ticket_id for r in refs]
        assert "#123" in ids
        assert "#456" in ids
        assert "#789" in ids
        assert all(r.ticket_type == "github" for r in refs)

    def test_extract_jira_keys(self):
        title = "[PROJ-452] Implement OAuth authentication"
        body = "Work tracking for JIRA-890 task."
        refs = TicketParser.extract_ticket_references(title=title, body=body)
        ids = [r.ticket_id for r in refs]
        assert "PROJ-452" in ids
        assert "JIRA-890" in ids

    def test_extract_linear_keys(self):
        title = "feat: add webhook retry logic"
        body = "Reference: ENG-1044 and LIN-52"
        refs = TicketParser.extract_ticket_references(title=title, body=body)
        ids = [r.ticket_id for r in refs]
        assert "ENG-1044" in ids
        assert "LIN-52" in ids

    def test_extract_from_branch_name(self):
        branch = "feature/PROJ-99-secure-tokens"
        refs = TicketParser.extract_ticket_references(title="Update tokens", body="", branch=branch)
        ids = [r.ticket_id for r in refs]
        assert "PROJ-99" in ids

    def test_deduplication(self):
        title = "Fix #42"
        body = "Closes #42 and #42"
        refs = TicketParser.extract_ticket_references(title=title, body=body)
        assert len([r for r in refs if r.ticket_id == "#42"]) == 1

    def test_excludes_security_and_standard_tokens(self):
        """CWE/CVE/AES etc. must not be misparsed as Jira tickets in a security tool."""
        refs = TicketParser.extract_ticket_references(
            title="Fix SQL injection (CWE-89)",
            body="Mitigates CVE-2023-1234; hardens with AES-256 and SHA-256.",
        )
        ids = [r.ticket_id for r in refs]
        assert "CWE-89" not in ids
        assert "CVE-2023" not in ids
        assert "AES-256" not in ids
        assert "SHA-256" not in ids

    def test_real_jira_key_still_parses_alongside_security_tokens(self):
        refs = TicketParser.extract_ticket_references(
            title="PROJ-321 fix auth (CWE-79)", body=""
        )
        ids = [r.ticket_id for r in refs]
        assert "PROJ-321" in ids
        assert "CWE-79" not in ids


class TestTicketFetcher:
    """Test extraction of acceptance criteria from markdown."""

    def test_extract_from_markdown_checkboxes(self):
        text = """
        ### Requirements
        - [ ] User can authenticate via password
        - [x] Password must be hashed with bcrypt
        - [ ] Rate limit failed attempts to 5 per minute
        """
        criteria = TicketFetcher.extract_acceptance_criteria_from_text(text)
        assert len(criteria) == 3
        assert "User can authenticate via password" in criteria
        assert "Password must be hashed with bcrypt" in criteria
        assert "Rate limit failed attempts to 5 per minute" in criteria

    def test_extract_from_header_bullets(self):
        text = """
        ### Acceptance Criteria
        * Enforce HTTPS on all webhook endpoints
        * Add HMAC SHA256 signature verification
        * Respond with 401 on invalid signature
        """
        criteria = TicketFetcher.extract_acceptance_criteria_from_text(text)
        assert len(criteria) == 3
        assert "Enforce HTTPS on all webhook endpoints" in criteria

    def test_fetch_ticket_from_github_client(self):
        mock_client = MagicMock()
        mock_client.fetch_issue.return_value = {
            "title": "Secure Password Hashing",
            "body": "- [ ] Use bcrypt for password hashing\n- [ ] Store salt with password",
            "state": "open",
            "html_url": "https://github.com/org/repo/issues/42"
        }
        refs = TicketParser.extract_ticket_references(title="Fixes #42", body="")
        details = TicketFetcher.fetch_ticket(
            ticket_ref=refs[0],
            repo_full_name="org/repo",
            github_client=mock_client
        )
        assert details.ticket_id == "#42"
        assert details.title == "Secure Password Hashing"
        assert len(details.acceptance_criteria) == 2
        assert "Use bcrypt for password hashing" in details.acceptance_criteria


class TestIntentComplianceEngine:
    """Test verification of PR diffs against acceptance criteria."""

    def test_no_ticket_returns_informative_status(self):
        report = IntentComplianceEngine.evaluate(ticket=None, pr_diff=SAMPLE_AUTH_DIFF)
        assert report.status == "NO_TICKET"
        assert "No Linked Ticket Detected" in report.summary_markdown

    def test_compliant_diff(self):
        ticket = TicketDetails(
            ticket_id="#42",
            title="Password Hashing with Bcrypt",
            acceptance_criteria=["Use bcrypt to hash passwords"]
        )
        report = IntentComplianceEngine.evaluate(ticket=ticket, pr_diff=SAMPLE_AUTH_DIFF)
        assert report.status in ("COMPLIANT", "PARTIALLY_COMPLIANT")
        assert len(report.criteria_evaluations) == 1
        assert report.criteria_evaluations[0].status == "MET"
        assert "COMPLIANT" in report.summary_markdown

    def test_non_compliant_diff(self):
        ticket = TicketDetails(
            ticket_id="#99",
            title="Database Migration to PostgreSQL",
            acceptance_criteria=["Migrate user schema to PostgreSQL with foreign keys"]
        )
        # Sample doc edit diff does not mention postgres or migration
        report = IntentComplianceEngine.evaluate(ticket=ticket, pr_diff=SAMPLE_UNRELATED_DIFF)
        assert report.status == "NON_COMPLIANT"
        assert report.criteria_evaluations[0].status == "UNMET"
        assert "NON-COMPLIANT" in report.summary_markdown

    def test_uses_injected_llm_call(self):
        """The flow injects a bounded llm_call; evaluate must use it, not build its own."""
        ticket = TicketDetails(
            ticket_id="#7",
            title="Add login endpoint",
            acceptance_criteria=["Add a login endpoint"],
        )
        seen = {}

        def fake_llm(prompt: str) -> str:
            seen["called"] = True
            return '{"criteria":[{"criterion":"Add a login endpoint","status":"MET","evidence":"added login()"}],"scope_creep":[]}'

        report = IntentComplianceEngine.evaluate(
            ticket=ticket, pr_diff="+def login(): pass", llm_call=fake_llm
        )
        assert seen.get("called") is True
        assert report.status == "COMPLIANT"
        assert report.criteria_evaluations[0].status == "MET"

    @patch("code_review_agent.llm_factory.LLMFactory.create_llm")
    def test_llm_compliance_with_scope_creep(self, mock_llm_factory):
        mock_llm = MagicMock()
        mock_llm.call.return_value = """
        {
          "criteria": [
            {"criterion": "Hash passwords with bcrypt", "status": "MET", "evidence": "app/auth.py adds bcrypt.hashpw"}
          ],
          "scope_creep": ["docs/internal_notes.txt"]
        }
        """
        mock_llm_factory.return_value = mock_llm

        ticket = TicketDetails(
            ticket_id="PROJ-101",
            title="Auth security hardening",
            acceptance_criteria=["Hash passwords with bcrypt"]
        )
        report = IntentComplianceEngine.evaluate(
            ticket=ticket,
            pr_diff=SAMPLE_AUTH_DIFF,
            files_changed=["app/auth.py", "docs/internal_notes.txt"]
        )
        assert report.status == "COMPLIANT"
        assert "docs/internal_notes.txt" in report.scope_creep_files
        assert "Scope Creep" in report.summary_markdown


class TestBotTicketCommand:
    """Test /ticket slash command."""

    def test_ticket_command_with_explicit_arg(self):
        res = CommandRouter.dispatch(
            command_text="/ticket #42",
            raw_diff=SAMPLE_AUTH_DIFF,
            auto_post=False
        )
        assert res.command == "ticket"
        assert res.status == "SUCCESS"
        assert "#42" in res.response_markdown

    def test_ticket_command_no_ticket_found(self):
        res = CommandRouter.dispatch(
            command_text="/ticket",
            raw_diff=SAMPLE_AUTH_DIFF,
            auto_post=False
        )
        assert res.command == "ticket"
        assert res.status == "SUCCESS"
        assert "No Linked Ticket Detected" in res.response_markdown
