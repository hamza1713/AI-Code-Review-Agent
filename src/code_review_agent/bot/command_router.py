"""
Interactive PR Bot & Slash Command Router.
Handles developer commands in PR comments: /describe, /ask, /improve, /compliance, /help, /review.
Enables bi-directional conversation inside GitHub Pull Requests.
"""

import re
import os
import urllib.parse
from pathlib import Path
from contextlib import ExitStack
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from code_review_agent.config import logger
from code_review_agent.llm_factory import LLMFactory
from code_review_agent.diff_parser import DiffParser
from code_review_agent.github_client import GitHubClient
from code_review_agent.governance.rules_engine import RulesEngine
from code_review_agent.tools.sast_scanner import SastEngine
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.context_engine.semantic import SemanticContextEngine
from code_review_agent.bot.checkout import temporary_pr_checkout
from code_review_agent.platform.factory import get_platform_client
from code_review_agent.learning import TeamMemoryStore
from code_review_agent.compliance import TicketParser, TicketFetcher, IntentComplianceEngine, TicketReference

# Max characters accepted for a free-text /ask question (defence against abuse / prompt bloat).
_MAX_ASK_QUESTION_CHARS = 2000
# Markers delimiting the bot-managed block inside a PR description, so /describe can
# be re-run idempotently without clobbering the author's own text.
_DESC_MARKER_START = "<!-- ai-code-review:description -->"
_DESC_MARKER_END = "<!-- /ai-code-review:description -->"


@dataclass
class BotCommandResult:
    """Result of executing a bot command."""
    command: str
    status: str  # "SUCCESS", "ERROR", "IGNORED"
    response_markdown: str
    action_taken: str  # "POST_COMMENT", "UPDATE_DESCRIPTION", "NONE"
    metadata: Dict[str, Any]


class CommandRouter:
    """
    Parses and dispatches slash commands from PR comments or REST API.
    """

    COMMAND_PATTERN = re.compile(r"^\s*/([a-zA-Z0-9_\-]+)(?:\s+(.*))?$", re.DOTALL)

    # Commands that reason about the repository (not just the diff) and therefore need
    # a real checkout. For live PRs the router clones one; see temporary_pr_checkout.
    _REPO_CONTEXT_COMMANDS = {"ask", "improve", "compliance", "review", "ticket"}

    @classmethod
    def is_bot_command(cls, text: str) -> bool:
        """Check whether a comment string begins with a slash command."""
        if not text:
            return False
        first_line = text.strip().split("\n")[0].strip()
        return bool(cls.COMMAND_PATTERN.match(first_line))

    @classmethod
    def parse_command(cls, text: str) -> Tuple[str, str]:
        """
        Extract (command_name, arguments) from text.
        Returns ("", "") if not a command.
        """
        if not text:
            return "", ""
        stripped = text.strip()
        match = cls.COMMAND_PATTERN.match(stripped)
        if not match:
            return "", ""
        cmd = match.group(1).lower()
        args = (match.group(2) or "").strip()
        return cmd, args

    @classmethod
    def dispatch(
        cls,
        command_text: str,
        pr_url: Optional[str] = None,
        raw_diff: Optional[str] = None,
        repo_root: Optional[str] = None,
        auto_post: bool = True
    ) -> BotCommandResult:
        """
        Parse command and dispatch to appropriate handler.
        If auto_post is True and pr_url is provided, automatically posts the response back to GitHub.
        """
        cmd, args = cls.parse_command(command_text)
        if not cmd:
            return BotCommandResult(
                command="unknown",
                status="IGNORED",
                response_markdown="No valid slash command found.",
                action_taken="NONE",
                metadata={}
            )

        # Resolve diff if missing but PR URL is provided — across GitHub, GitLab,
        # Bitbucket, or a local repo, via the platform adapter factory.
        diff = raw_diff
        pr_metadata = {}
        client = None
        owner, repo, pull_number = None, None, None
        platform = "github"
        host = None

        if pr_url:
            try:
                client, ident = get_platform_client(pr_url)
                platform = ident.platform
                owner, repo, pull_number = ident.owner_or_project, ident.repo_or_slug, ident.pr_id
                if "://" in pr_url:
                    host = urllib.parse.urlparse(pr_url).netloc or None
                if not diff:
                    diff = client.fetch_pull_request_diff(owner, repo, pull_number)
                meta = client.fetch_pull_request_metadata(owner, repo, pull_number)
                pr_metadata = meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
            except Exception as e:
                logger.warning(f"Could not fetch PR data for '{pr_url}': {e}")

        # Route to handlers
        handler_map = {
            "describe": cls._handle_describe,
            "ask": cls._handle_ask,
            "improve": cls._handle_improve,
            "compliance": cls._handle_compliance,
            "ticket": cls._handle_ticket,
            "memory": cls._handle_memory,
            "learn": cls._handle_learn,
            "benchmark": cls._handle_benchmark,
            "help": cls._handle_help,
            "review": cls._handle_review,
        }

        handler = handler_map.get(cmd)
        if not handler:
            response_md = (
                f"⚠️ Unknown command `/{cmd}`.\n\n"
                f"Type `/help` to view all available commands."
            )
            result = BotCommandResult(
                command=cmd,
                status="ERROR",
                response_markdown=response_md,
                action_taken="POST_COMMENT",
                metadata={"error": "unknown_command"}
            )
        else:
            with ExitStack() as stack:
                # For a live PR with no caller-supplied working tree, clone the PR's
                # repo into an isolated temp dir so repo-aware commands index the real
                # codebase — never the server's own working directory.
                effective_root = repo_root
                if effective_root is None and cmd in cls._REPO_CONTEXT_COMMANDS and owner and repo:
                    if platform == "local":
                        # Local adapter already points at a working tree — no clone needed.
                        effective_root = str(getattr(client, "repo_dir", "."))
                    else:
                        effective_root = stack.enter_context(
                            temporary_pr_checkout(
                                owner,
                                repo,
                                head_sha=pr_metadata.get("head_sha"),
                                head_ref=pr_metadata.get("head_ref"),
                                platform=platform,
                                host=host,
                            )
                        )

                try:
                    result = handler(
                        args=args,
                        diff=diff or "",
                        pr_metadata=pr_metadata,
                        repo_root=effective_root,
                        owner=owner,
                        repo=repo,
                        pull_number=pull_number,
                        client=client
                    )
                except Exception as err:
                    logger.error(f"Error handling bot command `/{cmd}`: {err}", exc_info=True)
                    result = BotCommandResult(
                        command=cmd,
                        status="ERROR",
                        response_markdown=f"❌ Error executing `/{cmd}`: {err}",
                        action_taken="POST_COMMENT",
                        metadata={"error": str(err)}
                    )

        # Post back to GitHub if requested and client is available
        if auto_post and client and owner and repo and pull_number:
            try:
                if result.action_taken == "UPDATE_DESCRIPTION":
                    client.update_pull_request_description(
                        owner=owner,
                        repo=repo,
                        pull_number=pull_number,
                        body=result.response_markdown
                    )
                    # Also leave an acknowledgment comment
                    client.post_issue_comment(
                        owner=owner,
                        repo=repo,
                        issue_number=pull_number,
                        body="✅ **PR Description Updated** with automated summary and walkthrough."
                    )
                elif result.action_taken == "POST_COMMENT" and result.response_markdown:
                    client.post_issue_comment(
                        owner=owner,
                        repo=repo,
                        issue_number=pull_number,
                        body=result.response_markdown
                    )
            except Exception as post_err:
                logger.error(f"Failed to post bot response to GitHub: {post_err}")

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Command Handlers
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _handle_describe(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        client: Optional[GitHubClient] = None
    ) -> BotCommandResult:
        """
        Generate PR summary, classification tags, file-by-file walkthrough, and Mermaid diagram.
        """
        if not diff.strip():
            return BotCommandResult(
                command="describe",
                status="ERROR",
                response_markdown="Cannot describe PR: diff content is empty.",
                action_taken="POST_COMMENT",
                metadata={}
            )

        parsed = DiffParser.parse_diff(diff)
        file_list = "\n".join(
            f"- `{f.target_file or f.source_file}` (+{f.added_lines_count}/-{f.deleted_lines_count})"
            for f in parsed.files
        )

        prompt = (
            "You are an expert software engineer documenting a pull request.\n"
            "Analyze the pull request diff below and generate a structured description.\n\n"
            "Include:\n"
            "1. **Summary**: 2-3 sentences explaining the intent and key modifications.\n"
            "2. **Type of Change**: Select from [Bug fix, New feature, Refactoring, Security, Performance, Documentation].\n"
            "3. **Walkthrough Table**: A markdown table with columns | File | Key Changes | Purpose |\n"
            "4. **Mermaid Diagram**: A fenced ```mermaid diagram (flowchart or sequence diagram) illustrating the architectural impact or logic flow.\n\n"
            f"Files Changed:\n{file_list}\n\n"
            f"PR Diff:\n{diff[:30000]}\n"
        )

        llm = LLMFactory.create_llm()
        ai_description = str(llm.call(messages=prompt))

        ai_section = (
            f"## 🤖 PR Description (Automated via `/describe`)\n\n"
            f"{ai_description}\n\n"
            f"---\n"
            f"*Generated by AI Code Review Agent*"
        )

        updating = bool(client and pull_number)
        if updating:
            # Preserve the author's original description; append/replace only the
            # bot-managed block so re-running /describe is idempotent and non-destructive.
            original_body = pr_metadata.get("body", "") or ""
            response_body = cls._merge_description(original_body, ai_section)
        else:
            response_body = ai_section

        return BotCommandResult(
            command="describe",
            status="SUCCESS",
            response_markdown=response_body,
            action_taken="UPDATE_DESCRIPTION" if updating else "POST_COMMENT",
            metadata={"files_changed": parsed.files_changed}
        )

    @staticmethod
    def _merge_description(original_body: str, ai_section: str) -> str:
        """
        Insert or replace the bot-managed block within a PR description without
        touching the author's own text. Idempotent: re-running replaces the previous
        bot block rather than stacking a new one.
        """
        block = f"{_DESC_MARKER_START}\n{ai_section}\n{_DESC_MARKER_END}"
        original_body = original_body or ""
        if _DESC_MARKER_START in original_body and _DESC_MARKER_END in original_body:
            pattern = re.compile(
                re.escape(_DESC_MARKER_START) + r".*?" + re.escape(_DESC_MARKER_END),
                re.DOTALL,
            )
            return pattern.sub(block, original_body, count=1)
        prefix = original_body.rstrip()
        return f"{prefix}\n\n{block}" if prefix else block

    @classmethod
    def _handle_ask(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        **kwargs
    ) -> BotCommandResult:
        """
        Answer arbitrary questions about the PR using diff, AST graph, and Semantic RAG.
        """
        question = args.strip()
        if not question:
            return BotCommandResult(
                command="ask",
                status="ERROR",
                response_markdown="Please provide a question after `/ask`. Example: `/ask Why is this query vulnerable?`",
                action_taken="POST_COMMENT",
                metadata={}
            )
        if len(question) > _MAX_ASK_QUESTION_CHARS:
            question = question[:_MAX_ASK_QUESTION_CHARS].rstrip() + " …"

        # Retrieve AST and Semantic RAG context
        root = repo_root or "."
        semantic_context = ""
        try:
            rag = SemanticContextEngine(repo_roots=[root])
            rag.index(max_files_per_repo=200)
            semantic_context = rag.format_semantic_context(diff or question, top_k=5)
        except Exception as e:
            logger.debug(f"Semantic context lookup in /ask: {e}")

        ast_context = ""
        try:
            indexer = CodeGraphIndexer(repo_root=root)
            indexer.index_repository(max_files=200)
            ast_context = indexer.format_impact_context(diff)
        except Exception as e:
            logger.debug(f"AST context lookup in /ask: {e}")

        prompt = (
            "You are a principal engineer conducting a code review Q&A session on a pull request.\n"
            "Answer the developer's question accurately, citing specific line numbers, functions, and causal mechanisms.\n\n"
            f"Developer's Question: {question}\n\n"
            f"Pull Request Diff:\n{diff[:25000]}\n\n"
            f"AST Call Graph Context:\n{ast_context[:3000]}\n\n"
            f"Semantic Context (from repository):\n{semantic_context[:4000]}\n"
        )

        llm = LLMFactory.create_llm()
        answer = str(llm.call(messages=prompt))

        response_md = (
            f"### 💬 Response to: \"{question}\"\n\n"
            f"{answer}\n\n"
            f"---\n"
            f"<sub>Asked via `/ask` · AI Code Review Agent</sub>"
        )

        return BotCommandResult(
            command="ask",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"question": question}
        )

    @classmethod
    def _handle_improve(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        **kwargs
    ) -> BotCommandResult:
        """
        Generate actionable 1-click GitHub suggestion diffs for detected issues.
        """
        if not diff.strip():
            return BotCommandResult(
                command="improve",
                status="ERROR",
                response_markdown="Cannot suggest improvements: diff is empty.",
                action_taken="POST_COMMENT",
                metadata={}
            )

        # Run fast pre-scans
        sast_findings = SastEngine.scan_diff(diff)
        rules_path = str(os.path.join(repo_root, ".code-review.yaml")) if repo_root else None
        engine = RulesEngine(rules_file_path=rules_path)
        violations = engine.evaluate_diff(diff)

        prompt = (
            "You are a senior developer proposing concrete, 1-click replacement fixes for a PR.\n"
            "Format your code suggestions using standard GitHub suggestion markdown blocks:\n"
            "```suggestion\n"
            "<replacement code>\n"
            "```\n\n"
            "Provide:\n"
            "1. Exact file and line number for each change.\n"
            "2. Clear explanation of why the change is needed.\n"
            "3. The replacement code.\n\n"
            f"SAST Findings:\n{[f.model_dump() for f in sast_findings]}\n\n"
            f"Governance Violations:\n{[v.model_dump() for v in violations]}\n\n"
            f"Diff:\n{diff[:30000]}\n"
        )

        llm = LLMFactory.create_llm()
        suggestions_md = str(llm.call(messages=prompt))

        response_md = (
            f"## 🛠️ Code Improvement Suggestions (`/improve`)\n\n"
            f"{suggestions_md}\n\n"
            f"---\n"
            f"<sub>Use the 'Commit suggestion' button on GitHub to apply approved changes.</sub>"
        )

        return BotCommandResult(
            command="improve",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"findings_count": len(sast_findings) + len(violations)}
        )

    @classmethod
    def _handle_compliance(
        cls,
        args: str,
        diff: str,
        repo_root: Optional[str] = None,
        **kwargs
    ) -> BotCommandResult:
        """
        Evaluate PR against .code-review.yaml rules deterministically.
        """
        rules_path = str(os.path.join(repo_root, ".code-review.yaml")) if repo_root else None
        engine = RulesEngine(rules_file_path=rules_path)
        violations = engine.evaluate_diff(diff)

        blocking = [v for v in violations if v.severity == "BLOCKING"]
        warnings = [v for v in violations if v.severity == "WARNING"]
        info = [v for v in violations if v.severity == "INFO"]

        status_badge = "🟢 **COMPLIANT**" if not blocking and not warnings else (
            "🔴 **NON-COMPLIANT (BLOCKING)**" if blocking else "🟡 **WARNINGS DETECTED**"
        )

        rows = []
        for v in violations:
            rows.append(
                f"| `{v.file_path}` | L{v.line_number} | **{v.severity}** | {v.rule_name} | `{v.suggested_fix}` |"
            )

        table = (
            "| File | Line | Severity | Rule | Suggested Fix |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n" + "\n".join(rows)
            if rows else "All team governance rules passed."
        )

        response_md = (
            f"## 📜 Team Policy & Governance Report (`/compliance`)\n\n"
            f"**Status**: {status_badge}\n\n"
            f"- **Blocking Violations**: {len(blocking)}\n"
            f"- **Warnings**: {len(warnings)}\n"
            f"- **Info Notices**: {len(info)}\n\n"
            f"### Policy Findings\n{table}\n\n"
            f"---\n"
            f"<sub>Evaluated deterministically via `.code-review.yaml` · Zero token cost</sub>"
        )

        return BotCommandResult(
            command="compliance",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"violations": len(violations), "blocking": len(blocking)}
        )

    @classmethod
    def _handle_review(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        **kwargs
    ) -> BotCommandResult:
        """
        Trigger a full multi-agent review flow.

        Reviews the already-fetched diff (platform-agnostic) rather than re-fetching via
        a GitHub-only URL, and lets dispatch post the report back through whichever
        platform client resolved the PR/MR. `repo_root` (the isolated checkout) gives the
        crew real RAG/AST context.
        """
        from code_review_agent.review_service import ReviewService

        if not diff or not diff.strip():
            return BotCommandResult(
                command="review",
                status="ERROR",
                response_markdown="Cannot run review: no diff content available for this PR/MR.",
                action_taken="POST_COMMENT",
                metadata={},
            )

        response = ReviewService.execute_review(raw_diff=diff, repo_root=repo_root)

        return BotCommandResult(
            command="review",
            status="SUCCESS",
            response_markdown=response.report_markdown,
            action_taken="POST_COMMENT",
            metadata={"verdict": response.verdict, "confidence": response.confidence}
        )

    @classmethod
    def _handle_ticket(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        client: Optional[GitHubClient] = None
    ) -> BotCommandResult:
        """Verify PR diff against ticket requirements and acceptance criteria."""
        repo_full = f"{owner}/{repo}" if owner and repo else None
        title = pr_metadata.get("title", "")
        body = pr_metadata.get("body", "")
        branch = pr_metadata.get("head_ref")

        ticket_ref = None
        if args:
            refs = TicketParser.extract_ticket_references(title=args, body="")
            if refs:
                ticket_ref = refs[0]
            else:
                clean_arg = args.strip()
                t_type = "jira" if "-" in clean_arg else "github"
                ticket_ref = TicketReference(ticket_type=t_type, ticket_id=clean_arg, raw_text=clean_arg, source="args")

        if not ticket_ref:
            refs = TicketParser.extract_ticket_references(title=title, body=body, branch=branch)
            if refs:
                ticket_ref = refs[0]

        if not ticket_ref:
            report = IntentComplianceEngine.evaluate(ticket=None, pr_diff=diff)
            return BotCommandResult(
                command="ticket",
                status="SUCCESS",
                response_markdown=report.summary_markdown,
                action_taken="POST_COMMENT",
                metadata={"status": "NO_TICKET"}
            )

        ticket_details = TicketFetcher.fetch_ticket(
            ticket_ref=ticket_ref,
            repo_full_name=repo_full,
            pr_body=body,
            github_client=client
        )

        files = []
        if diff:
            parsed = DiffParser.parse_diff(diff)
            files = [f.target_file for f in parsed.files]

        report = IntentComplianceEngine.evaluate(ticket=ticket_details, pr_diff=diff, files_changed=files)
        return BotCommandResult(
            command="ticket",
            status="SUCCESS",
            response_markdown=report.summary_markdown,
            action_taken="POST_COMMENT",
            metadata={"ticket_id": ticket_details.ticket_id, "status": report.status}
        )

    @classmethod
    def _handle_memory(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        client: Optional[GitHubClient] = None
    ) -> BotCommandResult:
        """View learned team conventions and best practices for this repository."""
        repo_id = f"{owner}/{repo}" if owner and repo else (Path(repo_root).name if repo_root else "default_repo")
        store = TeamMemoryStore()
        category = args.strip().lower() if args else None
        practices = store.get_practices(repo_id, category=category)

        if not practices:
            msg = (
                f"### 🧠 Team Memory for `{repo_id}`\n\n"
                f"No conventions recorded yet for this repository.\n\n"
                f"Use `/learn <rule or standard>` in a comment to teach the agent a team standard."
            )
        else:
            lines = [
                f"### 🧠 Learned Team Conventions for `{repo_id}`",
                f"Found **{len(practices)}** active convention(s) enforced by the agent:\n",
                "| # | Convention | Category | Accepted | Details |",
                "| :-: | :--- | :---: | :---: | :--- |"
            ]
            for i, p in enumerate(practices, 1):
                clean_title = p.title.replace("|", "\\|")
                clean_desc = p.description.replace("|", "\\|")
                lines.append(f"| {i} | **{clean_title}** | `{p.category}` | {p.times_accepted}x | {clean_desc} |")
            lines.append("\n<sub>Use `/learn <rule>` to add new conventions.</sub>")
            msg = "\n".join(lines)

        return BotCommandResult(
            command="memory",
            status="SUCCESS",
            response_markdown=msg,
            action_taken="POST_COMMENT",
            metadata={"repo_id": repo_id, "count": len(practices)}
        )

    @classmethod
    def _handle_learn(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        client: Optional[GitHubClient] = None
    ) -> BotCommandResult:
        """Directly teach the agent a repository coding standard or convention."""
        if not args or len(args.strip()) < 5:
            return BotCommandResult(
                command="learn",
                status="ERROR",
                response_markdown="⚠️ Please provide a convention description. Example: `/learn Always use httpx instead of requests`",
                action_taken="POST_COMMENT",
                metadata={}
            )

        repo_id = f"{owner}/{repo}" if owner and repo else (Path(repo_root).name if repo_root else "default_repo")
        clean_text = args.strip()
        category = "idiom"
        if "security" in clean_text.lower() or "auth" in clean_text.lower():
            category = "security"
        elif "test" in clean_text.lower():
            category = "testing"

        store = TeamMemoryStore()
        practice = store.record_accepted_suggestion(
            repo_id=repo_id,
            title=clean_text[:60],
            description=clean_text,
            good_code="",
            category=category,
            source_pr=f"{repo_id}#{pull_number}" if pull_number else None
        )

        resp = (
            f"### 🧠 Team Memory Updated\n"
            f"Successfully recorded new `{practice.category}` convention for **{repo_id}**:\n\n"
            f"> **{practice.title}**\n\n"
            f"The agent will automatically prioritize and enforce this standard in future reviews."
        )

        return BotCommandResult(
            command="learn",
            status="SUCCESS",
            response_markdown=resp,
            action_taken="POST_COMMENT",
            metadata={"practice_id": practice.id}
        )

    @classmethod
    def _handle_benchmark(
        cls,
        args: str,
        diff: str,
        pr_metadata: Dict[str, Any],
        repo_root: Optional[str] = None,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        pull_number: Optional[int] = None,
        client: Optional[GitHubClient] = None
    ) -> BotCommandResult:
        """Run fast benchmark evaluation and post ground-truth performance card."""
        from code_review_agent.benchmarks import BenchmarkRunner
        runner = BenchmarkRunner()
        metrics = runner.run_deterministic_benchmark()
        f1_pct = metrics.f1_score * 100
        prec_pct = metrics.precision * 100
        rec_pct = metrics.recall * 100
        acc_pct = metrics.verdict_accuracy * 100

        lines = [
            "### 🎯 Ground-Truth Benchmark Results (`/benchmark`)",
            f"Evaluated against **{metrics.total_test_cases} curated ground-truth pull requests**:\n",
            "| Metric | Score | Detail |",
            "| :--- | :---: | :--- |",
            f"| **F1 Score** | **`{f1_pct:.1f}%`** | Precision: `{prec_pct:.1f}%`, Recall: `{rec_pct:.1f}%` |",
            f"| **Verdict Accuracy** | **`{acc_pct:.1f}%`** | {metrics.total_test_cases}/{metrics.total_test_cases} merge verdicts correctly classified |",
            f"| **True Positives** | **`{metrics.true_positives}`** | Flaws correctly identified and grounded |",
            f"| **False Positives** | **`{metrics.false_positives}`** | Noise / over-flagging |",
            f"| **Runtime** | **`{metrics.duration_seconds:.2f}s`** | Zero-cost deterministic pre-scan benchmark |",
            "",
            "> ℹ️ *Measured on this project's own 14-case ground-truth suite. This is not a "
            "head-to-head against other tools — those publish numbers on different, independent "
            "datasets, so the scores are not directly comparable.* Full report: `BENCHMARK_REPORT.md`."
        ]
        summary_md = "\n".join(lines)
        return BotCommandResult(
            command="benchmark",
            status="SUCCESS",
            response_markdown=summary_md,
            action_taken="POST_COMMENT",
            metadata={"f1_score": metrics.f1_score, "accuracy": metrics.verdict_accuracy}
        )

    @classmethod
    def _handle_help(cls, **kwargs) -> BotCommandResult:
        """List supported bot slash commands."""
        help_text = (
            "## 🤖 AI Code Review Agent — Slash Commands\n\n"
            "You can invoke the AI Code Review Agent directly in PR comments using the following commands:\n\n"
            "| Command | Description | Example |\n"
            "| :--- | :--- | :--- |\n"
            "| `/describe` | Generates summary, walkthrough table, and Mermaid diagram | `/describe` |\n"
            "| `/ask <question>` | Ask any technical question about this PR's diff | `/ask Why is mutex needed here?` |\n"
            "| `/improve` | Formats actionable 1-click GitHub commit suggestions | `/improve` |\n"
            "| `/compliance` | Deterministically validates diff against `.code-review.yaml` | `/compliance` |\n"
            "| `/ticket` | Verifies PR diff against linked ticket acceptance criteria | `/ticket #42` |\n"
            "| `/memory` | Displays learned repository coding standards & conventions | `/memory` |\n"
            "| `/learn <rule>` | Directly teaches the agent a repository coding standard | `/learn Always use httpx` |\n"
            "| `/benchmark` | Runs ground-truth benchmark suite and posts accuracy card | `/benchmark` |\n"
            "| `/review` | Triggers full multi-agent code & security review | `/review` |\n"
            "| `/help` | Shows this commands guide | `/help` |\n\n"
            "---\n"
            "<sub>Powered by AI Code Review Agent (v2.0)</sub>"
        )
        return BotCommandResult(
            command="help",
            status="SUCCESS",
            response_markdown=help_text,
            action_taken="POST_COMMENT",
            metadata={}
        )
