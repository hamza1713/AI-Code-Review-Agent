"""
Dynamic Tool Registry for Code Review Crew Agents.
Allows declarative specification and resolution of tools from YAML configuration.
"""

import os
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
    """

    _registry: Dict[str, Callable[[], BaseTool]] = {}

    @classmethod
    def register(cls, name: str, factory: Callable[[], BaseTool]):
        """Register a tool factory function under a unique name."""
        cls._registry[name.lower()] = factory
        cls._registry[name] = factory

    @classmethod
    def initialize_default_tools(cls):
        """Register all standard built-in tools for the code review system."""
        if cls._registry:
            return

        cls.register("CodebaseContextTool", lambda: CodebaseContextTool())
        cls.register("codebase_context", lambda: CodebaseContextTool())

        cls.register("RuffTool", lambda: RuffTool())
        cls.register("ruff", lambda: RuffTool())

        cls.register("QuickPatternScannerTool", lambda: QuickPatternScannerTool())
        cls.register("quick_pattern_scanner", lambda: QuickPatternScannerTool())

        cls.register("SastScannerTool", lambda: SastScannerTool())
        cls.register("sast_scanner", lambda: SastScannerTool())

        cls.register("UnifiedSecurityScannerTool", lambda: UnifiedSecurityScannerTool())
        cls.register("unified_security_scanner", lambda: UnifiedSecurityScannerTool())

        cls.register("CustomRulesTool", lambda: CustomRulesTool())
        cls.register("custom_rules", lambda: CustomRulesTool())

        cls.register("TestGeneratorTool", lambda: TestGeneratorTool())
        cls.register("test_generator", lambda: TestGeneratorTool())

        # Optional Serper Search tools
        def _get_serper():
            from crewai_tools import SerperDevTool
            serper_key = get_serper_api_key()
            if serper_key:
                os.environ["SERPER_API_KEY"] = serper_key
            return SerperDevTool()

        def _get_scraper():
            from crewai_tools import ScrapeWebsiteTool
            return ScrapeWebsiteTool()

        cls.register("SerperDevTool", _get_serper)
        cls.register("ScrapeWebsiteTool", _get_scraper)

    @classmethod
    def get_tool(cls, name: str) -> Optional[BaseTool]:
        """Instantiate a registered tool by its name."""
        cls.initialize_default_tools()
        factory = cls._registry.get(name) or cls._registry.get(name.lower())
        if factory:
            try:
                return factory()
            except Exception as e:
                logger.warning(f"Failed to instantiate tool '{name}': {e}")
                return None
        logger.warning(f"Unknown tool name '{name}' requested in agent config.")
        return None

    @classmethod
    def resolve_tools(cls, tool_names: List[str]) -> List[BaseTool]:
        """Resolve a list of tool names into instantiated BaseTool instances."""
        cls.initialize_default_tools()
        tools: List[BaseTool] = []
        for name in tool_names:
            tool = cls.get_tool(name)
            if tool is not None:
                tools.append(tool)
        return tools


# Pre-initialize built-in registry
ToolRegistry.initialize_default_tools()
