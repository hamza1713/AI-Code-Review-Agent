"""
Interactive PR Bot & Slash Command Router.
Handles developer commands in PR comments: /describe, /ask, /improve, /compliance, /help, /review.
Enables bi-directional conversation inside GitHub Pull Requests.
"""

import os
import re
import urllib.parse
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_review_agent.bot.checkout import temporary_pr_checkout
from code_review_agent.compliance import (
    IntentComplianceEngine,
    TicketFetcher,
    TicketParser,
    TicketReference,
)
from code_review_agent.config import logger
from code_review_agent.context_engine.code_graph import CodeGraphIndexer
from code_review_agent.context_engine.semantic import SemanticContextEngine
from code_review_agent.diff_parser import DiffParser
from code_review_agent.github_client import GitHubClient
from code_review_agent.governance.rules_engine import RulesEngine
from code_review_agent.learning import TeamMemoryStore
from code_review_agent.llm_factory import LLMFactory
from code_review_agent.tools.sast_scanner import SastEngine

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
    metadata: dict[str, Any]


class CommandRouter:
    """
    Parses and dispatches slash commands from PR comments or REST API.
    """

    COMMAND_PATTERN = re.compile(r"^\s*/([a-zA-Z0-9_\-]+)(?:\s+(.*))?$", re.DOTALL)

    # Commands that reason about the repository (not just the diff) and therefore need
    # a real checkout. For live PRs the router clones one; see temporary_pr_checkout.
    _REPO_CONTEXT_COMMANDS = {
        "ask",
        "improve",
        "compliance",
        "review",
        "ticket",
        "rerun",
        "explain",
        "apply",
    }

    @classmethod
    def is_bot_command(cls, text: str) -> bool:
        """Check whether a comment string begins with a slash command."""
        if not text:
            return False
        first_line = text.strip().split("\n")[0].strip()
        return bool(cls.COMMAND_PATTERN.match(first_line))

    @classmethod
    def parse_command(cls, text: str) -> tuple[str, str]:
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

    MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER"}

    @classmethod
    def check_maintainer_permission(
        cls, command_name: str, pr_metadata: dict[str, Any] | None = None, **kwargs
    ) -> BotCommandResult | None:
        """
        Verify that the user has maintainer permissions for privileged commands
        (/apply, /suppress, /unsuppress).
        Returns None if permitted, or a BotCommandResult with status='ERROR' and
        permission denied message if not permitted.
        """
        if kwargs.get("verified_maintainer") is True:
            return None
        return BotCommandResult(
            command=command_name,
            status="ERROR",
            response_markdown="Permission denied: verified repository maintainer required.",
            action_taken="NONE",
            metadata={"error": "permission_denied"},
        )

    @classmethod
    def dispatch(
        cls,
        command_text: str,
        pr_url: str | None = None,
        raw_diff: str | None = None,
        repo_root: str | None = None,
        auto_post: bool = True,
        author_association: str | None = None,
        is_maintainer: bool | None = None,
        pr_metadata: dict[str, Any] | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """
        Parse command and dispatch to appropriate handler.
        If auto_post is True and pr_url is provided, automatically posts the response back to GitHub.
        """
        from code_review_agent.access_policy import effective_command

        cmd, args = cls.parse_command(command_text)
        effective = effective_command(cmd, args)
        claim = kwargs.get("_job_claim", {})
        if pr_url and (auto_post or effective == "apply"):
            from code_review_agent.webhook_queue import WebhookJobQueue

            WebhookJobQueue().assert_claim(
                claim.get("job_id"), claim.get("claim_token")
            )
        if not cmd:
            return BotCommandResult(
                command="unknown",
                status="IGNORED",
                response_markdown="No valid slash command found.",
                action_taken="NONE",
                metadata={},
            )

        # Resolve diff if missing but PR URL is provided — across GitHub, GitLab,
        # Bitbucket, or a local repo, via the platform adapter factory.
        diff = raw_diff
        pr_meta = dict(pr_metadata or {})
        client = None
        owner, repo, pull_number = None, None, None
        platform = "github"
        host = None

        if pr_url:
            try:
                from code_review_agent.access_policy import (
                    authorize_actor,
                    effective_command,
                    validate_pr_target,
                )

                client, ident, host = validate_pr_target(pr_url)
                platform = ident.platform
                owner, repo, pull_number = (
                    ident.owner_or_project,
                    ident.repo_or_slug,
                    ident.pr_id,
                )
                if "://" in pr_url:
                    host = urllib.parse.urlparse(pr_url).netloc or None
                actor = (
                    "operator"
                    if kwargs.get("trusted_operator")
                    else kwargs.get("actor", "")
                )
                effective = effective_command(cmd, args)
                if not kwargs.get("trusted_operator") and not authorize_actor(
                    client, owner, repo, actor, effective, platform
                ):
                    raise PermissionError("Repository permission denied")
                kwargs["actor"] = actor
                kwargs["provider"] = platform
                kwargs["host"] = host
                kwargs["verified_maintainer"] = bool(
                    kwargs.get("trusted_operator")
                ) or authorize_actor(client, owner, repo, actor, "suppress", platform)
                if auto_post and effective in {"review", "rerun"}:
                    from code_review_agent.webhook_queue import execute_platform_review

                    result = execute_platform_review(
                        {}, pr_url, **kwargs.get("_job_claim", {})
                    )
                    return BotCommandResult(
                        command=cmd,
                        status="SUCCESS",
                        response_markdown=result["full_report"],
                        action_taken="NONE",
                        metadata={"review": result},
                    )
                if effective in {
                    "describe",
                    "ask",
                    "improve",
                    "compliance",
                    "review",
                    "rerun",
                    "explain",
                    "apply",
                }:
                    if not diff:
                        diff = client.fetch_pull_request_diff(owner, repo, pull_number)
                    meta = client.fetch_pull_request_metadata(owner, repo, pull_number)
                    fetched_meta = (
                        meta.model_dump() if hasattr(meta, "model_dump") else dict(meta)
                    )
                    pr_meta = {**fetched_meta, **pr_meta}
            except (PermissionError, ValueError):
                raise
            except Exception as e:
                from code_review_agent.publication import ReconciliationRequired

                if isinstance(e, ReconciliationRequired):
                    raise
                raise RuntimeError("PR retrieval failed") from e

        if author_association is not None:
            pr_meta["author_association"] = author_association
        if is_maintainer is not None:
            pr_meta["is_maintainer"] = is_maintainer

        if (
            not pr_url
            and cmd not in {"help", "benchmark"}
            and not kwargs.get("trusted_operator")
        ):
            raise PermissionError("Verified repository context required")

        from code_review_agent.diff_parser import DiffParser

        DiffParser.parse_diff(diff or "")

        # Route to handlers
        handler_map: dict[str, Any] = {
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
            "suppress": cls._handle_suppress,
            "unsuppress": cls._handle_unsuppress,
            "explain": cls._handle_explain,
            "rerun": cls._handle_rerun,
            "apply": cls._handle_apply,
            "fp": cls._handle_fp,
            "confirm": cls._handle_confirm,
            "calibration": cls._handle_calibration,
            "cried-wolf": cls._handle_calibration,
            "cried_wolf": cls._handle_calibration,
            "fp-rate": cls._handle_calibration,
            "fpr": cls._handle_calibration,
            "audit": cls._handle_audit,
            "audit-sample": cls._handle_audit,
            "audit_sample": cls._handle_audit,
            "audit-confirm": cls._handle_audit_confirm,
            "audit_confirm": cls._handle_audit_confirm,
            "audit-reject": cls._handle_audit_reject,
            "audit_reject": cls._handle_audit_reject,
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
                metadata={"error": "unknown_command"},
            )
        else:
            with ExitStack() as stack:
                # For a live PR with no caller-supplied working tree, clone the PR's
                # repo into an isolated temp dir so repo-aware commands index the real
                # codebase — never the server's own working directory.
                effective_root = repo_root
                if (
                    effective_root is None
                    and effective in cls._REPO_CONTEXT_COMMANDS
                    and owner
                    and repo
                ):
                    if platform == "local":
                        # Local adapter already points at a working tree — no clone needed.
                        effective_root = str(getattr(client, "repo_dir", "."))
                    else:
                        effective_root = stack.enter_context(
                            temporary_pr_checkout(
                                owner,
                                repo,
                                head_sha=pr_meta.get("head_sha"),
                                head_ref=pr_meta.get("head_ref"),
                                platform=platform,
                                host=host,
                            )
                        )

                try:
                    import inspect

                    sig = inspect.signature(handler)
                    has_varkw = any(
                        p.kind == inspect.Parameter.VAR_KEYWORD
                        for p in sig.parameters.values()
                    )
                    call_args = {
                        "args": args,
                        "diff": diff or "",
                        "pr_metadata": pr_meta,
                        "repo_root": effective_root,
                        "owner": owner,
                        "repo": repo,
                        "pull_number": pull_number,
                        "client": client,
                    }
                    if has_varkw:
                        call_args.update(kwargs)
                        if author_association is not None:
                            call_args["author_association"] = author_association
                        if is_maintainer is not None:
                            call_args["is_maintainer"] = is_maintainer
                    filtered_args = {
                        k: v
                        for k, v in call_args.items()
                        if k in sig.parameters or has_varkw
                    }
                    result = handler(**filtered_args)
                except Exception as err:
                    from code_review_agent.path_validation import UnsafeDiffPathError
                    from code_review_agent.publication import ReconciliationRequired

                    if effective == "apply" or isinstance(
                        err,
                        (UnsafeDiffPathError, PermissionError, ReconciliationRequired),
                    ):
                        raise
                    logger.error(
                        f"Error handling bot command `/{cmd}`: {err}", exc_info=True
                    )
                    result = BotCommandResult(
                        command=cmd,
                        status="ERROR",
                        response_markdown=f"❌ Error executing `/{cmd}`: {err}",
                        action_taken="POST_COMMENT",
                        metadata={"error": str(err)},
                    )

        if auto_post and client and owner and repo and pull_number:
            import os

            from code_review_agent.publication import publish
            from code_review_agent.webhook_queue import WebhookJobQueue

            if os.getenv("REVIEW_ENABLE_POSTING", "false").lower() != "true":
                return result
            claim = kwargs.get("_job_claim", {})
            queue = WebhookJobQueue()
            # Description updates are explicitly deferred while the posting gate is closed.
            if result.action_taken == "UPDATE_DESCRIPTION":
                raise PermissionError(
                    "Description mutation requires a separate release gate"
                )
            if result.action_taken == "POST_COMMENT" and result.response_markdown:
                publish(
                    queue,
                    job_id=claim.get("job_id"),
                    claim_token=claim.get("claim_token"),
                    key=f"command:{claim['job_id']}",
                    target=pr_url,
                    payload={"body": result.response_markdown},
                    send=lambda marker: client.post_issue_comment(
                        owner=owner,
                        repo=repo,
                        issue_number=pull_number,
                        body=result.response_markdown + "\n" + marker,
                    ),
                    search=lambda: (
                        client.list_issue_comments(owner, repo, pull_number)
                        if hasattr(client, "list_issue_comments")
                        else []
                    ),
                )

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # Command Handlers
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _handle_describe(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: GitHubClient | None = None,
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
                metadata={},
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
            metadata={"files_changed": parsed.files_changed},
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
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        **kwargs,
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
                metadata={},
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
            f'### 💬 Response to: "{question}"\n\n'
            f"{answer}\n\n"
            f"---\n"
            f"<sub>Asked via `/ask` · AI Code Review Agent</sub>"
        )

        return BotCommandResult(
            command="ask",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"question": question},
        )

    @classmethod
    def _handle_improve(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        **kwargs,
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
                metadata={},
            )

        # Run fast pre-scans
        sast_findings = SastEngine.scan_diff(diff)
        rules_path = (
            str(os.path.join(repo_root, ".code-review.yaml")) if repo_root else None
        )
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
            metadata={"findings_count": len(sast_findings) + len(violations)},
        )

    @classmethod
    def _handle_compliance(
        cls, args: str, diff: str, repo_root: str | None = None, **kwargs
    ) -> BotCommandResult:
        """
        Evaluate PR against .code-review.yaml rules deterministically.
        """
        rules_path = (
            str(os.path.join(repo_root, ".code-review.yaml")) if repo_root else None
        )
        engine = RulesEngine(rules_file_path=rules_path)
        violations = engine.evaluate_diff(diff)

        blocking = [v for v in violations if v.severity == "BLOCKING"]
        warnings = [v for v in violations if v.severity == "WARNING"]
        info = [v for v in violations if v.severity == "INFO"]

        status_badge = (
            "🟢 **COMPLIANT**"
            if not blocking and not warnings
            else (
                "🔴 **NON-COMPLIANT (BLOCKING)**"
                if blocking
                else "🟡 **WARNINGS DETECTED**"
            )
        )

        rows = []
        for v in violations:
            rows.append(
                f"| `{v.file_path}` | L{v.line_number} | **{v.severity}** | {v.rule_name} | `{v.suggested_fix}` |"
            )

        table = (
            "| File | Line | Severity | Rule | Suggested Fix |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n" + "\n".join(rows)
            if rows
            else "All team governance rules passed."
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
            metadata={"violations": len(violations), "blocking": len(blocking)},
        )

    @classmethod
    def _handle_review(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: Any | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """
        Trigger a full multi-agent review flow or dispatch review subcommands
        (/review suppress, /review unsuppress, /review explain, /review rerun).
        """
        clean_args = args.strip()
        tokens = clean_args.split(maxsplit=1)
        sub_name = tokens[0].lower() if tokens else ""
        sub_rest = tokens[1].strip() if len(tokens) > 1 else ""

        if sub_name == "suppress":
            return cls._handle_suppress(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name == "unsuppress":
            return cls._handle_unsuppress(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name == "explain":
            return cls._handle_explain(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name == "rerun":
            return cls._handle_rerun(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name == "apply":
            return cls._handle_apply(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                client=client,
                **kwargs,
            )
        if sub_name == "fp":
            return cls._handle_fp(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name == "confirm":
            return cls._handle_confirm(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )
        if sub_name in ("calibration", "cried-wolf", "cried_wolf", "fp-rate", "fpr"):
            return cls._handle_calibration(
                args=sub_rest,
                diff=diff,
                pr_metadata=pr_metadata,
                repo_root=repo_root,
                owner=owner,
                repo=repo,
                pull_number=pull_number,
                **kwargs,
            )

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
            response_markdown=response.full_report or response.summary or "Review completed.",
            action_taken="POST_COMMENT",
            metadata={
                "verdict": response.verdict,
                "confidence": response.confidence_score,
            },
        )

    @classmethod
    def _handle_suppress(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Suppress a finding fingerprint for the repository."""
        perm_err = cls.check_maintainer_permission(
            "suppress", pr_metadata=pr_metadata, **kwargs
        )
        if perm_err:
            return perm_err
        from code_review_agent.suppression_store import SuppressionStore

        clean = args.strip()
        if not clean:
            return BotCommandResult(
                command="suppress",
                status="ERROR",
                response_markdown=(
                    "⚠️ **Missing Fingerprint**\n\n"
                    "Please specify the finding fingerprint to suppress.\n"
                    "**Example**: `/review suppress 9a4b2f1e Accepted risk in mock test`"
                ),
                action_taken="POST_COMMENT",
                metadata={},
            )

        parts = clean.split(maxsplit=1)
        fingerprint = parts[0].strip().lower()
        reason = parts[1].strip() if len(parts) > 1 else ""
        critical_override = "--critical-override" in reason.split()
        reason = reason.replace("--critical-override", "").strip()
        if not reason:
            raise ValueError("Suppression requires a reason")

        if len(fingerprint) < 4:
            return BotCommandResult(
                command="suppress",
                status="ERROR",
                response_markdown=f"⚠️ Fingerprint `{fingerprint}` is invalid. Expected a hex fingerprint.",
                action_taken="POST_COMMENT",
                metadata={},
            )

        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (Path(repo_root).name if repo_root else "default")
        )
        author = kwargs.get("actor", "operator")

        store = SuppressionStore()
        store.suppress(
            repo_id=repo_id,
            fingerprint=fingerprint,
            reason=reason,
            author=author,
            critical_override=critical_override,
            pr_id=str(pull_number or ""),
        )

        response_md = (
            f"### 🚫 Finding Suppressed\n"
            f"Successfully suppressed finding fingerprint **`{fingerprint}`** for **`{repo_id}`**.\n\n"
            f"- **Reason**: {reason}\n"
            f"- **Author**: @{author}\n"
            f"- **Scope**: Repository `{repo_id}`\n\n"
            f"This finding will be treated as `SUPPRESSED` and will not block PR merges or request changes in subsequent reviews.\n\n"
            f"<sub>To reverse suppression, comment: `/review unsuppress {fingerprint}`</sub>"
        )
        return BotCommandResult(
            command="suppress",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"fingerprint": fingerprint, "repo_id": repo_id, "reason": reason},
        )

    @classmethod
    def _handle_unsuppress(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Unsuppress a previously suppressed finding fingerprint."""
        perm_err = cls.check_maintainer_permission(
            "unsuppress", pr_metadata=pr_metadata, **kwargs
        )
        if perm_err:
            return perm_err
        from code_review_agent.suppression_store import SuppressionStore

        fingerprint = args.strip().lower()
        if not fingerprint:
            return BotCommandResult(
                command="unsuppress",
                status="ERROR",
                response_markdown="⚠️ Please specify the finding fingerprint to unsuppress. Example: `/review unsuppress 9a4b2f1e`",
                action_taken="POST_COMMENT",
                metadata={},
            )

        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (Path(repo_root).name if repo_root else "default")
        )
        store = SuppressionStore()
        unsuppressed = store.unsuppress(repo_id, fingerprint)

        if unsuppressed:
            response_md = (
                f"### 🔓 Finding Unsuppressed\n"
                f"Finding fingerprint **`{fingerprint}`** is no longer suppressed for **`{repo_id}`**.\n\n"
                f"Future reviews will re-evaluate and enforce this rule normally."
            )
        else:
            response_md = f"⚠️ Finding **`{fingerprint}`** was not found in the active suppressions list for **`{repo_id}`**."

        return BotCommandResult(
            command="unsuppress",
            status="SUCCESS" if unsuppressed else "ERROR",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={"fingerprint": fingerprint, "unsuppressed": unsuppressed},
        )

    @classmethod
    def _handle_fp(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Mark a review finding as FALSE_POSITIVE without changing suppressions."""
        from code_review_agent.calibration import (
            FALSE_POSITIVE,
            FindingOutcomeStore,
            RepoCalibrationEngine,
        )

        clean = args.strip()
        if not clean:
            return BotCommandResult(
                command="fp",
                status="ERROR",
                response_markdown=(
                    "⚠️ **Missing Fingerprint**\n\n"
                    "Please specify the finding fingerprint to mark as false positive.\n"
                    "**Example**: `/fp 9a4b2f1e Test fixture false alarm`"
                ),
                action_taken="POST_COMMENT",
                metadata={"error": "missing_fingerprint"},
            )

        parts = clean.split(maxsplit=1)
        raw_fp = parts[0].strip()
        fingerprint = raw_fp.strip("`'\"#:, <>[]()").lower()
        reason = parts[1].strip() if len(parts) > 1 else ""

        if len(fingerprint) < 4 or not re.match(r"^[a-f0-9]+$", fingerprint):
            return BotCommandResult(
                command="fp",
                status="ERROR",
                response_markdown=f"⚠️ Fingerprint `{raw_fp}` is invalid. Expected a hex fingerprint.",
                action_taken="POST_COMMENT",
                metadata={"error": "invalid_fingerprint"},
            )

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )
        author = kwargs.get("actor", "operator")

        # 1. Update outcome in FindingOutcomeStore
        outcome_store = FindingOutcomeStore()
        outcome_store.update_outcome(
            repo_id=repo_id,
            fingerprint=fingerprint,
            outcome=FALSE_POSITIVE,
            reason=reason or "Marked as false positive by developer",
            source="HUMAN_EXPLICIT",
            actor=author,
            pr_id=str(pull_number or ""),
            provider=kwargs.get("provider", "github"),
            host=kwargs.get("host", "github.com"),
        )

        # 3. Retrieve updated repository metrics
        engine = RepoCalibrationEngine(outcome_store)
        metrics = engine.get_metrics(repo_id=repo_id, window_days=30)
        triaged = metrics.confirmed_real + metrics.false_positives
        fpr_pct = f"{metrics.false_positive_rate * 100:.1f}%" if triaged > 0 else "0.0%"

        response_md = (
            f"### 🎯 Finding Marked as False Positive\n\n"
            f"Finding `{fingerprint}` has been recorded as a **False Positive** for repository `{repo_id}`.\n\n"
            f"- **Author**: @{author}\n"
            f"- **Reason**: {reason or 'No specific reason provided'}\n"
            f"- **Updated 30-Day FP Rate**: **{fpr_pct}** (Total Triaged: {triaged})\n\n"
            f"Thank you for calibrating the AI Code Review Agent! Suppression requires a separate maintainer action."
        )

        return BotCommandResult(
            command="fp",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={
                "fingerprint": fingerprint,
                "repo_id": repo_id,
                "reason": reason,
                "outcome": FALSE_POSITIVE,
                "false_positive_rate": metrics.false_positive_rate,
            },
        )

    @classmethod
    def _handle_confirm(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Confirm a review finding as a verified real defect (True Positive)."""
        from code_review_agent.calibration import (
            CONFIRMED_REAL,
            FindingOutcomeStore,
            RepoCalibrationEngine,
        )

        clean = args.strip()
        if not clean:
            return BotCommandResult(
                command="confirm",
                status="ERROR",
                response_markdown=(
                    "⚠️ **Missing Fingerprint**\n\n"
                    "Please specify the finding fingerprint to confirm.\n"
                    "**Example**: `/confirm 9a4b2f1e`"
                ),
                action_taken="POST_COMMENT",
                metadata={"error": "missing_fingerprint"},
            )

        parts = clean.split(maxsplit=1)
        raw_fp = parts[0].strip()
        fingerprint = raw_fp.strip("`'\"#:, <>[]()").lower()
        note = parts[1].strip() if len(parts) > 1 else ""

        if len(fingerprint) < 4 or not re.match(r"^[a-f0-9]+$", fingerprint):
            return BotCommandResult(
                command="confirm",
                status="ERROR",
                response_markdown=f"⚠️ Fingerprint `{raw_fp}` is invalid. Expected a hex fingerprint.",
                action_taken="POST_COMMENT",
                metadata={"error": "invalid_fingerprint"},
            )

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )
        author = kwargs.get("actor", "operator")

        outcome_store = FindingOutcomeStore()
        outcome_store.update_outcome(
            repo_id=repo_id,
            fingerprint=fingerprint,
            outcome=CONFIRMED_REAL,
            reason=note or "Confirmed real defect by developer",
            source="HUMAN_EXPLICIT",
            actor=author,
            pr_id=str(pull_number or ""),
            provider=kwargs.get("provider", "github"),
            host=kwargs.get("host", "github.com"),
        )

        engine = RepoCalibrationEngine(outcome_store)
        metrics = engine.get_metrics(repo_id=repo_id, window_days=30)
        triaged = metrics.confirmed_real + metrics.false_positives
        fpr_pct = f"{metrics.false_positive_rate * 100:.1f}%" if triaged > 0 else "0.0%"

        response_md = (
            f"### ✅ Finding Confirmed Real (True Positive)\n\n"
            f"Finding `{fingerprint}` has been verified as a **Genuine Defect** for repository `{repo_id}`.\n\n"
            f"- **Author**: @{author}\n"
            f"- **Updated 30-Day FP Rate**: **{fpr_pct}** (Total Confirmed: {metrics.confirmed_real})\n\n"
            f"Thank you for verifying this defect! This ground-truth telemetry helps keep our review models sharp and grounded."
        )

        return BotCommandResult(
            command="confirm",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={
                "fingerprint": fingerprint,
                "repo_id": repo_id,
                "outcome": CONFIRMED_REAL,
                "false_positive_rate": metrics.false_positive_rate,
            },
        )

    @classmethod
    def _handle_calibration(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Display historical false-positive rates and Cried-Wolf calibration metrics."""
        from code_review_agent.calibration import (
            FindingOutcomeStore,
            RepoCalibrationEngine,
        )

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )
        window_days = 30
        clean_args = args.strip()
        if clean_args:
            match = re.search(r"(\d+)", clean_args)
            if match:
                try:
                    parsed_days = int(match.group(1))
                    if 1 <= parsed_days <= 365:
                        window_days = parsed_days
                except ValueError:
                    pass

        outcome_store = FindingOutcomeStore()
        engine = RepoCalibrationEngine(outcome_store)
        metrics = engine.get_metrics(repo_id=repo_id, window_days=window_days)
        report_md = engine.generate_markdown_report(metrics)

        return BotCommandResult(
            command="calibration",
            status="SUCCESS",
            response_markdown=report_md,
            action_taken="POST_COMMENT",
            metadata={
                "repo_id": repo_id,
                "window_days": window_days,
                "false_positive_rate": metrics.false_positive_rate,
                "cried_wolf_index": metrics.cried_wolf_index,
                "total_findings": metrics.total_findings,
            },
        )

    @classmethod
    def _handle_audit(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Sample REPRODUCED findings for human audit verification."""
        from code_review_agent.calibration import AuditSampler, FindingOutcomeStore

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )

        sample_size = 5
        clean_args = args.strip()
        if clean_args:
            match = re.search(r"(\d+)", clean_args)
            if match:
                try:
                    parsed_size = int(match.group(1))
                    if 1 <= parsed_size <= 50:
                        sample_size = parsed_size
                except ValueError:
                    pass

        outcome_store = FindingOutcomeStore()
        sampler = AuditSampler(outcome_store)
        sample = sampler.sample_for_audit(
            repo_id=repo_id, sample_size=sample_size, days=30, unaudited_only=True
        )

        if not sample:
            import time
            start_30d = time.time() - 30 * 86400
            all_repro = outcome_store.list_findings(
                repo_id=repo_id, evidence_grade="REPRODUCED", start_time=start_30d
            )
            if not all_repro:
                msg = (
                    f"### 🔬 Human Audit Sampling (`/audit`)\n\n"
                    f"No `REPRODUCED` findings found for `{repo_id}` in the last 30 days.\n"
                    f"Findings labeled with `REPRODUCED` will appear here once generated tests run on pull requests."
                )
            else:
                msg = (
                    f"### 🔬 Human Audit Sampling (`/audit`)\n\n"
                    f"🎉 All {len(all_repro)} `REPRODUCED` finding(s) in the last 30 days have already been audited!\n\n"
                    f"Use `/calibration` to see the current verified reproduction accuracy and validation error rate."
                )
            return BotCommandResult(
                command="audit",
                status="SUCCESS",
                response_markdown=msg,
                action_taken="POST_COMMENT",
                metadata={"repo_id": repo_id, "sample_count": 0},
            )

        lines = [
            f"### 🔬 Stratified Audit Sample for `{repo_id}` ({len(sample)} findings selected)",
            "",
            "To verify whether a generated test actually reproduces the claimed defect, reply with:",
            "- `/audit-confirm <fingerprint> [notes]` — confirms test legitimately reproduces defect",
            "- `/audit-reject <fingerprint> <reason_code> [notes]` — rejects reproduction",
            "",
            "**Valid Reason Codes**: `PROVES_SOMETHING_ELSE`, `ASSERTION_INCORRECT`, `FLAKY_OR_MOCKED`, `FINDING_FALSE`, `OTHER`",
            "",
            "| Fingerprint | Cat / Sev | Claim / Location | Test Status | PR |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ]

        for item in sample:
            fp = item.get("fingerprint", "")
            cat = item.get("category", "QUALITY")
            sev = item.get("severity", "MEDIUM")
            claim = item.get("claim_title") or item.get("reason") or "Defect claim"
            fpath = item.get("file_path") or ""
            line_no = item.get("line_number")
            loc_str = (
                f" `{fpath}:{line_no}`"
                if fpath and line_no
                else (f" `{fpath}`" if fpath else "")
            )
            test_st = item.get("test_status") or "PASSED"
            pr_num = item.get("pr_id") or "-"

            claim_cell = f"**{claim}**{loc_str}"
            lines.append(
                f"| `{fp}` | {cat} / {sev} | {claim_cell} | `{test_st}` | #{pr_num} |"
            )

        lines.extend(
            [
                "",
                "> ℹ️ *Stratified proportional sample across (Category × Severity) strata. Prevents cherry-picking.*",
            ]
        )

        return BotCommandResult(
            command="audit",
            status="SUCCESS",
            response_markdown="\n".join(lines),
            action_taken="POST_COMMENT",
            metadata={
                "repo_id": repo_id,
                "sample_count": len(sample),
                "fingerprints": [s.get("fingerprint") for s in sample],
            },
        )

    @classmethod
    def _handle_audit_confirm(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Confirm that a generated test legitimately reproduces the claimed defect."""
        from code_review_agent.calibration import (
            AUDIT_REASON_CORRECT,
            AUDIT_STATUS_VALID,
            FindingOutcomeStore,
            RepoCalibrationEngine,
        )

        clean = args.strip()
        if not clean:
            return BotCommandResult(
                command="audit-confirm",
                status="ERROR",
                response_markdown=(
                    "⚠️ **Missing Fingerprint**\n\n"
                    "Please specify the finding fingerprint to confirm as a valid reproduction.\n"
                    "**Example**: `/audit-confirm 9a4b2f1e Verified test triggers off-by-one correctly`"
                ),
                action_taken="POST_COMMENT",
                metadata={"error": "missing_fingerprint"},
            )

        parts = clean.split(maxsplit=1)
        raw_fp = parts[0].strip()
        fingerprint = raw_fp.strip("`'\"#:, <>[]()").lower()
        notes = parts[1].strip() if len(parts) > 1 else ""

        if len(fingerprint) < 4 or not re.match(r"^[a-f0-9]+$", fingerprint):
            return BotCommandResult(
                command="audit-confirm",
                status="ERROR",
                response_markdown=f"⚠️ Fingerprint `{raw_fp}` is invalid. Expected a hex fingerprint.",
                action_taken="POST_COMMENT",
                metadata={"error": "invalid_fingerprint"},
            )

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )
        author = (pr_metadata or {}).get("author") or kwargs.get("actor") or "operator"

        outcome_store = FindingOutcomeStore()
        record = outcome_store.record_human_audit(
            repo_id=repo_id,
            fingerprint=fingerprint,
            is_valid=True,
            reason_code=AUDIT_REASON_CORRECT,
            notes=notes,
            audited_by=author,
            pr_id=str(pull_number or ""),
            provider=kwargs.get("provider", "github"),
            host=kwargs.get("host", "github.com"),
        )

        if not record:
            return BotCommandResult(
                command="audit-confirm",
                status="ERROR",
                response_markdown=f"⚠️ Finding `{fingerprint}` was not found in outcome store for repo `{repo_id}`.",
                action_taken="POST_COMMENT",
                metadata={"error": "finding_not_found"},
            )

        engine = RepoCalibrationEngine(outcome_store)
        metrics = engine.get_metrics(repo_id=repo_id, window_days=30)
        ver_pct = (
            f"{metrics.validation_error_rate * 100:.1f}%"
            if metrics.audited_total > 0
            else "0.0%"
        )

        response_md = (
            f"### ✅ REPRODUCED Finding Verified as Valid\n\n"
            f"Finding `{fingerprint}` reproduction has been human-audited and confirmed **VALID** for `{repo_id}`.\n\n"
            f"- **Audited By**: @{author}\n"
            f"- **Audit Status**: `VALID_REPRODUCTION`\n"
            f"- **Notes**: {notes or 'Test verified to accurately demonstrate claimed defect'}\n"
            f"- **Updated 30-Day Validation Error Rate**: **{ver_pct}** (Audited Sample: *n* = {metrics.audited_total}, Valid: {metrics.audited_valid}, Invalid: {metrics.audited_invalid})\n\n"
            f"Thank you for auditing! This directly strengthens calibration and verifiable trust in the `REPRODUCED` label."
        )

        return BotCommandResult(
            command="audit-confirm",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={
                "fingerprint": fingerprint,
                "repo_id": repo_id,
                "audit_status": AUDIT_STATUS_VALID,
                "validation_error_rate": metrics.validation_error_rate,
                "audited_total": metrics.audited_total,
            },
        )

    @classmethod
    def _handle_audit_reject(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Reject a generated reproduction as invalid, distinguishing invalid test vs false positive finding."""
        from code_review_agent.calibration import (
            AUDIT_REASON_ASSERTION_INCORRECT,
            AUDIT_REASON_FINDING_FALSE,
            AUDIT_REASON_FLAKY_OR_MOCKED,
            AUDIT_REASON_OTHER,
            AUDIT_REASON_PROVES_SOMETHING_ELSE,
            AUDIT_STATUS_INVALID,
            FindingOutcomeStore,
            RepoCalibrationEngine,
        )

        clean = args.strip()
        if not clean:
            return BotCommandResult(
                command="audit-reject",
                status="ERROR",
                response_markdown=(
                    "⚠️ **Missing Parameters**\n\n"
                    "Usage: `/audit-reject <fingerprint> <reason_code> [notes]`\n\n"
                    "**Reason Codes**:\n"
                    "- `PROVES_SOMETHING_ELSE`: Test exercises different behavior, not the claimed bug\n"
                    "- `ASSERTION_INCORRECT`: Test asserts incorrect behavior or wrong expected value\n"
                    "- `FLAKY_OR_MOCKED`: Test succeeds due to flawed mocks or test flakiness\n"
                    "- `FINDING_FALSE`: Underlying finding itself is completely false\n"
                    "- `OTHER`: Other reproduction discrepancy\n\n"
                    "**Example**: `/audit-reject 9a4b2f1e PROVES_SOMETHING_ELSE Tests null check instead of buffer overflow`"
                ),
                action_taken="POST_COMMENT",
                metadata={"error": "missing_parameters"},
            )

        parts = clean.split(maxsplit=2)
        raw_fp = parts[0].strip()
        fingerprint = raw_fp.strip("`'\"#:, <>[]()").lower()

        if len(fingerprint) < 4 or not re.match(r"^[a-f0-9]+$", fingerprint):
            return BotCommandResult(
                command="audit-reject",
                status="ERROR",
                response_markdown=f"⚠️ Fingerprint `{raw_fp}` is invalid. Expected a hex fingerprint.",
                action_taken="POST_COMMENT",
                metadata={"error": "invalid_fingerprint"},
            )

        raw_reason = parts[1].strip().upper() if len(parts) > 1 else "OTHER"
        notes = parts[2].strip() if len(parts) > 2 else ""

        reason_map = {
            "PROVES_SOMETHING_ELSE": AUDIT_REASON_PROVES_SOMETHING_ELSE,
            "TEST_PROVES_SOMETHING_ELSE": AUDIT_REASON_PROVES_SOMETHING_ELSE,
            "SOMETHING_ELSE": AUDIT_REASON_PROVES_SOMETHING_ELSE,
            "ASSERTION_INCORRECT": AUDIT_REASON_ASSERTION_INCORRECT,
            "WRONG_ASSERTION": AUDIT_REASON_ASSERTION_INCORRECT,
            "FLAKY_OR_MOCKED": AUDIT_REASON_FLAKY_OR_MOCKED,
            "TEST_FLAKY_OR_MOCKED": AUDIT_REASON_FLAKY_OR_MOCKED,
            "FLAKY": AUDIT_REASON_FLAKY_OR_MOCKED,
            "FINDING_FALSE": AUDIT_REASON_FINDING_FALSE,
            "FINDING_IS_FALSE": AUDIT_REASON_FINDING_FALSE,
            "FALSE_ALARM": AUDIT_REASON_FINDING_FALSE,
            "OTHER": AUDIT_REASON_OTHER,
        }
        canonical_reason = reason_map.get(raw_reason)
        if canonical_reason is None:
            raise ValueError("Invalid audit reason code")

        raw_repo_root_name = Path(repo_root).name if repo_root else ""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (
                kwargs.get("repo_id")
                or (raw_repo_root_name if raw_repo_root_name else "default")
            )
        )
        author = (pr_metadata or {}).get("author") or kwargs.get("actor") or "operator"

        outcome_store = FindingOutcomeStore()
        record = outcome_store.record_human_audit(
            repo_id=repo_id,
            fingerprint=fingerprint,
            is_valid=False,
            reason_code=canonical_reason,
            notes=notes,
            audited_by=author,
            pr_id=str(pull_number or ""),
            provider=kwargs.get("provider", "github"),
            host=kwargs.get("host", "github.com"),
        )

        if not record:
            return BotCommandResult(
                command="audit-reject",
                status="ERROR",
                response_markdown=f"⚠️ Finding `{fingerprint}` was not found in outcome store for repo `{repo_id}`.",
                action_taken="POST_COMMENT",
                metadata={"error": "finding_not_found"},
            )

        is_false_finding = canonical_reason == AUDIT_REASON_FINDING_FALSE
        engine = RepoCalibrationEngine(outcome_store)
        metrics = engine.get_metrics(repo_id=repo_id, window_days=30)
        ver_pct = (
            f"{metrics.validation_error_rate * 100:.1f}%"
            if metrics.audited_total > 0
            else "0.0%"
        )

        outcome_label = (
            "**FALSE_POSITIVE**" if is_false_finding else "**INVALID_REPRODUCTION**"
        )
        notice = (
            "Finding was determined fundamentally false."
            if is_false_finding
            else "The test was determined not to prove the claimed defect; finding remains distinguished from a pure false positive."
        )
        response_md = (
            f"### ❌ REPRODUCED Finding Audit: Rejected ({outcome_label})\n\n"
            f"Reproduction for finding `{fingerprint}` has been recorded as **INVALID** for `{repo_id}`.\n\n"
            f"- **Audited By**: @{author}\n"
            f"- **Audit Status**: `INVALID_REPRODUCTION`\n"
            f"- **Reason Code**: `{canonical_reason}`\n"
            f"- **Notes**: {notes or 'No specific audit notes'}\n"
            f"- **Outcome Recorded**: `{record.get('outcome', '')}`\n"
            f"- **Updated 30-Day Validation Error Rate**: **{ver_pct}** (Audited Sample: *n* = {metrics.audited_total}, Valid: {metrics.audited_valid}, Invalid: {metrics.audited_invalid})\n\n"
            f"{notice}"
        )

        return BotCommandResult(
            command="audit-reject",
            status="SUCCESS",
            response_markdown=response_md,
            action_taken="POST_COMMENT",
            metadata={
                "fingerprint": fingerprint,
                "repo_id": repo_id,
                "audit_status": AUDIT_STATUS_INVALID,
                "reason_code": canonical_reason,
                "outcome": record.get("outcome", ""),
                "validation_error_rate": metrics.validation_error_rate,
                "audited_total": metrics.audited_total,
            },
        )

    @classmethod
    def _handle_explain(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Provide detailed root cause explanation and remediation for a finding fingerprint."""
        from code_review_agent.tools.ast_security_scanner import AstSecurityScanner
        from code_review_agent.tools.sast_scanner import SastEngine

        fingerprint = args.strip().lower()
        if not fingerprint:
            return BotCommandResult(
                command="explain",
                status="ERROR",
                response_markdown="⚠️ Please specify a finding fingerprint to explain. Example: `/review explain 9a4b2f1e`",
                action_taken="POST_COMMENT",
                metadata={},
            )

        # Look up finding across SAST and AST scanners
        matched_finding = None
        sast_findings = SastEngine().scan_diff(diff)
        for f in sast_findings:
            if (f.fingerprint or "").lower().startswith(fingerprint):
                matched_finding = f
                break

        if not matched_finding:
            ast_findings = AstSecurityScanner.scan_diff(diff)
            for f in ast_findings:
                if (f.fingerprint or "").lower().startswith(fingerprint):
                    matched_finding = f
                    break

        if matched_finding:
            response_md = (
                f"### 🔍 Deep Finding Explanation (`{matched_finding.fingerprint}`)\n\n"
                f"**Rule**: `{matched_finding.rule_id}` ({matched_finding.name or 'Security/Quality Violation'})\n"
                f"**Location**: `{matched_finding.file_path}:L{matched_finding.line_number}`\n"
                f"**Severity**: `{matched_finding.severity}`"
                + (
                    f" | **CWE**: `{matched_finding.cwe}`"
                    if matched_finding.cwe
                    else ""
                )
                + f"\n\n#### 📌 Why was this flagged?\n{matched_finding.description}\n\n"
            )
            if matched_finding.snippet:
                response_md += f"#### 🧪 Affected Snippet\n```python\n{matched_finding.snippet}\n```\n\n"
            if matched_finding.fix_recommendation:
                response_md += (
                    f"#### 💡 Recommended Fix\n{matched_finding.fix_recommendation}\n\n"
                )
            response_md += (
                f"---\n"
                f"<sub>To suppress this finding if it is an accepted risk or false positive, run: "
                f"`/review suppress {matched_finding.fingerprint} <reason>`</sub>"
            )
            return BotCommandResult(
                command="explain",
                status="SUCCESS",
                response_markdown=response_md,
                action_taken="POST_COMMENT",
                metadata={
                    "fingerprint": matched_finding.fingerprint,
                    "rule_id": matched_finding.rule_id,
                },
            )

        return BotCommandResult(
            command="explain",
            status="ERROR",
            response_markdown=(
                f"⚠️ Could not locate an active finding with fingerprint `{fingerprint}` in this PR's diff.\n"
                f"The finding may have already been resolved, or the fingerprint may belong to another PR."
            ),
            action_taken="POST_COMMENT",
            metadata={"fingerprint": fingerprint},
        )

    @classmethod
    def _handle_rerun(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Trigger an expedited full re-review."""
        return cls._handle_review(
            args="",
            diff=diff,
            pr_metadata=pr_metadata,
            repo_root=repo_root,
            owner=owner,
            repo=repo,
            pull_number=pull_number,
            **kwargs,
        )

    @classmethod
    def _handle_apply(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: Any | None = None,
        **kwargs,
    ) -> BotCommandResult:
        """Apply 1-click remediation commit directly to PR branch."""
        perm_err = cls.check_maintainer_permission(
            "apply", pr_metadata=pr_metadata, **kwargs
        )
        if perm_err:
            return perm_err
        from code_review_agent.remediator import AutoRemediator

        fingerprint = args.strip().lower()
        if not fingerprint:
            return BotCommandResult(
                command="apply",
                status="ERROR",
                response_markdown="⚠️ Please specify the finding fingerprint to apply. Example: `/review apply 9a4b2f1e`",
                action_taken="POST_COMMENT",
                metadata={},
            )

        pr_ident = (
            f"{owner}/{repo}#{pull_number}"
            if owner and repo and pull_number
            else (pr_metadata.get("html_url") or "local")
        )
        actor_name = pr_metadata.get("author") or kwargs.get("sender") or "maintainer"
        reviewed_head_sha = pr_metadata.get("head_sha")
        result = AutoRemediator.apply_remediation(
            pr_identifier=pr_ident,
            fingerprint=fingerprint,
            client=client,
            repo_root=repo_root,
            reviewed_head=reviewed_head_sha,
            actor=actor_name,
            custom_message=kwargs.get("custom_message"),
            replacement_code=kwargs.get("replacement_code"),
            **kwargs.get("_job_claim", {}),
        )

        if result.get("status") == "success":
            commit_sha = result.get("commit_sha", "")
            commit_url = result.get("commit_url", "")
            file_path = result.get("file_path", "")
            branch = result.get("branch", "")
            sha_display = commit_sha[:8] if commit_sha else "committed"
            commit_link = (
                f"[{sha_display}]({commit_url})" if commit_url else f"`{sha_display}`"
            )

            response_md = (
                f"### 🚀 1-Click Auto-Remediation Applied!\n\n"
                f"Successfully committed verified remediation for finding **`{fingerprint}`** (`{result.get('rule_id', '')}`):\n\n"
                f"- **Target File**: `{file_path}`\n"
                f"- **Branch**: `{branch}`\n"
                f"- **Commit**: {commit_link}\n\n"
                f"A remediation was committed; confirmation and suppression are separate actions."
            )
            return BotCommandResult(
                command="apply",
                status="SUCCESS",
                response_markdown=response_md,
                action_taken="POST_COMMENT",
                metadata=result,
            )
        else:
            err_msg = result.get("message", "Unknown remediation error")
            response_md = (
                f"⚠️ Unable to apply auto-remediation for `{fingerprint}`:\n\n"
                f"> {err_msg}\n\n"
                f"Please review the finding details and apply the recommended fix manually."
            )
            return BotCommandResult(
                command="apply",
                status="ERROR",
                response_markdown=response_md,
                action_taken="POST_COMMENT",
                metadata=result,
            )

    @classmethod
    def _handle_ticket(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: GitHubClient | None = None,
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
                ticket_ref = TicketReference(
                    ticket_type=t_type,
                    ticket_id=clean_arg,
                    raw_text=clean_arg,
                    source="args",
                )

        if not ticket_ref:
            refs = TicketParser.extract_ticket_references(
                title=title, body=body, branch=branch
            )
            if refs:
                ticket_ref = refs[0]

        if not ticket_ref:
            report = IntentComplianceEngine.evaluate(ticket=None, pr_diff=diff)
            return BotCommandResult(
                command="ticket",
                status="SUCCESS",
                response_markdown=report.summary_markdown,
                action_taken="POST_COMMENT",
                metadata={"status": "NO_TICKET"},
            )

        ticket_details = TicketFetcher.fetch_ticket(
            ticket_ref=ticket_ref,
            repo_full_name=repo_full,
            pr_body=body,
            github_client=client,
        )

        files = []
        if diff:
            parsed = DiffParser.parse_diff(diff)
            files = [f.target_file for f in parsed.files]

        report = IntentComplianceEngine.evaluate(
            ticket=ticket_details, pr_diff=diff, files_changed=files
        )
        return BotCommandResult(
            command="ticket",
            status="SUCCESS",
            response_markdown=report.summary_markdown,
            action_taken="POST_COMMENT",
            metadata={"ticket_id": ticket_details.ticket_id, "status": report.status},
        )

    @classmethod
    def _handle_memory(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: GitHubClient | None = None,
    ) -> BotCommandResult:
        """View learned team conventions and best practices for this repository."""
        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (Path(repo_root).name if repo_root else "default_repo")
        )
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
                "| :-: | :--- | :---: | :---: | :--- |",
            ]
            for i, p in enumerate(practices, 1):
                clean_title = p.title.replace("|", "\\|")
                clean_desc = p.description.replace("|", "\\|")
                lines.append(
                    f"| {i} | **{clean_title}** | `{p.category}` | {p.times_accepted}x | {clean_desc} |"
                )
            lines.append("\n<sub>Use `/learn <rule>` to add new conventions.</sub>")
            msg = "\n".join(lines)

        return BotCommandResult(
            command="memory",
            status="SUCCESS",
            response_markdown=msg,
            action_taken="POST_COMMENT",
            metadata={"repo_id": repo_id, "count": len(practices)},
        )

    @classmethod
    def _handle_learn(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: GitHubClient | None = None,
    ) -> BotCommandResult:
        """Directly teach the agent a repository coding standard or convention."""
        if not args or len(args.strip()) < 5:
            return BotCommandResult(
                command="learn",
                status="ERROR",
                response_markdown="⚠️ Please provide a convention description. Example: `/learn Always use httpx instead of requests`",
                action_taken="POST_COMMENT",
                metadata={},
            )

        repo_id = (
            f"{owner}/{repo}"
            if owner and repo
            else (Path(repo_root).name if repo_root else "default_repo")
        )
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
            source_pr=f"{repo_id}#{pull_number}" if pull_number else None,
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
            metadata={"practice_id": practice.id},
        )

    @classmethod
    def _handle_benchmark(
        cls,
        args: str,
        diff: str,
        pr_metadata: dict[str, Any],
        repo_root: str | None = None,
        owner: str | None = None,
        repo: str | None = None,
        pull_number: int | None = None,
        client: GitHubClient | None = None,
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
            "### 🎯 Scanner Regression Results (`/benchmark`)",
            f"Evaluated against **{metrics.total_test_cases} synthetic diff fixtures**, scorer version 2:\n",
            "| Metric | Score | Detail |",
            "| :--- | :---: | :--- |",
            f"| **F1 Score** | **`{f1_pct:.1f}%`** | Precision: `{prec_pct:.1f}%`, Recall: `{rec_pct:.1f}%` |",
            f"| **Verdict Accuracy** | **`{acc_pct:.1f}%`** | Output-only binary flagging; not merge authorization |",
            f"| **True Positives** | **`{metrics.true_positives}`** | Flaws correctly identified and grounded |",
            f"| **False Positives** | **`{metrics.false_positives}`** | Noise / over-flagging |",
            f"| **Runtime** | **`{metrics.duration_seconds:.2f}s`** | Zero-cost deterministic pre-scan benchmark |",
            "",
            "> ℹ️ *Measured on this project's own 14-case ground-truth suite. This is not a "
            "head-to-head against other tools — those publish numbers on different, independent "
            "datasets, so the scores are not directly comparable.* Full report: `BENCHMARK_REPORT.md`.",
        ]
        summary_md = "\n".join(lines)
        return BotCommandResult(
            command="benchmark",
            status="SUCCESS",
            response_markdown=summary_md,
            action_taken="POST_COMMENT",
            metadata={
                "f1_score": metrics.f1_score,
                "accuracy": metrics.verdict_accuracy,
            },
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
            "| `/review rerun` | Triggers expedited full re-review on current PR head | `/review rerun` |\n"
            "| `/review suppress <fp> [reason]` | Suppresses finding fingerprint for this repository | `/review suppress 9a4b2f1e Mock test` |\n"
            "| `/review unsuppress <fp>` | Re-enables inspection for finding fingerprint | `/review unsuppress 9a4b2f1e` |\n"
            "| `/review explain <fp>` | Provides deep root-cause explanation and remediation | `/review explain 9a4b2f1e` |\n"
            "| `/review apply <fp>` | Commits 1-click verified remediation directly to PR branch | `/review apply 9a4b2f1e` |\n"
            "| `/fp <fp> [reason]` | Marks finding as false positive; suppression is separate | `/fp 9a4b2f1e Test fixture` |\n"
            "| `/confirm <fp>` | Confirms finding as a genuine defect (True Positive) | `/confirm 9a4b2f1e` |\n"
            "| `/calibration` | Displays false-positive rates & Cried-Wolf telemetry | `/calibration` |\n"
            "| `/audit [n]` | Stratified sampling of REPRODUCED findings for audit | `/audit 10` |\n"
            "| `/audit-confirm <fp>` | Confirms generated reproduction is valid | `/audit-confirm 9a4b2f1e` |\n"
            "| `/audit-reject <fp> <reason>` | Rejects reproduction as invalid test or false finding | `/audit-reject 9a4b2f1e PROVES_SOMETHING_ELSE` |\n"
            "| `/help` | Shows this commands guide | `/help` |\n\n"
            "---\n"
            "<sub>Powered by AI Code Review Agent (v2.0)</sub>"
        )
        return BotCommandResult(
            command="help",
            status="SUCCESS",
            response_markdown=help_text,
            action_taken="POST_COMMENT",
            metadata={},
        )
