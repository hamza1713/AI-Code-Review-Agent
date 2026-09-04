"""
Multi-Agent Code Review Crew definition.
Coordinates Senior Developer, Security Engineer, and Tech Lead agents
with AST Code Graph context, Quick Security Pattern Scanner, and custom rules governance.

Skill → Agent Assignments (from config/agents.yaml):
  senior_developer   : senior-dev-quality-reviewer, ast-callgraph-context-indexer,
                       api-breaking-change-detector, performance-and-concurrency-auditor
  security_engineer  : sast-vulnerability-auditor, git-diff-and-patch-analyzer
  tech_lead          : tech-lead-verdict-synthesizer, automated-unit-test-generator,
                       governance-policy-enforcer
"""

import os
from typing import List, Optional, Any, Dict
from crewai import Agent, Crew, Process, Task, LLM

from crewai.project import CrewBase, agent, crew, task
from crewai_tools import SerperDevTool, ScrapeWebsiteTool

from code_review_agent.llm_factory import LLMFactory
from code_review_agent.crews.code_review_crew.tool_registry import ToolRegistry
from code_review_agent.models import (
    CodeQualityJSON,
    ReviewSecurityJSON,
    SummarizedFindingsJSON,
)
from code_review_agent.crews.code_review_crew.guardrails import security_review_output_guardrail


@CrewBase
class CodeReviewCrew:
    """Production Multi-Agent Code Review Crew with AST Context, Dynamic Tools, and Governance Tooling."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def __init__(self, repo_root: Optional[str] = None):
        self.repo_root = repo_root

    def _get_llm(self) -> LLM:
        """Initialize configured LLM for crew agents using LLMFactory."""
        return LLMFactory.create_llm()

    def _get_agent_tools(self, agent_name: str, fallback_tools: Optional[List[Any]] = None) -> List[Any]:
        """Resolve tools dynamically from agents.yaml 'assigned_tools' field via ToolRegistry."""
        agent_cfg = self.agents_config.get(agent_name, {})
        configured_tools = agent_cfg.get("assigned_tools", []) or agent_cfg.get("tools", [])
        if configured_tools:
            resolved = ToolRegistry.resolve_tools(configured_tools, repo_root=getattr(self, "repo_root", None))
            if resolved:
                return resolved
        return fallback_tools or []

    def _get_agent_skills(self, agent_name: str) -> List[str]:
        """
        Read assigned_skills list from agents.yaml for a given agent.
        Skills are procedural reasoning protocols (SKILL.md) — not executable tools.
        They are injected into agent backstory context at crew build time.
        """
        agent_cfg = self.agents_config.get(agent_name, {})
        return agent_cfg.get("assigned_skills", [])

    def _build_skill_context(self, skill_ids: List[str]) -> str:
        """
        Build a compact skill-context preamble that is prepended to agent backstory.
        This ensures the agent LLM is aware of its active skill protocols.
        """
        if not skill_ids:
            return ""
        skill_list = "\n".join(f"  - [{s}]" for s in skill_ids)
        return (
            f"\n\n[ACTIVE SKILLS — Apply these reasoning protocols during your task]\n"
            f"{skill_list}\n"
            f"Refer to each skill by its bracketed identifier when describing\n"
            f"which protocol you are applying in your output reasoning.\n"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # AGENT DEFINITIONS
    # Each agent: loads its tools from assigned_tools + skills from assigned_skills
    # ─────────────────────────────────────────────────────────────────────────

    @agent
    def senior_developer(self) -> Agent:
        """
        Senior Developer agent.
        Active Skills: senior-dev-quality-reviewer, ast-callgraph-context-indexer,
                       api-breaking-change-detector, performance-and-concurrency-auditor
        Active Tools:  CodebaseContextTool (AST call graph queries), RuffTool (fast linting)
        """
        cfg = dict(self.agents_config["senior_developer"])
        tools = self._get_agent_tools("senior_developer")
        skills = self._get_agent_skills("senior_developer")
        skill_ctx = self._build_skill_context(skills)

        # Inject skill context into backstory so the LLM knows its active protocols
        cfg["backstory"] = cfg.get("backstory", "") + skill_ctx

        # Remove raw tool/skill config keys before passing to Agent constructor
        cfg.pop("tools", None)
        cfg.pop("assigned_tools", None)
        cfg.pop("assigned_skills", None)

        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def security_engineer(self) -> Agent:
        """
        Security Engineer agent.
        Active Skills: sast-vulnerability-auditor, git-diff-and-patch-analyzer
        Active Tools:  QuickPatternScannerTool (Semgrep+Bandit+Regex unified scanner),
                       SerperDevTool (CVE/OWASP web search),
                       ScrapeWebsiteTool (vulnerability detail scraping)
        """
        cfg = dict(self.agents_config["security_engineer"])
        tools = self._get_agent_tools("security_engineer")
        skills = self._get_agent_skills("security_engineer")
        skill_ctx = self._build_skill_context(skills)

        cfg["backstory"] = cfg.get("backstory", "") + skill_ctx

        cfg.pop("tools", None)
        cfg.pop("assigned_tools", None)
        cfg.pop("assigned_skills", None)

        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def tech_lead(self) -> Agent:
        """
        Tech Lead agent.
        Active Skills: tech-lead-verdict-synthesizer, automated-unit-test-generator,
                       governance-policy-enforcer
        Active Tools:  CustomRulesTool (.code-review.yaml evaluator),
                       TestGeneratorTool (pytest AST suite generator)
        """
        cfg = dict(self.agents_config["tech_lead"])
        tools = self._get_agent_tools("tech_lead")
        skills = self._get_agent_skills("tech_lead")
        skill_ctx = self._build_skill_context(skills)

        cfg["backstory"] = cfg.get("backstory", "") + skill_ctx

        cfg.pop("tools", None)
        cfg.pop("assigned_tools", None)
        cfg.pop("assigned_skills", None)

        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # TASK DEFINITIONS
    # Each task maps to the agent above and specifies the output schema.
    # Tasks run async (senior_developer + security_engineer in parallel),
    # then tech_lead synthesizes sequentially with both outputs as context.
    # ─────────────────────────────────────────────────────────────────────────

    @task
    def analyze_code_quality(self) -> Task:
        """
        Task: Code quality review by Senior Developer.
        Skills applied: senior-dev-quality-reviewer, ast-callgraph-context-indexer,
                        api-breaking-change-detector, performance-and-concurrency-auditor
        Runs: async (parallel with review_security)
        Output schema: CodeQualityJSON
        """
        return Task(
            config=self.tasks_config["analyze_code_quality"],
            output_json=CodeQualityJSON,
            async_execution=True,
        )

    @task
    def review_security(self) -> Task:
        """
        Task: Security vulnerability audit by Security Engineer.
        Skills applied: sast-vulnerability-auditor, git-diff-and-patch-analyzer
        Runs: async (parallel with analyze_code_quality)
        Output schema: ReviewSecurityJSON
        Guardrails: security_review_output_guardrail (validates highest_risk, deduplication)
        """
        return Task(
            config=self.tasks_config["review_security"],
            output_json=ReviewSecurityJSON,
            async_execution=True,
            guardrails=[security_review_output_guardrail],
        )

    @task
    def summarize_findings(self) -> Task:
        """
        Task: Synthesis and verdict by Tech Lead.
        Skills applied: tech-lead-verdict-synthesizer, automated-unit-test-generator,
                        governance-policy-enforcer
        Runs: sequential (after both async tasks complete)
        Context: analyze_code_quality + review_security outputs
        Output schema: SummarizedFindingsJSON
        """
        return Task(
            config=self.tasks_config["summarize_findings"],
            output_json=SummarizedFindingsJSON,
            context=[self.analyze_code_quality(), self.review_security()],
        )

    # ─────────────────────────────────────────────────────────────────────────
    # CREW ASSEMBLY
    # ─────────────────────────────────────────────────────────────────────────

    @crew
    def crew(self) -> Crew:
        """
        Assembles and configures the multi-agent code review crew.
        Process: sequential (analyze_code_quality and review_security run async
                 in parallel; summarize_findings runs after both complete).
        """
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
            memory=False,
        )
