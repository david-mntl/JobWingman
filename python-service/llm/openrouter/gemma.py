"""
JobWingman — OpenRouter LLM client (Gemma variant).

Implements LLMClient against the OpenRouter `/chat/completions` endpoint,
currently pointed at Google's `google/gemma-4-31b-it:free` model. The shape
of this file mirrors `gemini.py` deliberately — same structure, same retry
helper, same failure-reporting style — so reading the two side by side
highlights only the provider-specific differences:

  - Auth lives in an `Authorization: Bearer ...` header, not a URL query
    parameter.
  - Request/response follow the OpenAI Chat Completions schema
    (`messages` / `choices[0].message.content`) rather than Gemini's
    `contents` / `candidates[0].content.parts[0].text`.
  - OpenRouter adds two failure modes that HTTP status codes alone do not
    surface and that bit us during Gemini debugging:
      1. 200 OK with an `error` object in the JSON body (upstream provider
         rejected the request but OpenRouter tunnelled it through).
      2. 200 OK with an empty `content` string (free-tier provider returned
         no text — often silent moderation or provider cold-start).
    Both are translated into loud, descriptive exceptions so a caller
    cannot confuse them with a valid empty answer.

Why raw httpx instead of the `openai` SDK (OpenRouter is OpenAI-compatible):
  Same reasoning as in gemini.py — a single POST request does not justify
  another heavyweight dependency, and we keep full control of timeout and
  retry behaviour.
"""

import asyncio
from collections.abc import Callable

import httpx

from constants import (
    OPENROUTER_503_MAX_RETRIES,
    OPENROUTER_503_RETRY_BASE_DELAY,
    OPENROUTER_API_URL,
    OPENROUTER_APP_NAME,
    OPENROUTER_DELAY_BETWEEN_CALLS,
    OPENROUTER_HTTP_REFERER,
    OPENROUTER_MAX_OUTPUT_TOKENS,
    OPENROUTER_MAX_RETRIES,
    OPENROUTER_MODEL,
    OPENROUTER_RETRY_BASE_DELAY,
    OPENROUTER_TIMEOUT_SECONDS,
)
from logger import get_logger

from ..base import LLMClient

logger = get_logger(__name__)


class OpenRouterGemmaError(RuntimeError):
    """
    Raised when OpenRouter returns a 200 response that is not actually usable.

    Two distinct conditions funnel through this one type, each with a clear
    message prefix so logs stay greppable:

      - "empty content":   the model produced an empty string (often
                           silent moderation or a provider cold-start).
      - "upstream error":  the JSON body carried an `error` object despite
                           HTTP 200 (the provider rejected the request
                           and OpenRouter forwarded the failure).

    A dedicated exception lets callers distinguish these semantic failures
    from network failures (`httpx.RequestError`) and from real HTTP errors
    (`httpx.HTTPStatusError`) without having to sniff the message text.
    """


class OpenRouterGemmaClient(LLMClient):
    """
    LLM client for Google Gemma served through OpenRouter.

    Instantiate once at application startup and reuse across all scoring
    requests. The default model is `google/gemma-4-31b-it:free`, configurable
    via OPENROUTER_MODEL in .env (mirroring how GEMINI_MODEL is overridable).

    Args:
        api_key:                OpenRouter API key. Raises RuntimeError at
                                instantiation time if empty — fail fast at
                                startup rather than silently on the first call.
        model:                  OpenRouter model slug (e.g.
                                `google/gemma-4-31b-it:free`).
        max_output_tokens:      Token budget per response. Matches the Gemini
                                client at 4096 so the scoring JSON has the
                                same headroom across providers.
        max_retries:            Retry budget for HTTP 429 (rate limit).
        retry_base_delay:       Base seconds for 429 exponential backoff.
        max_503_retries:        Retry budget for HTTP 502/503 (provider
                                unavailable). OpenRouter surfaces upstream
                                Google outages as 502, so 502 is treated the
                                same as 503 here.
        retry_503_base_delay:   Base seconds for 502/503 exponential backoff.
        timeout_seconds:        Per-request HTTP timeout. 60s matches Gemini
                                and gives Gemma headroom for CV-sized prompts.
        http_referer:           Optional `HTTP-Referer` header — OpenRouter
                                uses it to rank traffic on its leaderboard
                                and (more importantly for us) to attribute
                                free-tier usage per app.
        app_name:               Optional `X-Title` header — same purpose as
                                http_referer; shown in the OpenRouter dashboard.
    """

    # Retries for ReadTimeout. Matches GeminiClient — 3 attempts with
    # timeout_seconds as base delay (60 → 120 → 240s). Same rationale:
    # this should be a transient server-side hang.
    _TIMEOUT_MAX_RETRIES = 3

    def __init__(
        self,
        api_key: str,
        model: str = OPENROUTER_MODEL,
        max_output_tokens: int = OPENROUTER_MAX_OUTPUT_TOKENS,
        max_retries: int = OPENROUTER_MAX_RETRIES,
        retry_base_delay: int = OPENROUTER_RETRY_BASE_DELAY,
        max_503_retries: int = OPENROUTER_503_MAX_RETRIES,
        retry_503_base_delay: int = OPENROUTER_503_RETRY_BASE_DELAY,
        timeout_seconds: int = OPENROUTER_TIMEOUT_SECONDS,
        http_referer: str = OPENROUTER_HTTP_REFERER,
        app_name: str = OPENROUTER_APP_NAME,
    ):
        if not api_key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set. Add it to .env and restart the container."
            )
        self._api_key = api_key
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._max_503_retries = max_503_retries
        self._retry_503_base_delay = retry_503_base_delay
        self._timeout_seconds = timeout_seconds
        self._http_referer = http_referer
        self._app_name = app_name
        logger.info("LLM client ready — provider: openrouter, model: %s", self._model)

    @property
    def delay_between_calls(self) -> float:
        """
        Seconds to wait between consecutive generate() calls.

        Free-tier OpenRouter models are rate-limited to ~20 req/min per
        account. A 4-second gap (default) caps us at 15 req/min, leaving
        headroom for retries without tripping 429.
        """
        return float(OPENROUTER_DELAY_BETWEEN_CALLS)

    def _headers(self) -> dict[str, str]:
        """
        Build the request headers for every call.

        `Authorization` is required. `HTTP-Referer` and `X-Title` are
        optional but recommended by OpenRouter — they let the dashboard
        attribute free-tier usage to this app, which helps when debugging
        quota exhaustion ("who burned the daily budget?").
        """
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_referer:
            headers["HTTP-Referer"] = self._http_referer
        if self._app_name:
            headers["X-Title"] = self._app_name
        return headers

    async def _wait_or_raise(
        self,
        attempt: int,
        max_retries: int,
        base_delay: int,
        label: str,
        re_raise: Callable[[], None],
    ) -> None:
        """
        Shared backoff helper for retryable errors (429, 502/503, timeout).

        Identical structure to GeminiClient._wait_or_raise — kept as a local
        method rather than pulled into base.py because the two providers
        may yet diverge (e.g. OpenRouter publishes a `Retry-After` header
        we could honour) and premature sharing would make that harder.

        Args:
            attempt:     Zero-based retry counter for this specific error type.
            max_retries: Maximum retries allowed for this error type.
            base_delay:  Base seconds for exponential backoff.
            label:       Error label used in log messages.
            re_raise:    Callable that raises the appropriate error when
                         retries are exhausted.
        """
        if attempt >= max_retries:
            logger.error(
                "OpenRouter %s — exhausted all %d retries, giving up",
                label,
                max_retries,
            )
            re_raise()

        wait = base_delay * (2**attempt)
        logger.warning(
            "OpenRouter %s — retry %d/%d in %ss",
            label,
            attempt + 1,
            max_retries,
            wait,
        )
        await asyncio.sleep(wait)

    def _log_http_error(self, response: httpx.Response) -> None:
        """
        Log an OpenRouter HTTP error with the most useful status-specific hint.

        OpenRouter's docs map each status code to a fairly specific meaning
        (auth, credits, moderation, provider outage…). Surfacing that hint
        next to the raw body dramatically shortens time-to-diagnosis —
        which was exactly what hurt us with Gemini.
        """
        hints = {
            400: "bad request — malformed payload or unsupported parameter for this model",
            401: "invalid/expired OPENROUTER_API_KEY or missing Authorization header",
            402: "out of credits or free-tier daily quota exhausted (add credits or wait for reset)",
            403: "input moderation flagged the prompt",
            404: "model slug not found — check OPENROUTER_MODEL spelling",
            408: "request timed out on OpenRouter's side",
            429: "rate limit hit (free tier: ~20 req/min, 50 req/day without credits)",
            502: "upstream provider (Google) is down or returned an invalid response",
            503: "no provider available for this model right now",
        }
        hint = hints.get(response.status_code, "unexpected status")
        logger.error(
            "OpenRouter HTTP %s — %s | body: %s",
            response.status_code,
            hint,
            response.text[:500],
        )

    def _extract_text(self, response: httpx.Response) -> str:
        """
        Pull the assistant text out of an OpenRouter chat completion.

        Guards against two Gemma-on-OpenRouter failure modes that return
        HTTP 200 and would otherwise look like success:

          1. JSON body contains an `error` object (upstream rejection
             tunnelled as 200). Raised as OpenRouterGemmaError.
          2. `choices[0].message.content` is empty / missing
             (silent moderation or provider cold-start). Raised as
             OpenRouterGemmaError.

        Unexpected structural breakage (missing `choices`, wrong types)
        still falls through to KeyError/IndexError so genuinely malformed
        responses are not swallowed.
        """
        try:
            data = response.json()
        except ValueError as exc:
            logger.error(
                "OpenRouter returned non-JSON body — body: %s",
                response.text[:500],
            )
            raise OpenRouterGemmaError(f"invalid JSON from OpenRouter: {exc}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            logger.error(
                "OpenRouter upstream error in 200 body — code: %s | message: %s",
                code,
                msg,
            )
            raise OpenRouterGemmaError(f"upstream error (code={code}): {msg}")

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.error(
                "Unexpected OpenRouter response structure — %s: %s | body: %s",
                type(exc).__name__,
                exc,
                response.text[:500],
            )
            raise

        if not text or not text.strip():
            finish_reason = None
            try:
                finish_reason = data["choices"][0].get("finish_reason")
            except (KeyError, IndexError, TypeError):
                pass
            logger.error(
                "OpenRouter returned empty content — finish_reason: %s | body: %s",
                finish_reason,
                response.text[:500],
            )
            raise OpenRouterGemmaError(
                f"empty content from model (finish_reason={finish_reason})"
            )

        return text

    async def generate(self, prompt: str) -> str:
        """
        Send the prompt to Gemma via OpenRouter and return the raw text.

        Retries independently on HTTP 429 (rate limit), HTTP 502/503
        (provider unavailable), and ReadTimeout, each with its own counter
        and exponential backoff via _wait_or_raise. On every other non-2xx
        status, the most helpful hint we have is logged alongside the body
        and the error is raised via `response.raise_for_status()`.

        Why separate counters per error type:
          Each condition is unrelated — 429 is quota, 502/503 is upstream
          outage, timeout is a server hang. Independent counters prevent
          one error type from burning another's retry budget.

        Raises:
          httpx.HTTPStatusError  on non-2xx responses (after retries).
          httpx.TimeoutException on timeout (after retries).
          httpx.RequestError     on network failures (DNS, connect, etc).
          OpenRouterGemmaError   on semantic 200-OK failures (empty content,
                                 upstream error object, non-JSON body).
          KeyError / IndexError  if the response structure is unexpected.
        """
        payload = {
            "model": self._model,
            # Chat-completions schema: a single user turn is enough. The
            # scoring prompt already contains every instruction the model
            # needs, so we do not split it into system/user turns.
            "messages": [{"role": "user", "content": prompt}],
            # Low temperature = consistent, structured output. Matches Gemini.
            "temperature": 0.2,
            "max_tokens": self._max_output_tokens,
        }

        attempts_429 = 0
        attempts_503 = 0
        attempts_timeout = 0

        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            while True:
                try:
                    response = await client.post(
                        OPENROUTER_API_URL,
                        headers=self._headers(),
                        json=payload,
                    )
                except httpx.TimeoutException as exc:
                    _exc = exc

                    def _re_raise_timeout() -> None:
                        raise _exc

                    logger.warning(
                        "OpenRouter timed out after %ss (attempt %d/%d)",
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
                        "Network error calling OpenRouter — %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    raise

                if response.status_code == 429:
                    self._log_http_error(response)
                    await self._wait_or_raise(
                        attempts_429,
                        self._max_retries,
                        self._retry_base_delay,
                        "429 rate-limit",
                        re_raise=response.raise_for_status,
                    )
                    attempts_429 += 1
                    continue

                # 502 = upstream provider error, 503 = no provider available.
                # Both are transient and retryable on OpenRouter's side.
                if response.status_code in (502, 503):
                    self._log_http_error(response)
                    await self._wait_or_raise(
                        attempts_503,
                        self._max_503_retries,
                        self._retry_503_base_delay,
                        f"{response.status_code} provider unavailable",
                        re_raise=response.raise_for_status,
                    )
                    attempts_503 += 1
                    continue

                if not response.is_success:
                    self._log_http_error(response)
                response.raise_for_status()

                text = self._extract_text(response)
                logger.debug(
                    "OpenRouter response received — response_chars: %d", len(text)
                )
                return text
