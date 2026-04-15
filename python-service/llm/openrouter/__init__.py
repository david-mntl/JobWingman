"""
JobWingman — OpenRouter provider subpackage.

Groups every OpenRouter-specific concern (per-model client modules, the
long-form troubleshooting notes in troubleshooting_reference.md, the live connectivity test
script) under one folder.
"""

from .gemma import OpenRouterGemmaClient, OpenRouterGemmaError

__all__ = ["OpenRouterGemmaClient", "OpenRouterGemmaError"]
