"""
JobWingman — FastAPI service entry point.

This file is the HTTP controller. Its only responsibilities are:
- Load the CV at startup and hold it in module-level state.
- Instantiate the LLM client once.
- Start the Telegram bot listener as a background task.
- Define HTTP endpoints, map requests to pipeline/service calls, and return
  HTTP responses.

It does NOT contain business logic. All job discovery, dedup, filtering, and
scoring logic lives in pipeline/orchestrator.py. This separation (controller vs
logic) means each piece can be read, tested, and modified independently.

Endpoints:
  GET  /health                — liveness check + CV load status
  POST /telegram/send         — send an arbitrary message to Telegram
  POST /jobs/fetch-and-score  — run the full pipeline, return top jobs as JSON
  POST /jobs/send-digest      — run pipeline + send Telegram digest
  POST /jobs/analyze-url      — fetch, score, and send a single job URL (Phase 5)
  DELETE /jobs/clear-db       — reset dedup state (dev helper)
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from constants import (
    CV_PATH,
    LLM_PROVIDER_DEFAULT,
    LLM_PROVIDER_ENV_VAR,
)
from logger import get_logger
from llm import build_llm_client
from job_sources.url_scraper import analyze_url
from pipeline.orchestrator import run_pipeline
from storage.database import (
    clear_all_seen,
    delete_pending_job,
    get_pending_job,
    get_saved_jobs,
    insert_pending_job,
    make_hash,
    save_job,
)
from telegram.bot import TelegramBotListener, _make_save_markup
from telegram.client import send_message
from telegram.formatter import format_digest, format_single_job
from models.telegram import TelegramMessage, AnalyzeUrlRequest

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_CV_FILE = Path(CV_PATH)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
N8N_WEBHOOK_URL = os.environ["N8N_WEBHOOK_URL"]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# cv_text is loaded once at startup and passed into every pipeline run.
# Using a module-level variable avoids re-reading the file on every request
# and keeps the CV available to any endpoint without dependency injection.
cv_text: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Loads cv.txt, instantiates the Telegram bot listener as a background asyncio
    task, then yields control to the server. On shutdown the polling task is
    cancelled cleanly.

    Why the bot starts here rather than at module level:
      The bot's analyze_fn closure captures cv_text, which must be loaded first.
      The lifespan context guarantees the correct startup order.
    """
    global cv_text
    if _CV_FILE.exists():
        cv_text = _CV_FILE.read_text(encoding="utf-8")
        logger.info("[startup] CV loaded — %d chars", len(cv_text))
    else:
        logger.warning("[startup] %s not found — scoring will be degraded", _CV_FILE)

    bot = TelegramBotListener(
        token=TELEGRAM_BOT_TOKEN,
        chat_id=TELEGRAM_CHAT_ID,
        n8n_webhook_url=N8N_WEBHOOK_URL,
        analyze_fn=lambda url: analyze_url(url, cv_text, _llm_client),
        save_fn=save_job,
        get_saved_jobs_fn=get_saved_jobs,
        insert_pending_job_fn=insert_pending_job,
        get_pending_job_fn=get_pending_job,
        delete_pending_job_fn=delete_pending_job,
    )
    bot_task = bot.start()

    yield

    bot_task.cancel()
    logger.info("[shutdown] bot polling task cancelled")


app = FastAPI(title="JobWingman", version="0.1.0", lifespan=lifespan)


# Instantiated once at module level via the factory so the provider is
# selected from the LLM_PROVIDER env var (falling back to LLM_PROVIDER_DEFAULT
# when unset). Swapping between Gemini and Gemma is now an env-var change,
# not a code edit. Passed into every pipeline run via run_pipeline().
_llm_provider = os.environ.get(LLM_PROVIDER_ENV_VAR, LLM_PROVIDER_DEFAULT)
_llm_client = build_llm_client(_llm_provider)
logger.info("[startup] LLM provider = %s", _llm_provider)


# ---------------------------------------------------------------------------
# Helper: send one Telegram message (wraps shared client for this service's
# credentials, and maps HTTP errors to FastAPI HTTPException for endpoints)
# ---------------------------------------------------------------------------
async def _send_telegram(text: str) -> None:
    """
    Send a single message to Telegram using this service's credentials.

    Wraps telegram.client.send_message so endpoints don't need to pass the
    token and chat_id explicitly on every call. Converts httpx errors into
    HTTPException(502) so FastAPI returns a clean error response.

    Raises:
      HTTPException(502)  if Telegram rejects the message.
    """
    try:
        await send_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


async def _send_telegram_with_markup(text: str, reply_markup: dict) -> None:
    """
    Send a Telegram message with an inline keyboard attached.

    Wraps send_message() with reply_markup, raising HTTPException on failure
    the same way _send_telegram() does — so callers don't need separate
    error handling for the two send paths.
    """
    try:
        await send_message(
            TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text, reply_markup=reply_markup
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    """
    Liveness check.

    Returns cv_loaded=false when cv.txt was not found at startup — a signal
    that scoring will produce lower-quality results.
    """
    return {
        "status": "ok",
        "cv_loaded": bool(cv_text),
        "cv_chars": len(cv_text),
    }


@app.post("/telegram/send")
async def send_telegram(msg: TelegramMessage):
    """
    Send an arbitrary message to Telegram.

    n8n workflows call this endpoint instead of integrating with Telegram
    directly, keeping all Telegram logic in one place.
    """
    await _send_telegram(msg.text)
    return {"ok": True}


@app.delete("/jobs/clear-db")
async def clear_db():
    """
    Delete all rows from the seen_jobs table.

    Development helper — resets the dedup state so the next pipeline run
    treats every job as new. Not called by n8n; intended for manual use via
    curl or the Swagger UI at /docs.
    """
    deleted = clear_all_seen()
    logger.info("[db] cleared %d rows from seen_jobs", deleted)
    return {"ok": True, "deleted": deleted}


@app.post("/jobs/fetch-and-score")
async def fetch_and_score():
    """
    Run the full job discovery pipeline and return the top scored jobs.

    Delegates entirely to run_pipeline() — this endpoint is a thin HTTP
    wrapper. The pipeline fetches all sources concurrently, deduplicates,
    filters, scores, and returns the top N jobs.

    Individual source failures are absorbed by the pipeline (logged, 0 jobs
    contributed). A top-level failure (e.g. LLM client not configured) raises
    a 500.
    """
    try:
        result = await run_pipeline(cv_text, _llm_client)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed: {type(e).__name__}: {e}",
        )
    return {"stats": result.stats, "jobs": result.jobs}


@app.post("/jobs/send-digest")
async def send_digest():
    """
    Run the pipeline and deliver the results as a Telegram digest.

    Combines fetch-and-score + Telegram delivery in one call so n8n only
    needs a single HTTP node in the daily workflow.

    If the pipeline returns zero jobs, a "nothing today" message is sent —
    user always gets feedback, even on dry days.

    If the pipeline itself fails, an error message is sent to Telegram and the
    endpoint raises 500 — no silent failures.
    """
    try:
        result = await run_pipeline(cv_text, _llm_client)
        messages = format_digest(result.jobs, result.stats)
    except Exception as e:
        error_msg = (
            "🚨 <b>JobWingman pipeline failed</b>\n\n"
            f"Error: <code>{type(e).__name__}: {e}</code>\n\n"
            "Check the service logs for details."
        )
        logger.error("[pipeline] FATAL — %s: %s", type(e).__name__, e)
        await _send_telegram(error_msg)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed: {type(e).__name__}: {e}",
        )

    for i, message in enumerate(messages):
        # format_digest() returns [header, card_0, card_1, ..., footer].
        # Job cards occupy indices 1 through len(messages)-2 (not the first
        # header or the last footer). Each card maps to result.jobs[i - 1].
        is_job_card = len(result.jobs) > 0 and 0 < i < len(messages) - 1
        if is_job_card:
            job = result.jobs[i - 1]
            insert_pending_job(job)
            await _send_telegram_with_markup(message, _make_save_markup(job.hash))
        else:
            await _send_telegram(message)

    return {"ok": True, "jobs_sent": len(result.jobs), "stats": result.stats}


@app.post("/jobs/analyze-url")
async def analyze_url_endpoint(req: AnalyzeUrlRequest):
    """
    Fetch, score, and deliver a single job posting URL as a Telegram card.

    Accepts any job posting URL, fetches the page, uses the LLM to extract
    structured job fields, runs hard-discard and scoring, and sends the result
    to Telegram in the same card format as the daily digest.

    Deduplication is intentionally skipped — the user explicitly requested this
    URL so it is always analyzed regardless of prior exposure.

    The endpoint always sends something to Telegram:
      - On success: a scored job card.
      - On failure: a specific error message explaining what went wrong.

    This makes the endpoint safe to call from curl or n8n without also reading
    the response body — the user sees the outcome in Telegram either way.
    """
    result = await analyze_url(req.url, cv_text, _llm_client)

    if result.error:
        await _send_telegram(result.error)
        return {"ok": False, "error": result.error}

    # URL scraper skips the dedup pipeline, so hash may be None.
    # Compute it now so the Save button callback has a stable lookup key.
    if not result.job.hash:
        result.job.hash = make_hash(result.job.title, result.job.company)

    # Persist to SQLite so the Save button survives a service restart before
    # the user taps it (same mechanism used by the bot's _handle_url path).
    insert_pending_job(result.job)

    card = format_single_job(result.job)
    await _send_telegram_with_markup(card, _make_save_markup(result.job.hash))
    return {"ok": True}
