"""
Dynamic Tool Registry for Code Review Crew Agents.
Allows declarative specification and resolution of tools from YAML configuration.
"""

import os
from pathlib import Path
from typing import Dict, List, Any, Type, Optional, Callable
from crewai.tools import BaseTool

from code_review_agent.config import get_serper_api_key, logger
from code_review_agent.tools import (
    QuickPatternScannerTool,
    SastScannerTool,
    RuffTool,
    UnifiedSecurityScannerTool,
)
from code_review_agent.context_engine import CodebaseContextTool
from code_review_agent.governance import CustomRulesTool
from code_review_agent.tools.test_generator import TestGeneratorTool


class ToolRegistry:
    """
    Central registry for agent tools.
    Resolves string tool identifiers in YAML configurations to instantiated BaseTool objects.
    Supports context-aware parameters such as repo_root for target repository indexing.
    """

    _registry: Dict[str, Callable[..., BaseTool]] = {}

    @classmethod
    def register(cls, name: str, factory: Callable[..., BaseTool]):
        """Register a tool factory function under a unique name."""
        cls._registry[name.lower()] = factory
        cls._registry[name] = factory

    @classmethod
    def reset(cls):
        """Reset registry and re-register standard tools."""
        cls._registry.clear()
        cls.initialize_default_tools()

    @classmethod
    def initialize_default_tools(cls):
        """Register all standard built-in tools for the code review system."""
        if cls._registry:
            return

        cls.register(
            "CodebaseContextTool",
            lambda repo_root=None, **kwargs: CodebaseContextTool(repo_root=repo_root)
        )
        cls.register(
            "codebase_context",
            lambda repo_root=None, **kwargs: CodebaseContextTool(repo_root=repo_root)
        )

        cls.register("RuffTool", lambda **kwargs: RuffTool())
        cls.register("ruff", lambda **kwargs: RuffTool())

        cls.register("QuickPatternScannerTool", lambda **kwargs: QuickPatternScannerTool())
        cls.register("quick_pattern_scanner", lambda **kwargs: QuickPatternScannerTool())

        cls.register("SastScannerTool", lambda **kwargs: SastScannerTool())
        cls.register("sast_scanner", lambda **kwargs: SastScannerTool())

        cls.register("UnifiedSecurityScannerTool", lambda **kwargs: UnifiedSecurityScannerTool())
        cls.register("unified_security_scanner", lambda **kwargs: UnifiedSecurityScannerTool())

        def _get_custom_rules(repo_root=None, rules_path=None, **kwargs):
            final_rules_path = rules_path or (str(Path(repo_root) / ".code-review.yaml") if repo_root else None)
            return CustomRulesTool(rules_path=final_rules_path)

        cls.register("CustomRulesTool", _get_custom_rules)
        cls.register("custom_rules", _get_custom_rules)

        cls.register("TestGeneratorTool", lambda **kwargs: TestGeneratorTool())
        cls.register("test_generator", lambda **kwargs: TestGeneratorTool())

        # Optional Serper Search tools
        def _get_serper(**kwargs):
            from crewai_tools import SerperDevTool
            serper_key = get_serper_api_key()
            if serper_key:
                os.environ["SERPER_API_KEY"] = serper_key
            return SerperDevTool()

        def _get_scraper(**kwargs):
            from crewai_tools import ScrapeWebsiteTool
            return ScrapeWebsiteTool()

        cls.register("SerperDevTool", _get_serper)
        cls.register("ScrapeWebsiteTool", _get_scraper)

    @classmethod
    def get_tool(cls, name: str, repo_root: Optional[str] = None, **kwargs) -> Optional[BaseTool]:
        """Instantiate a registered tool by its name, forwarding repo_root context."""
        cls.initialize_default_tools()
        factory = cls._registry.get(name) or cls._registry.get(name.lower())
        if factory:
            try:
                try:
                    return factory(repo_root=repo_root, **kwargs)
                except TypeError:
                    return factory(**kwargs)
            except Exception as e:
                logger.warning(f"Failed to instantiate tool '{name}': {e}")
                return None
        logger.warning(f"Unknown tool name '{name}' requested in agent config.")
        return None

    @classmethod
    def resolve_tools(cls, tool_names: List[str], repo_root: Optional[str] = None) -> List[BaseTool]:
        """Resolve a list of tool names into instantiated BaseTool instances with repo context."""
        cls.initialize_default_tools()
        tools: List[BaseTool] = []
        for name in tool_names:
            tool = cls.get_tool(name, repo_root=repo_root)
            if tool is not None:
                tools.append(tool)
        return tools


# Pre-initialize built-in registry
ToolRegistry.initialize_default_tools()
