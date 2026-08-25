"""
Robust LLM Output Parser and Resilient Schema Validation Engine.
Extracts structured JSON from markdown fences, repairs minor JSON syntax anomalies,
validates Pydantic schemas, and provides structured graceful fallbacks when LLM output is malformed.
"""

import json
import re
from typing import Type, TypeVar, Optional, Tuple, Any, Dict, List, Callable
from pydantic import BaseModel, ValidationError

from code_review_agent.models import (
    SummarizedFindingsJSON,
    CodeQualityJSON,
    ReviewSecurityJSON,
    InlineComment,
    Fix
)
from code_review_agent.config import logger

T = TypeVar("T", bound=BaseModel)


class RobustLLMOutputParser:
    """
    Resilient parser for LLM-generated structured outputs.
    Guarantees the pipeline never crashes or silently drops reviews due to unparseable JSON.
    """

    @staticmethod
    def extract_json_block(text: str) -> Optional[str]:
        """Extract JSON substring from markdown code blocks or conversational prose."""
        if not text or not isinstance(text, str):
            return None

        clean_text = text.strip()

        # 1. Match ```json ... ``` or ``` ... ```
        fence_match = re.search(r"```(?:json)?\s*\n?([\s\S]*?)\n?```", clean_text, re.IGNORECASE)
        if fence_match:
            candidate = fence_match.group(1).strip()
            if candidate.startswith("{") or candidate.startswith("["):
                return candidate

        # 2. Match outermost { ... }
        start_brace = clean_text.find("{")
        end_brace = clean_text.rfind("}")
        if start_brace != -1 and end_brace > start_brace:
            return clean_text[start_brace:end_brace + 1]

        # 3. Match outermost [ ... ]
        start_bracket = clean_text.find("[")
        end_bracket = clean_text.rfind("]")
        if start_bracket != -1 and end_bracket > start_bracket:
            return clean_text[start_bracket:end_bracket + 1]

        return None

    @staticmethod
    def repair_json_string(json_str: str) -> str:
        """Heuristically repair trailing commas and minor syntax flaws in JSON."""
        repaired = json_str.strip()
        # Remove trailing commas before closing braces/brackets
        repaired = re.sub(r",\s*([\}\]])", r"\1", repaired)
        return repaired

    @classmethod
    def parse_to_dict(cls, raw_output: Any) -> Tuple[Optional[Dict[str, Any]], bool, Optional[str]]:
        """
        Attempt to parse raw output into a dictionary.
        Returns: (parsed_dict, is_success, error_or_warning_message)
        """
        if isinstance(raw_output, dict):
            return raw_output, True, None

        # Check if object has json_dict attribute (e.g. CrewAI TaskOutput)
        if hasattr(raw_output, "json_dict") and isinstance(raw_output.json_dict, dict) and raw_output.json_dict:
            return raw_output.json_dict, True, None

        raw_str = ""
        if hasattr(raw_output, "raw"):
            raw_str = str(raw_output.raw)
        elif isinstance(raw_output, str):
            raw_str = raw_output
        else:
            raw_str = str(raw_output)

        extracted = cls.extract_json_block(raw_str) or raw_str.strip()
        repaired = cls.repair_json_string(extracted)

        try:
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed, True, None
            elif isinstance(parsed, list):
                return {"items": parsed}, True, None
            return None, False, "Parsed JSON is not an object or list."
        except Exception as e:
            return None, False, f"JSON decode error: {str(e)}"

    @classmethod
    def parse_with_schema(
        cls,
        raw_output: Any,
        target_schema: Type[T],
        fallback_factory: Optional[Callable[[str], T]] = None
    ) -> Tuple[T, bool, Optional[str]]:
        """
        Parse and validate against target Pydantic schema with automatic fallback.
        Guarantees returning a valid instance of target_schema.
        """
        if isinstance(raw_output, target_schema):
            return raw_output, True, None

        parsed_dict, ok, parse_error = cls.parse_to_dict(raw_output)

        raw_text_str = str(getattr(raw_output, "raw", raw_output))

        if ok and parsed_dict is not None:
            try:
                instance = target_schema.model_validate(parsed_dict)
                return instance, True, None
            except ValidationError as ve:
                logger.warning(f"Schema validation error against {target_schema.__name__}: {ve}")
                parse_error = f"Schema validation error: {str(ve)}"

        # Fallback path
        logger.warning(f"⚠️ Activating graceful fallback for {target_schema.__name__}. Error: {parse_error}")

        if fallback_factory:
            fallback_instance = fallback_factory(raw_text_str)
        else:
            fallback_instance = cls._default_fallback(target_schema, raw_text_str)

        return fallback_instance, False, parse_error

    @classmethod
    def _default_fallback(cls, target_schema: Type[T], raw_text: str) -> T:
        """Create a safe, structured fallback instance for known schema types."""
        clean_text = raw_text.strip() or "No review response generated."

        if target_schema is SummarizedFindingsJSON:
            return SummarizedFindingsJSON(
                confidence=50,
                findings=f"⚠️ Notice: The LLM returned unstructured output. Raw review content:\n\n{clean_text}",
                fix=[],
                recommendations=["Review output required fallback parsing; manual review recommended."],
                inline_comments=[],
                suggested_unit_tests=""
            ) # type: ignore

        elif target_schema is CodeQualityJSON:
            return CodeQualityJSON(
                critical_issues=[],
                minor_issues=[],
                reasoning=f"Unstructured code quality output: {clean_text}",
                cross_file_risks=[],
                inline_suggestions=[]
            ) # type: ignore

        elif target_schema is ReviewSecurityJSON:
            return ReviewSecurityJSON(
                security_vulnerabilities=[],
                blocking=False,
                highest_risk="none",
                security_recommendations=["Output degraded to raw text; verify security manually."],
                inline_security_comments=[]
            ) # type: ignore

        # Generic fallback
        try:
            return target_schema.model_validate({"raw_response": clean_text})
        except Exception:
            # Construct empty/minimal model
            return target_schema.model_construct()
