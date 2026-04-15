"""
JobWingman — LLM client package.

Exports the abstract interface and every concrete implementation. To swap
providers, change the instantiation in main.py — nothing downstream of the
LLMClient interface needs to know which backend is in use.

Implementations:
  GeminiClient            — Google Gemini via generateContent API.
  OpenRouterGemmaClient   — Google Gemma via OpenRouter (chat-completions).
"""

from .base import LLMClient
from .gemini import GeminiClient
from .openrouter import OpenRouterGemmaClient, OpenRouterGemmaError

__all__ = [
    "LLMClient",
    "GeminiClient",
    "OpenRouterGemmaClient",
    "OpenRouterGemmaError",
]
