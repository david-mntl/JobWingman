"""
JobWingman — LLM client package.

Exports the abstract interface and the active Gemini implementation.
To add a new provider, create a new module in this package (e.g.
claude.py), subclass LLMClient, and swap the instantiation in main.py.
"""

from .base import LLMClient
from .gemini import GeminiClient

__all__ = ["LLMClient", "GeminiClient"]
