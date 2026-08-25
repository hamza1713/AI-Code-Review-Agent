"""
Multi-Agent Code Review Crew definition.
Coordinates Senior Developer, Security Engineer, and Tech Lead agents
with AST Code Graph context, Quick Security Pattern Scanner, and custom rules governance.
"""

import os
from typing import List
from crewai import Agent, Crew, Process, Task, LLM
from crewai.project import CrewBase, agent, crew, task
from crewai_tools import SerperDevTool, ScrapeWebsiteTool

from code_review_agent.config import get_gemini_api_key, get_serper_api_key, get_model_name, get_max_tokens, logger
from code_review_agent.models import (
    CodeQualityJSON,
    ReviewSecurityJSON,
    SummarizedFindingsJSON,
)
from code_review_agent.tools import QuickPatternScannerTool, SastScannerTool, RuffTool, UnifiedSecurityScannerTool
from code_review_agent.context_engine import CodebaseContextTool
from code_review_agent.governance import CustomRulesTool
from code_review_agent.tools.test_generator import TestGeneratorTool
from code_review_agent.crews.code_review_crew.guardrails import security_review_output_guardrail


@CrewBase
class CodeReviewCrew:
    """Production Multi-Agent Code Review Crew with AST Context and Governance Tooling."""

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    def _get_llm(self) -> LLM:
        """Initialize configured Gemini LLM for crew agents with max_tokens=4096."""
        api_key = get_gemini_api_key()
        model = get_model_name()
        max_tokens = get_max_tokens()
        return LLM(model=model, api_key=api_key, max_tokens=max_tokens)


    @agent
    def senior_developer(self) -> Agent:
        """Senior Developer agent equipped with AST Code Graph caller context and Ruff linter."""
        return Agent(
            config=self.agents_config["senior_developer"],
            tools=[CodebaseContextTool(), RuffTool()],
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def security_engineer(self) -> Agent:
        """Security Engineer agent equipped with Quick Security Pattern Scanner and OWASP search."""
        tools = [QuickPatternScannerTool()]
        serper_key = get_serper_api_key()
        if serper_key:
            os.environ["SERPER_API_KEY"] = serper_key
            try:
                tools.extend([SerperDevTool(), ScrapeWebsiteTool()])
            except Exception as e:
                logger.warning(f"Could not initialize web search tools: {e}")

        return Agent(
            config=self.agents_config["security_engineer"],
            tools=tools,
            llm=self._get_llm(),
            verbose=True,
        )

    @agent
    def tech_lead(self) -> Agent:
        """Tech Lead agent equipped with Governance Rules Validator and Test Generator."""
        return Agent(
            config=self.agents_config["tech_lead"],
            tools=[CustomRulesTool(), TestGeneratorTool()],
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
