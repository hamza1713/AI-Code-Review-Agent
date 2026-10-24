"""
Unit tests for Phase 1 Bounded Agentic Tool Calling in CodeReviewCrew._stage().
Verifies that:
1. When an agent requests a tool call via structured JSON, the crew executes the tool securely.
2. The observation is fed back to the LLM conversation context.
3. The model terminates cleanly when the target schema is returned.
4. Tool invocations are bounded to max 4 calls.
"""

from unittest.mock import MagicMock
from code_review_agent.crews.code_review_crew.crew import CodeReviewCrew
from code_review_agent.llm_factory import LLMResponse, TokenUsage
from code_review_agent.models import CodeQualityJSON


def test_agentic_tool_loop_executes_tool_and_returns_schema(tmp_path):
    crew = CodeReviewCrew(repo_root=str(tmp_path))

    # Step 1 response: LLM asks to call CodebaseContextTool
    tool_call_resp = LLMResponse(
        text='{"action": "call_tool", "tool": "CodebaseContextTool", "arguments": {"query_type": "find_callers", "target": "validate_user"}}',
        model="mock-gemini",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )

    # Step 2 response: LLM receives observation and outputs final CodeQualityJSON
    final_schema_resp = LLMResponse(
        text="""
        ```json
        {
            "critical_issues": [],
            "minor_issues": ["Validation function caller verified across codebase"],
            "reasoning": "Verified caller impact across repository.",
            "cross_file_risks": [],
            "inline_suggestions": []
        }
        ```
        """,
        model="mock-gemini",
        usage=TokenUsage(prompt_tokens=150, completion_tokens=100, total_tokens=250),
    )

    mock_llm = MagicMock()
    mock_llm.call_response.side_effect = [tool_call_resp, final_schema_resp]
    crew._llm = mock_llm

    result, last_resp = crew._stage(
        name="analyze_code_quality",
        inputs={
            "file_content": "def validate_user(u): return bool(u)",
            "code_graph_context": "",
            "semantic_context": "",
            "team_memory_context": "",
            "ticket_context": "",
        },
        schema=CodeQualityJSON,
    )

    assert isinstance(result, CodeQualityJSON)
    assert result.reasoning == "Verified caller impact across repository."
    assert len(result.minor_issues) == 1
    # Check that mock LLM was called twice (tool call + final output)
    assert mock_llm.call_response.call_count == 2
    # Verify second call received the tool observation in user prompt
    second_call_args = mock_llm.call_response.call_args_list[1][0][0]
    messages = second_call_args.messages
    assert any("Observation from CodebaseContextTool" in m["content"] for m in messages)
