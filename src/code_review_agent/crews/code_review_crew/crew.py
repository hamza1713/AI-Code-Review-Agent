"""
Multi-Agent Code Review Crew definition.
Coordinates Senior Developer, Security Engineer, and Tech Lead agents
with AST Code Graph context, Quick Security Pattern Scanner, and custom rules governance.
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

    def _get_llm(self) -> LLM:
        """Initialize configured LLM for crew agents using LLMFactory."""
        return LLMFactory.create_llm()

    def _get_agent_tools(self, agent_name: str, fallback_tools: Optional[List[Any]] = None) -> List[Any]:
        """Resolve tools dynamically from agents.yaml configuration via ToolRegistry."""
        agent_cfg = self.agents_config.get(agent_name, {})
        configured_tools = agent_cfg.get("assigned_tools", []) or agent_cfg.get("tools", [])
        if configured_tools:
            resolved = ToolRegistry.resolve_tools(configured_tools)
            if resolved:
                return resolved
        return fallback_tools or []


    @agent
    def senior_developer(self) -> Agent:
        """Senior Developer agent equipped with AST Code Graph caller context and Ruff linter."""
        cfg = dict(self.agents_config["senior_developer"])
        tools = self._get_agent_tools("senior_developer")
        cfg.pop("tools", None)
        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def security_engineer(self) -> Agent:
        """Security Engineer agent equipped with Quick Security Pattern Scanner and OWASP search."""
        cfg = dict(self.agents_config["security_engineer"])
        tools = self._get_agent_tools("security_engineer")
        cfg.pop("tools", None)
        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def tech_lead(self) -> Agent:
        """Tech Lead agent equipped with Governance Rules Validator and Test Generator."""
        cfg = dict(self.agents_config["tech_lead"])
        tools = self._get_agent_tools("tech_lead")
        cfg.pop("tools", None)
        return Agent(
            config=cfg,
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )



    @task
    def analyze_code_quality(self) -> Task:
        """Task for asynchronous code quality inspection with cross-file context."""
        return Task(
            config=self.tasks_config["analyze_code_quality"],
            output_json=CodeQualityJSON,
            async_execution=True,
        )

    @task
    def review_security(self) -> Task:
        """Task for asynchronous security analysis with deterministic guardrails."""
        return Task(
            config=self.tasks_config["review_security"],
            output_json=ReviewSecurityJSON,
            async_execution=True,
            guardrails=[security_review_output_guardrail],
        )

    @task
    def summarize_findings(self) -> Task:
        """Task for synthesizing quality, security, rule checks, and generating inline comments & unit tests."""
        return Task(
            config=self.tasks_config["summarize_findings"],
            output_json=SummarizedFindingsJSON,
            context=[self.analyze_code_quality(), self.review_security()],
        )

    @crew
    def crew(self) -> Crew:
        """Assembles and configures the multi-agent code review crew."""
        return Crew(
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
            memory=False,
        )
