"""
Main Flow Orchestrator for the Production AI Code Review System (v2.0).
Integrates AST Code Graph context, Quick Security Pattern Scanner, governance rules engine,
multi-agent crew dispatch, guardrails, SARIF export, telemetry, and live GitHub PRs.
"""

import os
import sys
import json
import uuid
import argparse
import concurrent.futures
from pathlib import Path
from typing import Optional, List, Dict, Any


# Ensure src directory is in sys.path
_src_dir = str(Path(__file__).resolve().parent.parent)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

# Ensure UTF-8 stdout/stderr on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from crewai import LLM
from crewai.flow import Flow, listen, start, router, or_, persist

from code_review_agent.config import get_github_token, get_model_name, logger
from code_review_agent.llm_factory import LLMFactory

from code_review_agent.models import ReviewState, InlineComment, SastFinding, RuleViolation, SummarizedFindingsJSON
from code_review_agent.diff_parser import DiffParser
from code_review_agent.tools import SastEngine
from code_review_agent.sarif_exporter import SarifExporter
from code_review_agent.github_client import GitHubClient
from code_review_agent.context_engine import CodeGraphIndexer
from code_review_agent.governance import RulesEngine
from code_review_agent.observability import TelemetryTracker
from code_review_agent.crews.code_review_crew.crew import CodeReviewCrew
from code_review_agent.flow_parser import RobustLLMOutputParser
from code_review_agent.learning import TeamMemoryStore
from code_review_agent.compliance import TicketParser, TicketFetcher, IntentComplianceEngine


class PRCodeReviewFlow(Flow[ReviewState]):
    """
    Production-grade CrewAI Flow for intelligent, multi-agent Pull Request reviews.
    Supports repository AST context, security pattern scanning, team rules governance, and telemetry.
    """

    _telemetry_tracker: Optional[TelemetryTracker] = None
    _code_graph: Optional[CodeGraphIndexer] = None
    _rules_engine: Optional[RulesEngine] = None

    def _get_llm(self) -> LLM:
        """Initialize configured LLM for flow reasoning via LLMFactory."""
        return LLMFactory.create_llm()


    def _call_llm_with_timeout(self, prompt: str, timeout_seconds: Optional[float] = None) -> str:
        """Execute LLM call with strict timeout and record token telemetry."""
        timeout = timeout_seconds or float(os.environ.get("LLM_REQUEST_TIMEOUT", "60.0"))
        llm = self._get_llm()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(llm.call, messages=prompt)
            try:
                result = future.result(timeout=timeout)
                res_str = str(result)

                # Record token usage telemetry for router, fast-path, or standalone calls
                prompt_toks = max(1, len(prompt) // 4)
                comp_toks = max(1, len(res_str) // 4)
                curr_prompt = self.state.tokens_used.get("prompt_tokens", 0) + prompt_toks
                curr_comp = self.state.tokens_used.get("completion_tokens", 0) + comp_toks
                self.state.tokens_used = {
                    "prompt_tokens": curr_prompt,
                    "completion_tokens": curr_comp,
                    "total_tokens": curr_prompt + curr_comp
                }
                return res_str
            except concurrent.futures.TimeoutError as te:
                logger.error(f"⏱️ LLM call timed out after {timeout}s")
                raise TimeoutError(f"LLM call timed out after {timeout}s") from te


    @start()
    def read_pr_file(self):
        """Load PR diff, index repository AST graph, and execute security pattern & rules pre-scans."""
        logger.info("🔍 Initializing PR Code Review Ingestion...")
        self.state.errors = []
        self.state.final_answer = ""
        self.state.inline_comments = []
        self.state.crew_needed = False
        self._telemetry_tracker = TelemetryTracker(model_name=get_model_name())
        self._telemetry_tracker.start()


        # 1. Handle Live GitHub PR URL
        if self.state.pr_url:
            logger.info(f"🌐 Fetching live Pull Request from GitHub: {self.state.pr_url}")
            try:
                client = GitHubClient()
                owner, repo, pull_num = GitHubClient.parse_pr_identifier(self.state.pr_url)

                self.state.pr_metadata = client.fetch_pull_request_metadata(owner, repo, pull_num)
                self.state.pr_content = client.fetch_pull_request_diff(owner, repo, pull_num)
                logger.info(f"✅ Fetched PR #{pull_num} '{self.state.pr_metadata.get('title')}' ({len(self.state.pr_content)} bytes)")
            except Exception as e:
                error_msg = f"Failed to fetch live GitHub PR '{self.state.pr_url}': {str(e)}"
                logger.error(error_msg)
                self.state.errors.append("Error fetching live GitHub PR")
                self.state.final_answer = error_msg
                return

        # 2. Handle In-Memory PR Diff Content
        elif self.state.pr_content:
            logger.info(f"📝 Loaded in-memory PR diff content ({len(self.state.pr_content)} bytes).")

        # 3. Handle Local File Path
        else:
            pr_file_path = self.state.pr_file_path
            if not pr_file_path:
                error_msg = "Missing 'pr_file_path', 'pr_url', or 'pr_content' in flow state."
                self.state.errors.append(error_msg)
                self.state.final_answer = f"Error: {error_msg}"
                logger.error(self.state.final_answer)
                return

            target_path = Path(pr_file_path)
            if not target_path.exists():
                error_msg = f"PR diff file not found at path: {pr_file_path}"
                self.state.errors.append(error_msg)
                self.state.final_answer = f"Error: {error_msg}"
                logger.error(self.state.final_answer)
                return

            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    self.state.pr_content = f.read()
                logger.info(f"📂 Loaded local PR diff ({len(self.state.pr_content)} bytes).")
            except Exception as e:
                error_msg = f"Error reading file '{pr_file_path}': {str(e)}"
                logger.error(error_msg)
                self.state.errors.append("Error reading local PR file")
                self.state.final_answer = error_msg
                return

        # 4. Parse AST/Diff structure, run SAST scanner, index Code Graph & evaluate custom rules
        try:
            self.state.parsed_pr = DiffParser.parse_diff(self.state.pr_content)
            self.state.sast_findings = SastEngine.scan_diff(self.state.pr_content)

            # Initialize AST Code Graph
            self._code_graph = CodeGraphIndexer(repo_root=self.state.repo_root)
            self._code_graph.index_repository()

            # Initialize Governance Rules Engine
            rules_path = str(Path(self.state.repo_root) / ".code-review.yaml") if self.state.repo_root else None
            self._rules_engine = RulesEngine(rules_file_path=rules_path)
            self.state.rule_violations = self._rules_engine.evaluate_diff(self.state.pr_content)

            logger.info(
                f"📊 Parsed {self.state.parsed_pr.files_changed} file(s) "
                f"(+{self.state.parsed_pr.total_added}/-{self.state.parsed_pr.total_deleted}). "
                f"SAST: {len(self.state.sast_findings)}, Rule Violations: {len(self.state.rule_violations)}"
            )

            # 5. Load repository Team Memory (Best Practices)
            repo_id = "default_repo"
            if self.state.pr_url:
                try:
                    owner, repo, _ = GitHubClient.parse_pr_identifier(self.state.pr_url)
                    repo_id = f"{owner}/{repo}"
                except Exception:
                    repo_id = self.state.pr_url.replace("https://github.com/", "")
            elif self.state.repo_root:
                repo_id = Path(self.state.repo_root).name

            try:
                mem_store = TeamMemoryStore()
                self.state.team_memory_context = mem_store.format_team_memory_prompt(repo_id)
            except Exception as mem_err:
                logger.warning(f"Notice loading team memory for {repo_id}: {mem_err}")
                self.state.team_memory_context = "No team conventions loaded."

            # Ticket & intent compliance is deferred to the COMPLEX crew path
            # (see _evaluate_ticket_compliance) so the sub-second fast-path never
            # pays for its LLM call, and that call inherits the flow's timeout.

        except Exception as e:
            logger.warning(f"Diff parsing / Pre-scan warning: {e}")

    @router(read_pr_file)
    def analyze_changes(self, context=None) -> str:
        """
        Dynamic router: Evaluates PR complexity, SAST vulnerabilities, and Governance violations.
        """
        if len(self.state.errors) > 0:
            logger.warning("Routing to ERROR handler due to pre-existing errors in state.")
            return "ERROR"

        # Check for blocking SAST or governance rules
        has_critical_sast = any(f.severity in ["HIGH", "CRITICAL"] for f in self.state.sast_findings)
        has_blocking_rules = any(r.severity == "BLOCKING" for r in self.state.rule_violations)

        if has_critical_sast or has_blocking_rules:
            logger.info("🚨 Critical SAST findings or BLOCKING governance rules detected; auto-routing to COMPLEX Crew review.")
            self.state.crew_needed = True
            return "COMPLEX"

        logger.info(" Analyzing PR complexity with LLM router...")
        prompt = (
            "Analyze this pull request diff and categorize it as either SIMPLE or COMPLEX.\n"
            "Respond with EXACTLY ONE word: SIMPLE or COMPLEX.\n\n"
            "SIMPLE: Small cosmetic changes, typos, minor refactoring, formatting, comment updates, small doc changes.\n"
            "COMPLEX: New features, bug fixes, logic changes, authentication, database queries, security-relevant modifications, or anything requiring deep review.\n\n"
            f"PR Diff:\n{self.state.pr_content}\n"
        )

        try:
            decision = self._call_llm_with_timeout(prompt, timeout_seconds=30.0).strip()
            logger.info(f"Router verdict: {decision}")
        except Exception as e:
            logger.warning(f"Router LLM call failed or timed out ({e}); defaulting to COMPLEX flow.")
            decision = "COMPLEX"

        if "COMPLEX" in decision.upper():
            self.state.crew_needed = True
            return "COMPLEX"
        else:
            self.state.crew_needed = False
            return "SIMPLE"

    @listen("SIMPLE")
    def simple_review(self):
        """Fast-path review for minor, low-risk changes."""
        logger.info("⚡ Executing fast-path review for simple changes...")

        prompt = (
            "Analyze this pull request diff and evaluate the changes.\n"
            "Do not make assumptions about code outside the provided diff, but offer actionable suggestions.\n"
            "Return a JSON-formatted response with keys:\n"
            "- 'confidence': integer between 0 and 100\n"
            "- 'findings': string summarizing the changes\n"
            "- 'recommendations': list of string suggestions or observations\n\n"
            f"PR Diff:\n{self.state.pr_content}\n"
        )

        try:
            result = self._call_llm_with_timeout(prompt, timeout_seconds=45.0)
            parsed_dict, ok, err = RobustLLMOutputParser.parse_to_dict(result)
            if ok and parsed_dict:
                self.state.review_result = parsed_dict
            else:
                self.state.review_result = {
                    "confidence": 75,
                    "findings": str(result),
                    "recommendations": ["Fast-path review completed with fallback text parsing."]
                }
        except Exception as e:
            logger.warning(f"Fast-path review LLM call failed or timed out: {e}")
            self.state.review_result = {
                "confidence": 60,
                "findings": "Fast-path review completed using pre-scan heuristics.",
                "recommendations": ["Review changes carefully prior to merge."]
            }

        logger.info(" Fast-path review complete.")

    def _evaluate_ticket_compliance(self):
        """
        Parse a linked ticket, fetch its acceptance criteria, and audit the PR against
        them. Runs only on the COMPLEX path so the fast-path never pays for it, and
        routes the LLM call through the flow's timeout+telemetry wrapper (rather than an
        unbounded raw call). Populates state.ticket_context and state.ticket_compliance.
        """
        try:
            pr_title = self.state.pr_metadata.get("title", "")
            pr_body = self.state.pr_metadata.get("body", "")
            branch = self.state.pr_metadata.get("head_ref")
            ticket_refs = TicketParser.extract_ticket_references(title=pr_title, body=pr_body, branch=branch)
            if not ticket_refs:
                self.state.ticket_context = "No linked ticket detected for this PR."
                return

            repo_full_name = None
            if self.state.pr_url:
                try:
                    owner, repo, _ = GitHubClient.parse_pr_identifier(self.state.pr_url)
                    repo_full_name = f"{owner}/{repo}"
                except Exception:
                    repo_full_name = None

            ticket_details = TicketFetcher.fetch_ticket(
                ticket_refs[0], repo_full_name=repo_full_name, pr_body=pr_body
            )
            files_list = [f.target_file for f in self.state.parsed_pr.files] if self.state.parsed_pr else []
            report = IntentComplianceEngine.evaluate(
                ticket=ticket_details,
                pr_diff=self.state.pr_content,
                files_changed=files_list,
                llm_call=lambda p: self._call_llm_with_timeout(p, timeout_seconds=45.0),
            )
            self.state.ticket_compliance = report
            self.state.ticket_context = (
                f"Ticket: {ticket_details.ticket_id} - {ticket_details.title}\n"
                f"Acceptance Criteria:\n" + "\n".join(f"- {c}" for c in ticket_details.acceptance_criteria)
            )
            logger.info(f"🎯 Ticket compliance evaluated for {ticket_details.ticket_id}: {report.status}")
        except Exception as tick_err:
            logger.warning(f"Ticket compliance check notice: {tick_err}")
            self.state.ticket_context = "Ticket compliance check not available."

    @listen("COMPLEX")
    def full_crew_review(self):
        """Dispatches multi-agent crew with AST context, security pattern findings, and team rules."""
        logger.info("🚀 Deploying Multi-Agent Code Review Crew (Senior Dev, Security Eng, Tech Lead)...")

        # Ticket & intent compliance — COMPLEX path only, behind the flow's LLM timeout.
        self._evaluate_ticket_compliance()

        # Format AST Call Graph context
        code_graph_str = self._code_graph.format_impact_context(self.state.pr_content) if self._code_graph else "No AST call graph available."

        semantic_str = "No semantically related code found in the indexed repositories."
        # Semantic RAG retrieval — surface related code from across the codebase
        # (and optional sibling repos) that the diff itself cannot show. Enabled by
        # default; any failure degrades to the AST-only context above.
        if os.environ.get("RAG_ENABLED", "true").strip().lower() not in ("0", "false", "no"):
            try:
                from code_review_agent.context_engine import SemanticContextEngine

                repo_roots = [self.state.repo_root or "."]
                extra = os.environ.get("RAG_REPO_ROOTS", "").strip()
                if extra:
                    repo_roots += [p for p in extra.replace(os.pathsep, ",").split(",") if p.strip()]

                rag_engine = SemanticContextEngine(repo_roots=repo_roots)
                rag_engine.index()
                semantic_str = rag_engine.format_semantic_context(self.state.pr_content, top_k=8)
                code_graph_str = f"{code_graph_str}\n\n{semantic_str}"
                logger.info(f"🧠 Injected semantic RAG context from {len(repo_roots)} repo(s).")
            except Exception as e:
                logger.warning(f"Semantic RAG retrieval unavailable ({e}); continuing with AST-only context.")

        # Format Security Pattern Findings context
        sast_context_str = "\n".join([
            f"- [{f.severity}] {f.rule_id} ({f.cwe}) in {f.file_path}:L{f.line_number}: {f.description} (Fix: {f.fix_recommendation})"
            for f in self.state.sast_findings
        ]) or "No security anti-patterns detected."

        # Format Governance Rules context
        rules_context_str = "\n".join([
            f"- [{r.severity}] {r.rule_id}: {r.rule_name} in {r.file_path}:L{r.line_number} - {r.description} (Suggested fix: {r.suggested_fix})"
            for r in self.state.rule_violations
        ]) or "All custom governance rules passed."

        # Apply adaptive diff chunking if PR content exceeds LLM prompt budget (F8)
        pr_diff_for_crew = self.state.pr_content
        if len(self.state.pr_content) > 35000:
            logger.info(f"📦 Diff size ({len(self.state.pr_content)} chars) exceeds budget; performing adaptive chunking...")
            chunks = DiffParser.chunk_diff_adaptively(self.state.pr_content, max_tokens=6000)
            if chunks:
                summary_header = f"[Adaptive Diff Chunking: {len(chunks)} segments. Primary segments included below]\n\n"
                pr_diff_for_crew = summary_header + "\n\n".join(c["diff_content"] for c in chunks[:3])
                if len(chunks) > 3:
                    pr_diff_for_crew += f"\n\n... [{len(chunks) - 3} additional diff segments omitted from agent prompt] ..."

        try:
            code_review_crew = CodeReviewCrew(repo_root=self.state.repo_root).crew()
            result = code_review_crew.kickoff(
                inputs={
                    "file_content": pr_diff_for_crew,
                    "code_graph_context": code_graph_str,
                    "sast_context": sast_context_str,
                    "rules_context": rules_context_str,
                    "semantic_context": semantic_str,
                    "team_memory_context": self.state.team_memory_context or "No team conventions recorded yet.",
                    "ticket_context": self.state.ticket_context or "No ticket linked.",
                }
            )

            # Robust parse and validate against SummarizedFindingsJSON schema
            findings_model, is_success, parse_error = RobustLLMOutputParser.parse_with_schema(
                result,
                SummarizedFindingsJSON
            )

            self.state.review_result = findings_model.model_dump()

            # Extract inline comments
            for rc in findings_model.inline_comments:
                if rc.path and rc.line:
                    self.state.inline_comments.append(rc)

            # Extract suggested unit tests
            self.state.generated_unit_tests = findings_model.suggested_unit_tests or ""

            # Accumulate token metrics across all lifecycle steps (F8)
            if hasattr(result, "token_usage") and result.token_usage:
                crew_tokens = dict(result.token_usage)
                curr_prompt = self.state.tokens_used.get("prompt_tokens", 0) + crew_tokens.get("prompt_tokens", 0)
                curr_comp = self.state.tokens_used.get("completion_tokens", 0) + crew_tokens.get("completion_tokens", 0)
                self.state.tokens_used = {
                    "prompt_tokens": curr_prompt,
                    "completion_tokens": curr_comp,
                    "total_tokens": curr_prompt + curr_comp
                }

            logger.info(f" Multi-agent crew review completed (parsed: {is_success}). Generated {len(self.state.inline_comments)} inline comment(s).")

        except Exception as e:
            error_msg = f"Error during multi-agent crew execution: {str(e)}"
            logger.error(error_msg)
            self.state.errors.append("Error during crew review")

            # ── Graceful fallback ───────────────────────────────────────────────
            # Crew/LLM failed (likely network or invalid model), but we already
            # have SAST findings and governance violations from the pre-scan.
            # Build a deterministic review_result so make_final_decision can still
            # produce a complete report without a second LLM call.
            critical_findings = [f for f in self.state.sast_findings if f.severity in ("CRITICAL", "HIGH")]
            all_findings_text = "\n".join([
                f"- [{f.severity}] {f.rule_id} in {f.file_path}:L{f.line_number}: {f.description}"
                for f in self.state.sast_findings
            ]) or "No security anti-patterns detected."
            gov_text = "\n".join([
                f"- [{r.severity}] {r.rule_name} in {r.file_path}:L{r.line_number}: {r.description}"
                for r in self.state.rule_violations
            ]) or "All governance rules passed."

            self.state.review_result = {
                "confidence": 20 if critical_findings else 60,
                "findings": (
                    f"Multi-agent LLM crew unavailable (network/model error). "
                    f"Report based on static pre-scan results.\n\n"
                    f"Security Findings:\n{all_findings_text}\n\n"
                    f"Governance Violations:\n{gov_text}"
                ),
                "recommendations": [
                    f.fix_recommendation for f in self.state.sast_findings if f.fix_recommendation
                ] + [
                    r.suggested_fix for r in self.state.rule_violations if r.suggested_fix
                ] or ["Resolve network connectivity to Gemini API and re-run for full AI analysis."]
            }
            logger.warning("⚠️  Crew failed — falling back to pre-scan static analysis report.")

    @listen(or_("simple_review", "full_crew_review"))
    def make_final_decision(self):
        """Synthesize findings into an executive report, record telemetry, and submit to GitHub."""
        logger.info("🧐 Formulating final merge decision and executive review report...")

        review_res = self.state.review_result or {}
        structured_verdict = review_res.get("verdict")
        confidence_val = review_res.get("confidence")

        # ── Empirical Test Sandbox Execution (Phase 3) ──────────────────────
        test_evidence_md = ""
        if self.state.generated_unit_tests:
            logger.info("🧪 Running generated unit tests in isolated subprocess sandbox...")
            try:
                from code_review_agent.sandbox.test_runner import SandboxTestRunner
                test_exec = SandboxTestRunner.run_tests(
                    test_code=self.state.generated_unit_tests,
                    pr_content=self.state.pr_content,
                    repo_root=self.state.repo_root,
                    timeout_seconds=15.0
                )
                self.state.test_execution = test_exec
                badge = f"[{test_exec.evidence_badge}]"
                logger.info(f"🧪 Sandbox test outcome: {badge} - {test_exec.summary_message}")
                test_evidence_md = (
                    f"### 🧪 Empirical Test Evidence `{badge}`\n"
                    f"- **Evidence Badge**: `{test_exec.evidence_badge}`\n"
                    f"- **Execution Status**: `{test_exec.status}`\n"
                    f"- **Runtime**: {test_exec.duration_seconds}s\n"
                    f"- **Details**: {test_exec.summary_message}\n\n"
                )
            except Exception as e:
                logger.warning(f"Could not execute sandbox tests: {e}")

        # ── Deterministic Template Synthesis (F4) ──────────────────────────
        # If Tech Lead produced a structured verdict & confidence, render Markdown directly.
        # This eliminates the 4th LLM call, saves ~2000 tokens per review, and makes decisions reproducible.
        if structured_verdict and confidence_val is not None:
            logger.info(f"✨ Generating deterministic executive markdown from Tech Lead verdict: {structured_verdict} ({confidence_val}/100)")
            findings_text = review_res.get("findings") or "Review completed successfully."
            breakdown = review_res.get("confidence_breakdown") or ""
            breakdown_line = f"\n*Calculation*: `{breakdown}`\n" if breakdown else ""
            blocking_reasons = review_res.get("blocking_reasons") or []
            blocking_block = ""
            if blocking_reasons:
                blocking_block = "### 🚫 Blocking Violations\n" + "\n".join(f"- {b}" for b in blocking_reasons) + "\n\n"

            fixes = review_res.get("fix") or []
            action_items = []
            for item in fixes:
                if isinstance(item, dict):
                    action_items.append(f"- **{item.get('description', 'Fix')}**: {item.get('solutions', '')}")
                else:
                    action_items.append(f"- {item}")
            if not action_items:
                recs = review_res.get("recommendations") or []
                for r in recs:
                    action_items.append(f"- {r}")

            action_items_md = "\n".join(action_items) or "- No critical actions required."

            self.state.final_answer = (
                f"# Pull Request Review Report\n\n"
                f"**Final Decision**: {structured_verdict}\n\n"
                f"**Confidence Score**: {confidence_val}/100{breakdown_line}\n\n"
                f"### Executive Summary\n{findings_text}\n\n"
                f"{blocking_block}"
                f"{test_evidence_md}"
                f"### Required Action Items\n{action_items_md}\n"
            )
            return

        prompt = (
            "Based on the following comprehensive analysis of the pull request, "
            "make a final decision on whether to approve the PR for merging.\n"
            "Guidelines:\n"
            "- Reviews with a confidence score above 85 without blocking security issues can be APPROVED.\n"
            "- If there are actionable issues or missing tests, return REQUEST CHANGES.\n"
            "- If critical vulnerabilities (e.g. SQLi, auth bypass) or architectural flaws exist, return ESCALATE.\n\n"
            "Generate a professional, structured Markdown report including:\n"
            "1. **Final Decision**: APPROVE | REQUEST CHANGES | ESCALATE\n"
            "2. **Confidence Score**: (0-100)\n"
            "3. **Executive Summary**: Key highlights from code quality and security reviews\n"
            "4. **Detailed Findings**: Specific line-by-line issues or vulnerabilities\n"
            "5. **Required Action Items**: Concrete steps required prior to merge approval\n\n"
            f"Review Analysis Data:\n{json.dumps(self.state.review_result, indent=2, default=str)}\n"
            f"Security Pattern Findings (Regex Scan): {len(self.state.sast_findings)}\n"
            f"Governance Rule Violations: {len(self.state.rule_violations)}\n"
        )

        try:
            self.state.final_answer = self._call_llm_with_timeout(prompt, timeout_seconds=60.0)
            logger.info("✅ Final decision LLM call succeeded.")
        except Exception as llm_err:
            logger.error(f"Final decision LLM call failed or timed out: {llm_err}. Building deterministic report from pre-scan data.")
            # ── Deterministic fallback report ───────────────────────────────────
            # Build a structured markdown report entirely from the data we already
            # have (SAST findings + governance violations) so the response is
            # still meaningful even without a successful LLM call.
            has_critical = any(f.severity in ("CRITICAL", "HIGH") for f in self.state.sast_findings)
            has_blocking = any(r.severity == "BLOCKING" for r in self.state.rule_violations)

            if has_critical or has_blocking:
                verdict_line = "ESCALATE"
                confidence = 100
            elif self.state.sast_findings or self.state.rule_violations:
                verdict_line = "REQUEST CHANGES"
                confidence = 80
            else:
                verdict_line = "APPROVE"
                confidence = 90

            findings_rows = "\n".join([
                f"| `{f.file_path}` | {f.line_number} | **{f.severity}** | **{f.rule_id}**: {f.description} |"
                for f in self.state.sast_findings
            ] + [
                f"| `{r.file_path}` | {r.line_number} | **{r.severity}** | **{r.rule_name}**: {r.description} |"
                for r in self.state.rule_violations
            ]) or "| — | — | — | No findings detected. |"

            action_items = "\n".join([
                f"*   {f.fix_recommendation}" for f in self.state.sast_findings if f.fix_recommendation
            ] + [
                f"*   {r.suggested_fix}" for r in self.state.rule_violations if r.suggested_fix
            ]) or "*   No immediate action items — code passes all static checks."

            review_summary = (self.state.review_result or {}).get("findings", "")

            self.state.final_answer = (
                f"# Pull Request Review Report\n\n"
                f"## 1. Final Decision\n**{verdict_line}**\n\n"
                f"## 2. Confidence Score\n**{confidence}**\n\n"
                f"## 3. Executive Summary\n"
                f"{review_summary or 'Review performed via static pre-scan (SAST + Governance Rules). Full AI analysis was unavailable due to a network/model connectivity issue.'}\n\n"
                f"## 4. Detailed Findings\n\n"
                f"| File | Line | Severity | Issue |\n"
                f"| :--- | :--- | :--- | :--- |\n"
                f"{findings_rows}\n\n"
                f"## 5. Required Action Items\n\n"
                f"{action_items}\n\n"
                f"---\n"
                f"> ⚠️ *This report was generated using static analysis only. "
                f"Re-run after resolving `GEMINI_API_KEY` network connectivity for full multi-agent AI review.*"
            )

        # Append Ticket & Intent Compliance card if evaluated
        if self.state.ticket_compliance and getattr(self.state.ticket_compliance, "summary_markdown", None):
            self.state.final_answer += f"\n\n---\n{self.state.ticket_compliance.summary_markdown}"

        # 1. Stop Telemetry & Record Metrics
        if self._telemetry_tracker:
            self.state.telemetry = self._telemetry_tracker.stop(
                token_usage=self.state.tokens_used,
                sast_count=len(self.state.sast_findings),
                rules_count=len(self.state.rule_violations),
                inline_comments_count=len(self.state.inline_comments),
                final_verdict=self.state.final_answer[:50]
            )
            TelemetryTracker.save_metrics_to_file(self.state.telemetry)

        # 2. Export SARIF report if requested
        if self.state.sarif_output_path:
            try:
                saved_sarif = SarifExporter.export_to_file(
                    self.state.sarif_output_path,
                    self.state.sast_findings,
                    self.state.inline_comments
                )
                logger.info(f"🛡️ Exported standard SARIF report to: {saved_sarif}")
            except Exception as e:
                logger.warning(f"Could not export SARIF report: {e}")

        # 3. Submit Live GitHub Review if PR URL was provided and Token is configured
        if self.state.pr_url and get_github_token():
            try:
                logger.info("📤 Submitting automated review and inline comments to GitHub...")
                client = GitHubClient()
                owner, repo, pull_num = GitHubClient.parse_pr_identifier(self.state.pr_url)

                event = "COMMENT"
                if "APPROVE" in self.state.final_answer[:200].upper():
                    event = "APPROVE"
                elif "REQUEST CHANGES" in self.state.final_answer[:200].upper() or "ESCALATE" in self.state.final_answer[:200].upper():
                    event = "REQUEST_CHANGES"

                head_sha = self.state.pr_metadata.get("head_sha")
                client.post_pull_request_review(
                    owner=owner,
                    repo=repo,
                    pull_number=pull_num,
                    event=event,
                    body=self.state.final_answer,
                    commit_id=head_sha,
                    comments=self.state.inline_comments
                )
                self.state.github_review_submitted = True
                logger.info(f"✅ Successfully posted review ({event}) to GitHub PR #{pull_num}!")
            except Exception as e:
                logger.error(f"❌ Failed to submit review to GitHub: {e}")

    @listen(or_("ERROR", "make_final_decision"))
    def return_final_answer(self) -> str:
        """Display and return the final review report and telemetry."""
        print("\n" + "=" * 60)
        print("📋 AUTOMATED CODE REVIEW REPORT")
        print("=" * 60)
        print(self.state.final_answer)
        print("=" * 60)
        if self.state.sast_findings:
            print(f"🚨 Pattern Scanner: Found {len(self.state.sast_findings)} security anti-pattern issue(s).")
        if self.state.rule_violations:
            print(f"⚠️ Governance Rules: Found {len(self.state.rule_violations)} policy violation(s).")
        if self.state.inline_comments:
            print(f"💬 Inline Comments Generated: {len(self.state.inline_comments)}")
        if self.state.telemetry:
            print(TelemetryTracker.format_summary_table(self.state.telemetry))
        print("✨ Multi-Agent Review Lifecycle Finished.\n")
        return self.state.final_answer


def kickoff(
    pr_file_path: Optional[str] = None,
    pr_url: Optional[str] = None,
    raw_diff: Optional[str] = None,
    repo_root: Optional[str] = None,
    sarif_output: Optional[str] = None
) -> PRCodeReviewFlow:
    """Instantiate and execute the Code Review Flow."""
    flow = PRCodeReviewFlow(tracing=True)
    if raw_diff:
        flow.state.pr_content = raw_diff
    if pr_file_path:
        flow.state.pr_file_path = pr_file_path
    if pr_url:
        flow.state.pr_url = pr_url
    if repo_root:
        flow.state.repo_root = repo_root
    if sarif_output:
        flow.state.sarif_output_path = sarif_output

    flow_id = f"pr_review_{uuid.uuid4().hex[:8]}"
    flow.kickoff(inputs={"id": flow_id})

    # Save state for auditing
    output_state_path = Path("flow_state.json")
    try:
        with open(output_state_path, "w", encoding="utf-8") as f:
            json.dump(flow.state.model_dump(), f, indent=2, default=str)
        logger.info(f"Saved flow state to {output_state_path}")
    except Exception as e:
        logger.warning(f"Could not save flow state JSON: {e}")

    return flow


def plot():
    """Generate HTML visual graph of the flow architecture."""
    flow = PRCodeReviewFlow()
    flow.plot()
    logger.info("Flow visualization generated as HTML.")


def cli_main():
    """Command-line interface entry point supporting files, live GitHub PRs, rules, and server mode."""
    parser = argparse.ArgumentParser(
        description="Production Multi-Agent Code Review Agent (v2.0)"
    )
    parser.add_argument(
        "-f", "--file",
        type=str,
        default=None,
        help="Path to local PR diff file (default: samples/sql_injection_pr.txt)"
    )
    parser.add_argument(
        "--pr",
        type=str,
        default=None,
        help="Live GitHub PR URL or identifier (e.g. 'owner/repo/pull/123')"
    )
    parser.add_argument(
        "--sarif",
        type=str,
        default=None,
        help="Export security and review findings to standard SARIF 2.1.0 file"
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Launch the FastAPI Webhook Gateway daemon"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for Webhook Gateway (default: 8000)"
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Generate flow graph visualization HTML"
    )
    parser.add_argument(
        "staged_files",
        nargs="*",
        help=(
            "Optional list of file paths to review directly (no diff file needed). "
            "Populated automatically by pre-commit via .pre-commit-hooks.yaml, which "
            "passes staged filenames positionally. Exits non-zero on ESCALATE / "
            "REQUEST CHANGES so the hook can block the commit."
        )
    )

    args = parser.parse_args()

    if args.server:
        from code_review_agent.webhook_server import start_server
        start_server(port=args.port)
    elif args.plot:
        plot()
    elif args.staged_files and not args.file and not args.pr:
        # pre-commit integration: build a synthetic diff from staged files and
        # exit non-zero on a blocking verdict so git aborts the commit.
        from code_review_agent.review_service import ReviewService

        combined_diff = "\n\n".join(
            ReviewService.file_to_unified_diff(path, Path(path).read_text(encoding="utf-8", errors="replace"))
            for path in args.staged_files
            if Path(path).is_file()
        )
        if not combined_diff.strip():
            logger.warning("No readable staged files to review; skipping.")
            return

        flow = kickoff(raw_diff=combined_diff, sarif_output=args.sarif)
        verdict, confidence, _, _ = ReviewService.extract_verdict_and_confidence(flow.state)
        logger.info(f"Pre-commit verdict: {verdict} (confidence {confidence}/100)")
        if verdict in ("ESCALATE", "REQUEST CHANGES"):
            sys.exit(1)
    else:
        file_target = args.file or ("samples/sql_injection_pr.txt" if not args.pr else None)
        kickoff(
            pr_file_path=file_target,
            pr_url=args.pr,
            sarif_output=args.sarif
        )


if __name__ == "__main__":
    cli_main()
