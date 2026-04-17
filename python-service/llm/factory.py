"""
JobWingman — LLM client factory.

Single entry point for building the concrete LLMClient the service uses at
runtime. main.py should never instantiate GeminiClient or OpenRouterGemmaClient
directly; it should call build_llm_client() with a provider name and hand the
result to the pipeline.

Why a dict dispatch rather than an if/elif chain:
  - O(1) lookup, no cascading branches to read.
  - The dict keys double as a self-documenting list of supported providers.
  - Unknown providers hit a single `raise` path with a consistent message.
"""

import os
from collections.abc import Callable

from constants import (
    LLM_PROVIDER_ENV_VAR,
    LLM_PROVIDER_GEMINI,
    LLM_PROVIDER_GEMMA,
    LLM_PROVIDERS_SUPPORTED,
)

from .base import LLMClient
from .gemini import GeminiClient
from .openrouter import OpenRouterGemmaClient

# Env var names for each provider's API key. Declared here (not in
# constants.py) because they're only meaningful inside the factory — the
# rest of the codebase never reads these directly.
_GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
_OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"


def _build_gemini() -> LLMClient:
    """Construct a GeminiClient from the GEMINI_API_KEY env var."""
    return GeminiClient(api_key=os.environ.get(_GEMINI_API_KEY_ENV, ""))


def _build_gemma() -> LLMClient:
    """Construct an OpenRouterGemmaClient from the OPENROUTER_API_KEY env var."""
    return OpenRouterGemmaClient(api_key=os.environ.get(_OPENROUTER_API_KEY_ENV, ""))


# Provider name → zero-arg builder. Using builder callables (instead of
# pre-instantiated clients) defers env-var reads until the factory is
# actually invoked, which keeps import-time side effects out of this module.
_BUILDERS: dict[str, Callable[[], LLMClient]] = {
    LLM_PROVIDER_GEMINI: _build_gemini,
    LLM_PROVIDER_GEMMA: _build_gemma,
}


def build_llm_client(provider: str) -> LLMClient:
    """
    Build the LLMClient for the given provider name.

    Args:
        provider: One of the values in constants.LLM_PROVIDERS_SUPPORTED.
                  Case-insensitive — the value is lowercased before lookup
                  so `LLM_PROVIDER=Gemini` and `LLM_PROVIDER=gemini` both work.

    Returns:
        A concrete LLMClient instance, ready to pass into the pipeline.

    Raises:
        ValueError: If `provider` is not a recognised name. The message lists
            every supported value so the fix is obvious from the stack trace
            alone — no need to open this file to figure out what went wrong.
    """
    key = (provider or "").strip().lower()
    builder = _BUILDERS.get(key)
    if builder is None:
        supported = ", ".join(LLM_PROVIDERS_SUPPORTED)
        raise ValueError(
            f"Unknown LLM provider {provider!r}. "
            f"Set {LLM_PROVIDER_ENV_VAR} to one of: {supported}."
        )
    return builder()
