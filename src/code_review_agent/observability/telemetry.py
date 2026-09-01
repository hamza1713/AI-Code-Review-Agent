"""
Observability, Cost Telemetry, and Production Evaluation Tracker.
Measures end-to-end review latency, token usage breakdown, accurate USD cost estimation,
and optionally streams evaluation traces and quality scores to Langfuse.
"""

import os
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional
from code_review_agent.models import TelemetryMetrics
from code_review_agent.config import logger, get_model_name


# Pricing Table (USD per 1M tokens)
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "gemini/gemini-3.1-flash-lite-preview": {"prompt": 0.0375, "completion": 0.15},
    "gemini/gemini-2.5-flash": {"prompt": 0.075, "completion": 0.30},
    "gemini/gemini-2.5-pro": {"prompt": 1.25, "completion": 5.00},
    "default": {"prompt": 0.0375, "completion": 0.15}
}


class TelemetryTracker:
    """Tracks performance, latency, cost, and production evaluation telemetry."""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or get_model_name()
        self.start_time: float = 0.0
        self.end_time: float = 0.0
        self._langfuse_client = None
        self._init_langfuse()

    def _init_langfuse(self):
        """Initialize Langfuse observability client if credentials are configured."""
        public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        secret_key = os.getenv("LANGFUSE_SECRET_KEY")
        host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

        if public_key and secret_key:
            try:
                from langfuse import Langfuse
                self._langfuse_client = Langfuse(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=host
                )
                logger.info("📡 Langfuse production observability client initialized.")
            except ImportError:
                logger.debug("Langfuse library not installed. Install via pip install langfuse to enable cloud tracing.")
            except Exception as e:
                logger.warning(f"Failed to initialize Langfuse client: {e}")

    def start(self):
        """Start execution timer."""
        self.start_time = time.time()

    def stop(
        self,
        token_usage: Optional[Dict[str, Any]] = None,
        sast_count: int = 0,
        rules_count: int = 0,
        inline_comments_count: int = 0,
        final_verdict: str = "",
        eval_score: Optional[float] = None
    ) -> TelemetryMetrics:
        """Stop execution timer, compute cost metrics, and emit production traces."""
        self.end_time = time.time()
        duration = round(self.end_time - self.start_time, 3)

        token_usage = token_usage or {}
        prompt_tokens = int(token_usage.get("prompt_tokens", 0) or token_usage.get("promptTokens", 0) or 0)
        completion_tokens = int(token_usage.get("completion_tokens", 0) or token_usage.get("completionTokens", 0) or 0)
        total_tokens = prompt_tokens + completion_tokens

        # Calculate estimated cost
        rates = MODEL_PRICING.get(self.model_name, MODEL_PRICING["default"])
        cost = (prompt_tokens / 1_000_000 * rates["prompt"]) + (completion_tokens / 1_000_000 * rates["completion"])

        metrics = TelemetryMetrics(
            duration_seconds=duration,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=round(cost, 6),
            model_used=self.model_name,
            sast_findings_count=sast_count,
            rule_violations_count=rules_count,
            inline_comments_count=inline_comments_count,
            final_verdict=final_verdict[:30] if final_verdict else "COMPLETED"
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
                    }
                )
                if eval_score is not None:
                    trace.score(name="deterministic_eval_score", value=eval_score)
            except Exception as e:
                logger.debug(f"Langfuse trace logging error: {e}")

        return metrics

    @staticmethod
    def save_metrics_to_file(metrics: TelemetryMetrics, output_file: str = "telemetry_metrics.json"):
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
            f"• Estimated Cost      : ${metrics.estimated_cost_usd:.6f} USD\n"
            f"• SAST Findings       : {metrics.sast_findings_count}\n"
            f"• Governance Flags    : {metrics.rule_violations_count}\n"
            f"• Inline Comments     : {metrics.inline_comments_count}\n"
            "-" * 50
        )
