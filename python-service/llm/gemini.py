"""
JobWingman — Gemini LLM client.

Implements LLMClient for Google's Gemini generateContent API. All
Gemini-specific concerns live here: request payload shape, API key
authentication (URL query parameter), response parsing path, and
HTTP 429/503/timeout retry logic. Nothing in this file is visible to scoring.py.

Why raw httpx instead of the google-generativeai SDK:
  The SDK adds a heavy dependency for what is ultimately a single POST
  request. httpx gives us full control over payload, timeout, and retry
  logic with zero extra packages.
"""

import asyncio
from collections.abc import Callable

import httpx

from constants import (
    GEMINI_503_MAX_RETRIES,
    GEMINI_503_RETRY_BASE_DELAY,
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
        api_key:                Gemini API key. Raises RuntimeError at
                                instantiation time if empty — fail fast at
                                startup rather than silently on the first call.
        model:                  Gemini model identifier string.
        max_output_tokens:      Token budget for each response. 4096 is
                                sufficient for the scoring JSON; lower values
                                risk truncation (missing closing brace → parse
                                failure).
        max_retries:            How many times to retry on HTTP 429.
        retry_base_delay:       Base seconds for exponential backoff on 429.
                                Retry 1 waits base, retry 2 waits 2×base, etc.
        max_503_retries:        How many times to retry on HTTP 503.
        retry_503_base_delay:   Base seconds for exponential backoff on 503.
        timeout_seconds:        Per-request HTTP timeout. 60s gives the model
                                headroom for large CV + job description prompts.
    """

    # Retries for ReadTimeout. Not user-configurable — 3 attempts with
    # timeout_seconds as base delay (60 → 120 → 240s) is already generous
    # for what should be a transient server-side hang.
    _TIMEOUT_MAX_RETRIES = 3

    def __init__(
        self,
        api_key: str,
        model: str = GEMINI_MODEL,
        max_output_tokens: int = GEMINI_MAX_OUTPUT_TOKENS,
        max_retries: int = GEMINI_MAX_RETRIES,
        retry_base_delay: int = GEMINI_RETRY_BASE_DELAY,
        max_503_retries: int = GEMINI_503_MAX_RETRIES,
        retry_503_base_delay: int = GEMINI_503_RETRY_BASE_DELAY,
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
        self._max_503_retries = max_503_retries
        self._retry_503_base_delay = retry_503_base_delay
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

    async def _wait_or_raise(
        self,
        attempt: int,
        max_retries: int,
        base_delay: int,
        label: str,
        re_raise: Callable[[], None],
    ) -> None:
        """
        Shared backoff helper for retryable errors (429, 503, timeout).

        If all retries are exhausted it logs the failure and calls re_raise()
        to propagate the original error to the caller. Otherwise it logs a
        warning and sleeps for base_delay * 2^attempt seconds.

        Taking a re_raise callable instead of an httpx.Response lets this
        method serve both HTTP status errors (response.raise_for_status) and
        exceptions (a closure that re-raises the caught exception).

        Args:
            attempt:     Zero-based retry counter for this specific error type.
            max_retries: Maximum number of retries allowed for this error type.
            base_delay:  Base seconds for exponential backoff.
            label:       Error label used in log messages.
            re_raise:    Callable that raises the appropriate error when retries
                         are exhausted.
        """
        if attempt >= max_retries:
            logger.error(
                "Gemini %s — exhausted all %d retries, giving up",
                label,
                max_retries,
            )
            re_raise()

        wait = base_delay * (2**attempt)
        logger.warning(
            "Gemini %s — retry %d/%d in %ss",
            label,
            attempt + 1,
            max_retries,
            wait,
        )
        await asyncio.sleep(wait)

    async def generate(self, prompt: str) -> str:
        """
        Send the prompt to Gemini and return the raw text response.

        Retries independently on HTTP 429 (rate limited), HTTP 503 (service
        unavailable), and ReadTimeout (server accepted but went silent), each
        with their own counter and exponential backoff via _wait_or_raise.
        After the respective max retries are exhausted the error propagates.

        Why separate counters per error type:
          Each condition is unrelated — 429 is quota, 503 is overload, timeout
          is a server hang. Keeping them independent prevents one error type
          from spending another's retry budget.

        Raises:
          httpx.HTTPStatusError  on non-2xx responses (after retries).
          httpx.TimeoutException on timeout (after retries).
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

        attempts_429 = 0
        attempts_503 = 0
        attempts_timeout = 0

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            while True:
                try:
                    response = await client.post(url, json=payload)
                except httpx.TimeoutException as exc:
                    _exc = exc

                    def _re_raise_timeout() -> None:
                        raise _exc

                    logger.warning(
                        "Gemini timed out after %ss (attempt %d/%d)",
                        self._timeout_seconds,
                        attempts_timeout + 1,
                        self._TIMEOUT_MAX_RETRIES + 1,
                    )
                    await self._wait_or_raise(
                        attempts_timeout,
                        self._TIMEOUT_MAX_RETRIES,
                        self._timeout_seconds,
                        "timeout",
                        re_raise=_re_raise_timeout,
                    )
                    attempts_timeout += 1
                    continue
                except httpx.RequestError as exc:
                    logger.error(
                        "Network error calling Gemini API — %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    raise

                if response.status_code == 429:
                    await self._wait_or_raise(
                        attempts_429,
                        self._max_retries,
                        self._retry_base_delay,
                        "429 rate-limit",
                        re_raise=response.raise_for_status,
                    )
                    attempts_429 += 1
                    continue

                if response.status_code == 503:
                    await self._wait_or_raise(
                        attempts_503,
                        self._max_503_retries,
                        self._retry_503_base_delay,
                        "503 service unavailable",
                        re_raise=response.raise_for_status,
                    )
                    attempts_503 += 1
                    continue

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

                logger.debug("Gemini response received — response_chars: %d", len(text))
                return text
