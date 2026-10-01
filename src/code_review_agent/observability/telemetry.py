"""
Observability, Cost Telemetry, and Production Evaluation Tracker.
Measures end-to-end review latency, token usage breakdown, accurate USD cost estimation,
and optionally streams evaluation traces and quality scores to Langfuse.
"""

import json
import math
import os
import time
from pathlib import Path
from typing import Any

from code_review_agent.config import get_model_name, logger
from code_review_agent.models import TelemetryMetrics


# Pricing Table (USD per 1M tokens)
def configured_price(model: str):
    """Operator-supplied USD/1M rates with attribution; unknown prices stay unknown."""
    try:
        entry = json.loads(os.getenv("REVIEW_MODEL_PRICING_JSON", "{}"))[model]
        rates = [float(entry[key]) for key in ("prompt", "completion")]
        if not entry.get("as_of") or any(not math.isfinite(v) or v < 0 for v in rates):
            return None
        return entry
    except (KeyError, TypeError, ValueError):
        return None


class TelemetryTracker:
    """Tracks performance, latency, cost, and production evaluation telemetry."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or get_model_name()
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self._langfuse_client = None
        self._init_langfuse()

    def _init_langfuse(self):
        """Initialize Langfuse observability client if credentials are configured."""
        from code_review_agent.data_policy import production

        if production():
            return  # Production traces remain on the protected host.
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if public_key and secret_key:
            try:
                from langfuse import Langfuse  # type: ignore[import-not-found]

                self._langfuse_client = Langfuse(
                    public_key=public_key, secret_key=secret_key, host=host
                )
                logger.info("📡 Langfuse production observability client initialized.")
            except ImportError:
                logger.debug(
                    "Langfuse library not installed. Install via pip install langfuse to enable cloud tracing."
                )
            except Exception as e:
                logger.warning(f"Failed to initialize Langfuse client: {e}")

    def start(self):
        """Start execution timer."""
        self.start_time = time.monotonic()

    def stop(
        self,
        token_usage: dict[str, Any] | None = None,
        sast_count: int = 0,
        rules_count: int = 0,
        inline_comments_count: int = 0,
        final_verdict: str = "",
        eval_score: float | None = None,
    ) -> TelemetryMetrics:
        """Stop execution timer, compute cost metrics, and emit production traces."""
        self.end_time = time.monotonic()
        duration = round(self.end_time - self.start_time, 3)

        token_usage = token_usage or {}
        prompt_tokens = int(
            token_usage.get("prompt_tokens", 0)
            or token_usage.get("promptTokens", 0)
            or 0
        )
        completion_tokens = int(
            token_usage.get("completion_tokens", 0)
            or token_usage.get("completionTokens", 0)
            or 0
        )
        total_tokens = prompt_tokens + completion_tokens

        # Calculate estimated cost
        rates = configured_price(self.model_name)
        source = token_usage.get("source", "UNAVAILABLE")
        cost = None
        if rates and source != "UNAVAILABLE":
            cost = (
                prompt_tokens * float(rates["prompt"])
                + completion_tokens * float(rates["completion"])
            ) / 1_000_000

        metrics = TelemetryMetrics(
            duration_seconds=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=round(cost, 6) if cost is not None else None,
            token_usage_source=source,
            cost_source="ESTIMATED" if cost is not None else "UNAVAILABLE",
            pricing_as_of=rates["as_of"] if rates else None,
            model_used=self.model_name,
            sast_findings_count=sast_count,
            rule_violations_count=rules_count,
            inline_comments_count=inline_comments_count,
            final_verdict=final_verdict[:30] if final_verdict else "COMPLETED",
        )

        # Log trace and quality score to Langfuse if available
        if self._langfuse_client:
            try:
                trace = self._langfuse_client.trace(
                    name="ai_code_review_execution",
                    metadata={
                        "model": self.model_name,
                        "duration_seconds": duration,
                        "total_tokens": total_tokens,
                        "estimated_cost_usd": cost,
                        "final_verdict": metrics.final_verdict,
                    },
                )
                if eval_score is not None:
                    trace.score(name="deterministic_eval_score", value=eval_score)
            except Exception as e:
                logger.debug(f"Langfuse trace logging error: {e}")

        return metrics

    @staticmethod
    def save_metrics_to_file(
        metrics: TelemetryMetrics, output_file: str = "telemetry_metrics.json"
    ):
        """Save metrics JSON file."""
        target = Path(output_file)
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(metrics.model_dump(), f, indent=2)
            logger.info(f"📊 Telemetry metrics saved to {target.resolve()}")
        except Exception as e:
            logger.warning(f"Could not save telemetry metrics: {e}")

    @staticmethod
    def format_summary_table(metrics: TelemetryMetrics) -> str:
        """Format metrics into a clean text block."""
        return (
            "\n" + "-" * 50 + "\n"
            "📊 EXECUTION & COST TELEMETRY\n"
            "-" * 50 + "\n"
            f"• Execution Latency   : {metrics.duration_seconds}s\n"
            f"• LLM Model Used      : {metrics.model_used}\n"
            f"• Prompt Tokens       : {metrics.prompt_tokens:,}\n"
            f"• Completion Tokens   : {metrics.completion_tokens:,}\n"
            f"• Total Tokens        : {metrics.total_tokens:,}\n"
            f"• Usage Source        : {metrics.token_usage_source}\n"
            f"• Estimated Cost      : {('$' + format(metrics.estimated_cost_usd, '.6f') + ' USD') if metrics.estimated_cost_usd is not None else 'Unavailable'}\n"
            f"• SAST Findings       : {metrics.sast_findings_count}\n"
            f"• Governance Flags    : {metrics.rule_violations_count}\n"
            f"• Inline Comments     : {metrics.inline_comments_count}\n"
            "-" * 50
        )
