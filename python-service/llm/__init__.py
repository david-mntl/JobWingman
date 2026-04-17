"""
JobWingman — LLM client package.

Exports the abstract interface, every concrete implementation, and the
factory used to build the client from a provider name. Callers should
prefer build_llm_client() over direct instantiation so provider selection
stays in one place.

Implementations:
  GeminiClient            — Google Gemini via generateContent API.
  OpenRouterGemmaClient   — Google Gemma via OpenRouter (chat-completions).
"""

from .base import LLMClient
from .factory import build_llm_client
from .gemini import GeminiClient
from .openrouter import OpenRouterGemmaClient, OpenRouterGemmaError

__all__ = [
    "LLMClient",
    "GeminiClient",
    "OpenRouterGemmaClient",
    "OpenRouterGemmaError",
    "build_llm_client",
]
