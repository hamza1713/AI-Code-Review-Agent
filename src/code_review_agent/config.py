"""
Configuration and Environment Management for AI Code Review Agent.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv, find_dotenv

# ── 0. Silence noisy third-party loggers before anything else ─────────────────
# Disable the OpenTelemetry OTLP exporter (no collector running locally)
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
# Suppress the CrewAI update-check banner and telemetry
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")

# Load environment variables from .env file
# override=True ensures .env values take precedence over any empty/missing env vars
_project_root = Path(__file__).resolve().parent.parent.parent
_env_file = _project_root / ".env"

if _env_file.exists():
    load_dotenv(str(_env_file), override=True)
else:
    dotenv_path = find_dotenv(usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=True)

# ── 1. API Key normalisation ───────────────────────────────────────────────────
# LiteLLM (used by CrewAI) reads GEMINI_API_KEY or GOOGLE_API_KEY.
# We prefer GEMINI_API_KEY from .env; expose it under GOOGLE_API_KEY so
# LiteLLM can find it without duplication.  Never overwrite an explicitly
# set GOOGLE_API_KEY so we don't shadow a GCP service-account credential.
_gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
_google_key  = os.getenv("GOOGLE_API_KEY", "").strip()

if _gemini_key and not _google_key:
    # Only GEMINI_API_KEY supplied → mirror it for LiteLLM
    os.environ["GOOGLE_API_KEY"] = _gemini_key
elif _google_key and not _gemini_key:
    # Only GOOGLE_API_KEY supplied → mirror it for completeness
    os.environ["GEMINI_API_KEY"] = _google_key
# If both are set and equal, nothing to do.
# If both are set and different, keep them as-is (user knows what they're doing).

# ── 2. Model / token defaults ─────────────────────────────────────────────────
LLM_MODEL: str = os.getenv("LLM_MODEL", "gemini/gemini-3.1-flash-lite-preview").strip()
DEFAULT_MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))


def setup_logging(level=logging.INFO):
    """Configure structured logging for the application."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("opentelemetry").setLevel(logging.ERROR)
    logging.getLogger("opentelemetry.exporter").setLevel(logging.CRITICAL)
    logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)
    logging.getLogger("crewai").setLevel(logging.WARNING)
    return logging.getLogger("code_review_agent")


logger = setup_logging()


def get_gemini_api_key() -> str:
    """Retrieve Google Gemini API Key from environment."""
    key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not key:
        logger.warning("No Gemini API key found. Set GEMINI_API_KEY in your .env file.")
    return key


def get_github_token() -> str:
    """Retrieve GitHub Personal Access Token or App Installation Token."""
    return os.getenv("GITHUB_TOKEN", "")


def get_webhook_secret() -> str:
    """Retrieve GitHub Webhook HMAC Secret."""
    return os.getenv("GITHUB_WEBHOOK_SECRET", "")


def get_serper_api_key() -> str:
    """Retrieve Serper API Key for online security intelligence search."""
    return os.getenv("SERPER_API_KEY", "")


def get_model_name() -> str:
    """Retrieve configured LLM model identifier."""
    return os.getenv("LLM_MODEL", "gemini/gemini-3.1-flash-lite-preview").strip()


def get_max_tokens() -> int:
    """Retrieve max_tokens setting for LLM calls (default: 4096)."""
    return int(os.getenv("MAX_TOKENS", "4096"))
