"""
Unit tests for dynamic ToolRegistry and declarative YAML tool resolution.
"""

import pytest
from code_review_agent.crews.code_review_crew.tool_registry import ToolRegistry
from code_review_agent.crews.code_review_crew.crew import CodeReviewCrew


class TestToolRegistry:
    """Validate dynamic tool resolution from names and YAML config."""

    def test_resolve_known_tools(self):
        tools = ToolRegistry.resolve_tools(["RuffTool", "CodebaseContextTool", "QuickPatternScannerTool"])
        assert len(tools) == 3
        tool_names = [t.name for t in tools]
        assert any("Ruff" in name or "ruff" in name for name in tool_names)

    def test_case_insensitive_and_alias_resolution(self):
        tools = ToolRegistry.resolve_tools(["ruff", "quick_pattern_scanner", "custom_rules"])
        assert len(tools) == 3

    def test_graceful_handling_of_unknown_tool(self):
        tools = ToolRegistry.resolve_tools(["NonExistentToolXYZ", "RuffTool"])
        assert len(tools) == 1
        assert "ruff" in tools[0].name.lower()

    @pytest.mark.slow
    def test_crew_loads_tools_from_yaml(self):
        crew = CodeReviewCrew()
        senior_dev = crew.senior_developer()
        assert len(senior_dev.tools) >= 2

        sec_eng = crew.security_engineer()
        assert len(sec_eng.tools) >= 1

        tech_lead = crew.tech_lead()
        assert len(tech_lead.tools) >= 1
