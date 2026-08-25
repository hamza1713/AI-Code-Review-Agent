"""
Unit tests for RobustLLMOutputParser.
Verifies markdown JSON extraction, trailing comma repairs, schema validation,
and graceful degradation on truncated/malformed LLM responses without crashing.
"""

import pytest
from code_review_agent.flow_parser import RobustLLMOutputParser
from code_review_agent.models import SummarizedFindingsJSON, CodeQualityJSON, ReviewSecurityJSON


class TestLLMOutputResilience:
    """Test suite for robust LLM parsing and graceful fallback."""

    def test_extract_json_from_markdown_fences(self):
        """Confirm JSON inside ```json ... ``` blocks is correctly extracted."""
        llm_response = """
Here is my review analysis:

```json
{
  "confidence": 92,
  "findings": "Code changes look solid.",
  "fix": [],
  "recommendations": ["Add more integration tests."],
  "inline_comments": []
}
```

Hope this helps!
"""
        model, ok, err = RobustLLMOutputParser.parse_with_schema(llm_response, SummarizedFindingsJSON)
        assert ok is True
        assert model.confidence == 92
        assert model.findings == "Code changes look solid."
        assert model.recommendations == ["Add more integration tests."]

    def test_repair_trailing_commas_in_json(self):
        """Confirm JSON with trailing commas is repaired and parsed cleanly."""
        raw_json = """
{
  "confidence": 80,
  "findings": "Minor issue found",
  "fix": [],
  "recommendations": [
    "Refactor auth logic",
  ],
  "inline_comments": [],
}
"""
        model, ok, err = RobustLLMOutputParser.parse_with_schema(raw_json, SummarizedFindingsJSON)
        assert ok is True
        assert model.confidence == 80
        assert "Refactor auth logic" in model.recommendations

    def test_graceful_degradation_on_malformed_truncated_json(self):
        """
        Confirm malformed or truncated JSON does NOT throw an exception and degrades
        gracefully to a valid SummarizedFindingsJSON model preserving raw text.
        """
        truncated_response = """
{
  "confidence": 40,
  "findings": "The authentication query is severely truncated and syntax error...
"""
        model, ok, err = RobustLLMOutputParser.parse_with_schema(truncated_response, SummarizedFindingsJSON)
        # Should gracefully return fallback model without throwing
        assert ok is False
        assert isinstance(model, SummarizedFindingsJSON)
        assert model.confidence == 50
        assert "unstructured output" in model.findings.lower() or "truncated" in model.findings.lower()
        assert len(model.recommendations) > 0

    def test_graceful_degradation_on_plain_conversational_text(self):
        """Confirm pure conversational prose without JSON structure degrades gracefully."""
        plain_text = "I reviewed the code and everything looks okay, but please remove the print statement on line 12."
        model, ok, err = RobustLLMOutputParser.parse_with_schema(plain_text, CodeQualityJSON)
        assert ok is False
        assert isinstance(model, CodeQualityJSON)
        assert "print statement" in model.reasoning
