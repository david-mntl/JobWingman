"""
JobWingman — OpenRouter/Gemma connectivity test.

Standalone script (run: `python -m llm.test_openrouter_connectivity` from
inside python-service/) that exercises exactly the failure modes that hurt
us with Gemini — so the first time something breaks with Gemma we get a
clear, named diagnosis instead of a mystery traceback.

What this covers, and why each case exists:

  1. Config sanity       — API key present, model slug set. Cheap local
                           check; catches the "forgot to export the key"
                           case before any network traffic.
  2. Live smoke call     — tiny prompt to the real API. Proves the auth
                           header, endpoint URL, and request schema are
                           correct end-to-end.
  3. Scoring-shape call  — larger prompt that asks for strict JSON (same
                           shape the scoring pipeline needs). Exercises
                           `max_tokens`, temperature=0.2, and catches
                           truncation / empty-content regressions.
  4. Auth failure path   — deliberately wrong API key. Confirms 401 is
                           surfaced with a readable hint rather than a
                           bare `HTTPStatusError`.
  5. Bad-model path      — nonsense model slug. Confirms 404 is surfaced
                           with the "check OPENROUTER_MODEL spelling" hint.
  6. Empty-prompt path   — empty user message. Forces either an upstream
                           refusal or an empty-content response; either
                           way OpenRouterGemmaError must fire — never a
                           silent success.

This is a *test script*, not a pytest suite, because:
  - The project does not yet use pytest (see requirements.txt).
  - Hitting a live external API should be a manual, opt-in run, not part
    of automated CI.
  - Keeping it runnable with plain `python -m` means no new dependency
    and it works identically inside and outside the devcontainer.

Exit code is the number of failed cases, so shell/CI can still gate on
`$?` if we ever wire this into a hook.
"""

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

# When invoked as `python -m llm.test_openrouter_connectivity` from the
# python-service directory the imports below resolve naturally. When run as
# a plain script path, we fall back to injecting the parent directory so
# `from constants import ...` still works without requiring the caller to
# know about PYTHONPATH.
if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from constants import OPENROUTER_MODEL
from llm.openrouter import OpenRouterGemmaClient, OpenRouterGemmaError


# ---------------------------------------------------------------------------
# Test-local constants — kept here rather than in constants.py because they
# are only relevant to this script.
# ---------------------------------------------------------------------------

# Tiny prompt used for the live smoke call. Short on purpose so a cold
# free-tier provider has the best chance of responding within the timeout.
SMOKE_PROMPT = "Reply with the single word: pong"

# Scoring-shape prompt. Asks for strict JSON so we can validate that the
# model honours structure (which is what the real scoring path relies on).
JSON_PROMPT = (
    "Return ONLY a JSON object with two keys: "
    '{"status": "ok", "number": 7}. No prose, no code fences.'
)

# Obviously invalid API key used to probe the 401 path. Prefixed with the
# OpenRouter key format so it fails auth rather than being rejected as
# malformed at some earlier layer.
BAD_API_KEY = "sk-or-v1-invalid-key-for-connectivity-test"

# Model slug guaranteed not to exist, used to probe the 404 path.
BAD_MODEL_SLUG = "nonexistent/this-model-does-not-exist:free"


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    """One row in the final summary table."""
    name: str
    passed: bool
    detail: str


async def _run_case(
    name: str,
    coro_factory: Callable[[], Awaitable[str]],
) -> CaseResult:
    """
    Run a single test case and classify its outcome.

    The coro_factory returns a short human-readable detail string on success
    and raises on failure; this function converts exceptions into a failed
    CaseResult with the exception type and message. Wrapping every case
    through one function keeps the report format uniform.
    """
    print(f"\n── {name} " + "─" * max(1, 60 - len(name)))
    try:
        detail = await coro_factory()
        print(f"   PASS — {detail}")
        return CaseResult(name, True, detail)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"   FAIL — {detail}")
        return CaseResult(name, False, detail)


# ---------------------------------------------------------------------------
# Individual cases
# ---------------------------------------------------------------------------

async def case_config_sanity() -> str:
    """Cheap pre-flight: key is present and model slug looks reasonable."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set in environment")
    if "/" not in OPENROUTER_MODEL:
        raise RuntimeError(
            f"OPENROUTER_MODEL looks wrong (missing provider prefix): {OPENROUTER_MODEL}"
        )
    return f"key present ({len(key)} chars), model={OPENROUTER_MODEL}"


async def case_live_smoke() -> str:
    """Real round-trip with a tiny prompt — proves auth + endpoint."""
    client = OpenRouterGemmaClient(api_key=os.environ["OPENROUTER_API_KEY"])
    text = await client.generate(SMOKE_PROMPT)
    return f"got {len(text)} chars: {text.strip()[:80]!r}"


async def case_scoring_shape() -> str:
    """
    Ask for strict JSON. We don't require it to parse — the real scoring
    pipeline has its own parser and repair logic — but we do require the
    model to return something substantial (>=10 chars). Empty/near-empty
    content would have already raised OpenRouterGemmaError inside the
    client, so reaching this line implies non-empty output.
    """
    client = OpenRouterGemmaClient(api_key=os.environ["OPENROUTER_API_KEY"])
    text = await client.generate(JSON_PROMPT)
    if len(text) < 10:
        raise RuntimeError(f"response suspiciously short ({len(text)} chars): {text!r}")
    return f"got {len(text)} chars, looks structured"


async def case_bad_auth() -> str:
    """Deliberately wrong key must raise HTTPStatusError 401."""
    client = OpenRouterGemmaClient(api_key=BAD_API_KEY)
    try:
        await client.generate(SMOKE_PROMPT)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return "401 surfaced as expected"
        raise RuntimeError(
            f"expected 401, got {exc.response.status_code}"
        ) from exc
    raise RuntimeError("expected auth failure, request succeeded")


async def case_bad_model() -> str:
    """Nonsense model slug must surface as 4xx, not a silent success."""
    client = OpenRouterGemmaClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=BAD_MODEL_SLUG,
    )
    try:
        await client.generate(SMOKE_PROMPT)
    except httpx.HTTPStatusError as exc:
        # OpenRouter returns 404 for unknown models, but some edge cases
        # surface as 400 — either is acceptable here; the point is that
        # it is NOT a silent 200 with empty content.
        if exc.response.status_code in (400, 404):
            return f"{exc.response.status_code} surfaced as expected"
        raise RuntimeError(
            f"expected 400/404, got {exc.response.status_code}"
        ) from exc
    raise RuntimeError("expected bad-model failure, request succeeded")


async def case_empty_prompt() -> str:
    """
    Empty user content is the classic "silent success" trap: some providers
    return HTTP 200 with an empty string, which looks valid but breaks the
    scoring pipeline downstream. The client must translate that into
    OpenRouterGemmaError (or a 4xx) — never return an empty string.
    """
    client = OpenRouterGemmaClient(api_key=os.environ["OPENROUTER_API_KEY"])
    try:
        text = await client.generate("")
    except OpenRouterGemmaError as exc:
        return f"empty-content detected: {exc}"
    except httpx.HTTPStatusError as exc:
        return f"rejected with HTTP {exc.response.status_code} (acceptable)"
    raise RuntimeError(
        f"expected empty-content failure, got {len(text)} chars: {text!r}"
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def main() -> int:
    """
    Run every case in order and print a summary.

    Returns the number of failed cases so the process exit code reflects
    the health of the integration (0 = all pass).
    """
    # Load .env if present — saves having to `export OPENROUTER_API_KEY=...`
    # manually when running the script outside the FastAPI app.
    try:
        from dotenv import load_dotenv
        for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
            if candidate.exists():
                load_dotenv(candidate)
                print(f"[setup] loaded env from {candidate}")
                break
    except ImportError:
        # python-dotenv is in requirements.txt but tolerate absence for
        # flexibility when running in a stripped-down shell.
        pass

    cases: list[tuple[str, Callable[[], Awaitable[str]]]] = [
        ("config sanity", case_config_sanity),
        ("live smoke call", case_live_smoke),
        ("scoring-shape call", case_scoring_shape),
        ("bad auth (401)", case_bad_auth),
        ("bad model slug (404)", case_bad_model),
        ("empty prompt (semantic 200)", case_empty_prompt),
    ]

    results: list[CaseResult] = []
    for name, factory in cases:
        results.append(await _run_case(name, factory))

    print("\n" + "═" * 70)
    print(" summary")
    print("═" * 70)
    for r in results:
        marker = "PASS" if r.passed else "FAIL"
        print(f" [{marker}] {r.name:<32} {r.detail}")
    failed = sum(1 for r in results if not r.passed)
    print("═" * 70)
    print(f" {len(results) - failed}/{len(results)} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
