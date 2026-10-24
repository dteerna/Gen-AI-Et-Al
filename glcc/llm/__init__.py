"""Optional LLM layer. The system is fully functional without it."""

from __future__ import annotations

import os

from .base import LLMClient, LLMError, LLMUnavailable, ResilientLLM, SchemaError, validate

__all__ = ["LLMClient", "LLMError", "LLMUnavailable", "ResilientLLM", "SchemaError", "validate", "llm_from_env"]


def llm_from_env() -> ResilientLLM | None:
    """Build the configured LLM, or None for deterministic offline mode.

    GLCC_LLM_PROVIDER = none (default) | azure_openai
    azure_openai needs AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT; optional AZURE_OPENAI_API_VERSION, AZURE_OPENAI_API_KEY.
    """
    provider = os.getenv("GLCC_LLM_PROVIDER", "none").strip().lower()
    if provider in ("", "none", "off"):
        return None
    if provider == "azure_openai":
        from .azure_openai import AzureOpenAIClient
        missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT") if not os.getenv(k)]
        if missing:
            raise LLMError(f"GLCC_LLM_PROVIDER=azure_openai requires {', '.join(missing)}")
        return ResilientLLM(AzureOpenAIClient(
            os.environ["AZURE_OPENAI_ENDPOINT"], os.environ["AZURE_OPENAI_DEPLOYMENT"],
            os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"), os.getenv("AZURE_OPENAI_API_KEY") or None))
    raise LLMError(f"unknown GLCC_LLM_PROVIDER {provider!r} (use 'none' or 'azure_openai')")
