"""
JobWingman — FastAPI service entry point.

This file is the HTTP controller. Its only responsibilities are:
- Load the CV at startup and hold it in module-level state.
- Instantiate the LLM client once.
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
  DELETE /jobs/clear-db       — reset dedup state (dev helper)
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException


from constants import TELEGRAM_PARSE_MODE
from llm import GeminiClient
from pipeline.orchestrator import run_pipeline
from storage.database import clear_all_seen
from telegram.formatter import format_digest
from models.telegram import TelegramMessage

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

CV_PATH = Path("data/cv.txt")
TELEGRAM_API_BASE = "https://api.telegram.org"

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

    Loads cv.txt into module-level cv_text before the server starts accepting
    requests. If the file is missing, the service starts anyway but logs a
    warning — scoring will be degraded without CV content.
    """
    global cv_text
    if CV_PATH.exists():
        cv_text = CV_PATH.read_text(encoding="utf-8")
        print(f"[startup] CV loaded — {len(cv_text)} chars")
    else:
        print(f"[startup] WARNING: {CV_PATH} not found — scoring will be degraded")
    yield


app = FastAPI(title="JobWingman", version="0.1.0", lifespan=lifespan)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Instantiated once at module level — fails fast at startup if GEMINI_API_KEY
# is missing. Passed into every pipeline run via run_pipeline().
_llm_client = GeminiClient(api_key=os.environ.get("GEMINI_API_KEY", ""))


# ---------------------------------------------------------------------------
# Helper: send one Telegram message
# ---------------------------------------------------------------------------


async def _send_telegram(text: str) -> None:
    """
    Send a single message to Telegram via the Bot API.

    Raises:
      HTTPException(502)  if Telegram rejects the message.
    """
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": TELEGRAM_PARSE_MODE,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)


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
    print(f"[db] cleared {deleted} rows from seen_jobs")
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
        print(f"[pipeline] FATAL — {type(e).__name__}: {e}")
        await _send_telegram(error_msg)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed: {type(e).__name__}: {e}",
        )

    for message in messages:
        await _send_telegram(message)

    return {"ok": True, "jobs_sent": len(result.jobs), "stats": result.stats}
