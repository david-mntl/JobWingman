"""
JobWingman — abstract LLM client interface.

Defines the contract that every LLM provider must satisfy. Scoring logic in
scoring.py depends only on this interface — not on any concrete provider —
so swapping Gemini for Claude or OpenAI means writing a new subclass and
updating the instantiation in main.py. Nothing in the business logic changes.

Why an abstract class instead of a protocol:
  ABC gives us runtime enforcement (instantiating a subclass that hasn't
  implemented generate() raises TypeError immediately) and makes the
  inheritance hierarchy explicit in IDEs and type checkers.
"""

from abc import ABC, abstractmethod


class LLMClient(ABC):
    """
    Abstract base class for LLM provider clients.

    Subclasses must implement generate(). They may also override
    delay_between_calls to advertise their inter-request rate limit
    so callers can pace themselves without knowing the provider.
    """

    @abstractmethod
    async def generate(self, prompt: str) -> str:
        """
        Send a prompt and return the raw text response.

        Args:
            prompt: The full prompt string to send to the model.

        Returns:
            Raw text from the model — not yet parsed or validated.

        Raises:
            Any network or API error specific to the provider.
        """

    @property
    def delay_between_calls(self) -> float:
        """
        Seconds to wait between consecutive generate() calls.

        Override in provider subclasses that have free-tier rate limits.
        Default is 0.0 — no delay, suitable for paid tiers or local models.
        """
        return 0.0
