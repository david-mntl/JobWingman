"""
JobWingman — Gemini provider subpackage.

Groups every Gemini-specific concern (client, future prompt tweaks,
provider-specific errors if they ever emerge) under one folder. The only
public export is GeminiClient; anything else in this package should be
treated as internal.
"""

from .client import GeminiClient

__all__ = ["GeminiClient"]
