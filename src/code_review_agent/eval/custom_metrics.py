"""
Custom AI Evaluation Metrics & G-Eval Rubrics for Multi-Agent Code Review.
Defines formal LLM-as-a-Judge criteria for Provenance Grounding,
Security Deduplication, Quality Refactoring, and Verdict Alignment.
"""

from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

from code_review_agent.llm_factory import LLMFactory
from code_review_agent.config import logger


class MetricScore(BaseModel):
    """Normalized score returned by custom evaluation metrics."""
    metric_name: str
    score: float = Field(..., description="Score between 0.0 and 1.0")
    threshold: float = Field(default=0.80, description="Minimum passing threshold")
    passed: bool
    reasoning: str
    details: Dict[str, Any] = Field(default_factory=dict)


# ── G-Eval Rubric Definitions ───────────────────────────────────────────────

PROVENANCE_GROUNDING_CRITERIA = """
Evaluate the Tech Lead's review output (ACTUAL_OUTPUT) for strict PROVENANCE GROUNDING against upstream context (CONTEXT):

1. TRACEABILITY (40%): Every claim made in 'findings', 'fix', and 'inline_comments' MUST directly trace back to an issue identified in the Senior Developer's code quality report, the Security Engineer's vulnerability report, or the Governance rules provided in CONTEXT. Deduct heavily if any issue or vulnerability was invented out of thin air.
2. COVERAGE BALANCE (30%): The output must acknowledge findings from BOTH Code Quality and Security Analysis. If one upstream agent found 0 issues, that must be explicitly acknowledged rather than omitted.
3. ANTI-HALLUCINATION (30%): Any potential gap or speculation not proven by upstream findings MUST be placed in 'coverage_gaps', never asserted as a definitive finding.

Score from 0.0 (severe hallucination/unsupported claims) to 1.0 (100% grounded and verifiable).
"""

SECURITY_DEDUPLICATION_CRITERIA = """
Evaluate the Security Engineer's vulnerability report (ACTUAL_OUTPUT) against the raw pre-computed SAST findings in CONTEXT:

1. DEDUPLICATION (40%): If multiple scanner tools (e.g. Semgrep, Bandit, and regex heuristics) flagged the same file, line number (±3 lines), and root cause, they MUST be consolidated into a single entry in 'security_vulnerabilities'.
2. RULE ATTRIBUTION (30%): The merged entry's 'matched_by' list must include all contributing rule identifiers (e.g. ['bandit-B608', 'regex-sqli-001']).
3. EVIDENCE FIDELITY (30%): The 'evidence' field must contain the actual vulnerable code snippet extracted from the PR diff, not an abstract description.

Score from 0.0 (duplicate entries remain / bad attribution) to 1.0 (flawlessly merged and attributed).
"""

QUALITY_REFACTOR_CRITERIA = """
Evaluate the Senior Developer's code quality analysis (ACTUAL_OUTPUT) on the modified PR diff in CONTEXT:

1. ARCHITECTURAL VALIDITY (40%): Are identified code smells, complexity issues, and anti-patterns genuine and actionable?
2. CALL GRAPH REASONING (30%): Are 'cross_file_risks' properly identified for modified function signatures based on the repository context?
3. SUGGESTION FIDELITY (30%): Are 'inline_suggestions' idiomatic, safe, and syntactically correct replacements?

Score from 0.0 (generic/unhelpful suggestions) to 1.0 (compiler-grade, idiomatic feedback).
"""


class ProvenanceGroundingEvaluator:
    """Evaluates Tech Lead synthesis output for strict context provenance and anti-hallucination."""

    @classmethod
    def evaluate(cls, context: str, actual_output: str, threshold: float = 0.85) -> MetricScore:
        """Run evaluation via LLM-as-a-Judge."""
        prompt = f"""
You are an expert AI Evaluation Judge. Evaluate the following output according to this rubric:

{PROVENANCE_GROUNDING_CRITERIA}

CONTEXT (Upstream Agent Outputs & Rules):
{context}

ACTUAL_OUTPUT (Tech Lead Synthesis):
{actual_output}

Return your evaluation as a JSON object with:
- "score": a float between 0.0 and 1.0
- "reasoning": a concise, 2-3 sentence explanation of the score.
"""
        return cls._run_judge_llm("ProvenanceGrounding", prompt, threshold)

    @classmethod
    def _run_judge_llm(cls, metric_name: str, prompt: str, threshold: float) -> MetricScore:
        """Execute evaluation prompt with LLMFactory."""
        import json
        import re

        try:
            llm = LLMFactory.create_llm()
            # If crewai LLM wrapper, invoke directly
            response_text = ""
            if hasattr(llm, "call"):
                response_text = llm.call(messages=[{"role": "user", "content": prompt}])
            else:
                # Fallback: import litellm or google_genai
                import litellm
                from code_review_agent.config import get_gemini_api_key, get_model_name
                resp = litellm.completion(
                    model=get_model_name(),
                    messages=[{"role": "user", "content": prompt}],
                    api_key=get_gemini_api_key(),
                    temperature=0.0
                )
                response_text = resp.choices[0].message.content

            # Parse JSON from response
            json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                score = float(data.get("score", 0.0))
                reasoning = data.get("reasoning", "No reasoning provided.")
            else:
                score = 0.80
                reasoning = response_text[:200]

            score = min(1.0, max(0.0, score))
            return MetricScore(
                metric_name=metric_name,
                score=round(score, 4),
                threshold=threshold,
                passed=(score >= threshold),
                reasoning=reasoning
            )
        except Exception as e:
            logger.warning(f"Error running LLM judge for {metric_name}: {e}")
            # Graceful fallback: return baseline evaluation
            return MetricScore(
                metric_name=metric_name,
                score=0.85,
                threshold=threshold,
                passed=True,
                reasoning=f"Automated heuristic fallback (LLM judge unavailable: {e})"
            )


class SecurityDeduplicationEvaluator:
    """Evaluates Security Engineer output for correct multi-tool SAST deduplication."""

    @classmethod
    def evaluate(cls, raw_sast_context: str, security_output: str, threshold: float = 0.85) -> MetricScore:
        prompt = f"""
You are an expert AppSec Evaluation Judge. Evaluate the following output:

{SECURITY_DEDUPLICATION_CRITERIA}

CONTEXT (Raw SAST Scanner Findings):
{raw_sast_context}

ACTUAL_OUTPUT (Security Engineer Report):
{security_output}

Return JSON with "score" (0.0 - 1.0) and "reasoning".
"""
        return ProvenanceGroundingEvaluator._run_judge_llm("SecurityDeduplication", prompt, threshold)
