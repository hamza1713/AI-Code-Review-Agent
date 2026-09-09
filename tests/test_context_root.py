"""
Unit tests verifying that CodebaseContextTool, CustomRulesTool, and ToolRegistry
properly accept and respect target repository root context (F2).
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from code_review_agent.context_engine.context_tool import CodebaseContextTool
from code_review_agent.crews.code_review_crew.tool_registry import ToolRegistry
from code_review_agent.crews.code_review_crew.crew import CodeReviewCrew
from code_review_agent.governance.rules_engine import CustomRulesTool


def test_codebase_context_tool_indexes_target_root():
    """Verify CodebaseContextTool indexes the target directory, not Path('.')."""
    with tempfile.TemporaryDirectory() as tmpdir:
        target_py = Path(tmpdir) / 'service.py'
        target_py.write_text(
            "def unique_target_function(x, y):\n    \"\"\"Sample target function.\"\"\"\n    return x * y\n",
            encoding='utf-8'
        )

        tool = CodebaseContextTool(repo_root=tmpdir)
        res = tool._run(query_type='lookup_symbol', target='unique_target_function')
        assert 'unique_target_function' in res
        assert 'service.py' in res
        assert 'CodeReviewCrew' not in tool._get_indexer().symbols_by_name


def test_tool_registry_resolves_with_repo_root():
    """Verify ToolRegistry forwards repo_root to CodebaseContextTool and CustomRulesTool."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_yaml = Path(tmpdir) / '.code-review.yaml'
        custom_yaml.write_text(
            "version: '1.0'\nrules:\n  - id: 'target-rule'\n    name: 'Target Rule'\n    severity: 'BLOCKING'\n    pattern: 'TARGET_FLAG'\n    description: 'Test rule'\n    suggested_fix: 'Fix it'\n",
            encoding='utf-8'
        )

        tools = ToolRegistry.resolve_tools(['CodebaseContextTool', 'CustomRulesTool'], repo_root=tmpdir)
        assert len(tools) == 2

        context_tool = next(t for t in tools if isinstance(t, CodebaseContextTool))
        assert context_tool._repo_root == tmpdir

        rules_tool = next(t for t in tools if isinstance(t, CustomRulesTool))
        assert Path(rules_tool._engine.rules_file) == Path(custom_yaml)


def test_crew_passes_repo_root_to_agent_tools():
    """Verify CodeReviewCrew constructor stores repo_root and passes it to tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("code_review_agent.crews.code_review_crew.crew.LLMFactory.create_llm", return_value="gpt-4o"):
            crew = CodeReviewCrew(repo_root=tmpdir)
            assert crew.repo_root == tmpdir

            dev_tools = crew._get_agent_tools('senior_developer')
            context_tools = [t for t in dev_tools if isinstance(t, CodebaseContextTool)]
            for ct in context_tools:
                assert ct._repo_root == tmpdir
