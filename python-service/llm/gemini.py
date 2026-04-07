"""
JobWingman — Gemini LLM client.

Implements LLMClient for Google's Gemini generateContent API. All
Gemini-specific concerns live here: request payload shape, API key
authentication (URL query parameter), response parsing path, and
HTTP 429 retry logic. Nothing in this file is visible to scoring.py.

Why raw httpx instead of the google-generativeai SDK:
  The SDK adds a heavy dependency for what is ultimately a single POST
  request. httpx gives us full control over payload, timeout, and retry
  logic with zero extra packages.
"""

import asyncio

import httpx

from constants import (
    GEMINI_API_URL,
    GEMINI_DELAY_BETWEEN_CALLS,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MAX_RETRIES,
    GEMINI_MODEL,
    GEMINI_RETRY_BASE_DELAY,
    GEMINI_TIMEOUT_SECONDS,
)
from logger import get_logger

from .base import LLMClient

logger = get_logger(__name__)


class GeminiClient(LLMClient):
    """
    LLM client for Google Gemini's generateContent API.

    Each instance is bound to a specific model and API key. Instantiate
    once at application startup and reuse across all scoring requests.

    Args:
        api_key:            Gemini API key. Raises RuntimeError at
                            instantiation time if empty — fail fast at
                            startup rather than silently on the first call.
        model:              Gemini model identifier string.
        max_output_tokens:  Token budget for each response. 4096 is
                            sufficient for the scoring JSON; lower values
                            risk truncation (missing closing brace → parse
                            failure).
        max_retries:        How many times to retry on HTTP 429.
        retry_base_delay:   Base seconds for exponential backoff on 429.
                            Retry 1 waits base, retry 2 waits 2×base, etc.
        timeout_seconds:    Per-request HTTP timeout. 60s gives the model
                            headroom for large CV + job description prompts.
    """

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_MODEL,
        max_output_tokens: int = GEMINI_MAX_OUTPUT_TOKENS,
        max_retries: int = GEMINI_MAX_RETRIES,
        retry_base_delay: int = GEMINI_RETRY_BASE_DELAY,
        timeout_seconds: int = GEMINI_TIMEOUT_SECONDS,
    ):
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env and restart the container."
            )
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._timeout_seconds = timeout_seconds
        logger.info("LLM client ready — model: %s", self._model)

    @property
    def delay_between_calls(self) -> float:
        """
        Seconds to wait between consecutive generate() calls.

        Gemini free tier allows 15 requests/minute. A 5-second gap means
        max 12 req/min, staying safely under the limit even with retries.
        """
        return float(GEMINI_DELAY_BETWEEN_CALLS)

    async def generate(self, prompt: str) -> str:
        """
        Send the prompt to Gemini and return the raw text response.

        Retries on HTTP 429 (rate limited) with exponential backoff:
          Retry 1 waits retry_base_delay seconds, retry 2 waits 2×, etc.
        After max_retries attempts the 429 propagates to the caller.

        Why retry only on 429 and not on other errors:
          429 is transient — the quota window resets and the next call
          succeeds. Other errors (400 bad request, 401 auth, 500 upstream)
          are either permanent or indicate a real upstream problem; retrying
          would waste time and tokens without a reasonable chance of success.

        Raises:
          httpx.HTTPStatusError  on non-2xx responses (after retries).
          httpx.RequestError     on network failures.
          KeyError / IndexError  if the response structure is unexpected.
        """
        url = GEMINI_API_URL.format(model=self._model, key=self._api_key)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,  # Low temperature = consistent, structured output
                "maxOutputTokens": self._max_output_tokens,
            },
        }

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await client.post(url, json=payload)
                except httpx.TimeoutException as exc:
                    logger.error(
                        "Gemini request timed out after %ss (attempt %d/%d) — %s",
                        self._timeout_seconds,
                        attempt + 1,
                        self._max_retries + 1,
                        exc,
                    )
                    raise
                except httpx.RequestError as exc:
                    logger.error(
                        "Network error calling Gemini API (attempt %d/%d) — %s: %s",
                        attempt + 1,
                        self._max_retries + 1,
                        type(exc).__name__,
                        exc,
                    )
                    raise

                if response.status_code != 429:
                    if not response.is_success:
                        logger.error(
                            "Gemini API returned HTTP %s — body: %s",
                            response.status_code,
                            response.text[:500],
                        )
                    response.raise_for_status()

                    try:
                        text = response.json()["candidates"][0]["content"]["parts"][0][
                            "text"
                        ]
                    except (KeyError, IndexError) as exc:
                        logger.error(
                            "Unexpected Gemini response structure — %s: %s | body: %s",
                            type(exc).__name__,
                            exc,
                            response.text[:500],
                        )
                        raise

                    logger.debug(
                        "Gemini response received — response_chars: %d", len(text)
                    )
                    return text

                # 429 — rate limited. Back off and retry unless out of attempts.
                if attempt >= self._max_retries:
                    logger.error(
                        "Gemini 429 rate-limit — exhausted all %d retries, giving up",
                        self._max_retries,
                    )
                    response.raise_for_status()  # raises HTTPStatusError with the 429

                wait = self._retry_base_delay * (2**attempt)
                logger.warning(
                    "Gemini 429 rate-limited — retry %d/%d in %ss",
                    attempt + 1,
                    self._max_retries,
                    wait,
                )
                await asyncio.sleep(wait)

        # Unreachable: the loop either returns on success or raises on final 429.
        raise RuntimeError("unreachable")
