"""
JobWingman — Python FastAPI service.

Responsibilities:
- Load the user's CV once at startup and keep it in memory for LLM prompts.
- Expose HTTP endpoints that n8n workflows call to orchestrate job scoring and delivery.
- Forward messages to Telegram via the Bot API.

Phase 1 adds:
- POST /jobs/fetch-and-score — runs the full pipeline:
    fetch → relevance filter → hard discard → dedup → LLM score → return top N
- POST /jobs/send-digest — formats scored jobs and sends the Telegram digest.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from constants import TOP_N_JOBS
from database import clear_all_seen, is_seen, make_hash, mark_seen
from filters import apply_hard_discard
from scoring import score_jobs
from sources.arbeitnow import fetch_jobs
from telegram_formatter import format_digest

CV_PATH = Path("data/cv.txt")
TELEGRAM_API_BASE = "https://api.telegram.org"
HTML_PARSE_MODE = "HTML"

cv_text: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Runs once on startup before the server starts accepting requests.
    Loads cv.txt into the module-level `cv_text` string so every scoring
    prompt can inject it without hitting the filesystem on each request.
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


class TelegramMessage(BaseModel):
    """Payload for the /telegram/send endpoint."""

    text: str


@app.get("/health")
async def health():
    """
    Health check endpoint.

    Called by n8n to verify the service is up and the CV is loaded.
    Returns cv_loaded=false as a signal that scoring will be degraded.
    """
    return {
        "status": "ok",
        "cv_loaded": bool(cv_text),
        "cv_chars": len(cv_text),
    }


@app.post("/telegram/send")
async def send_telegram(msg: TelegramMessage):
    """
    Send a message to Telegram via the Bot API.

    n8n workflows call this endpoint instead of integrating with Telegram
    directly, keeping all Telegram logic in one place.
    Raises 502 if the Telegram API rejects the request.
    """
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg.text,
                "parse_mode": HTML_PARSE_MODE,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Phase 1 — pipeline endpoints
# ---------------------------------------------------------------------------


@app.delete("/jobs/clear-db")
async def clear_db():
    """
    Delete all rows from the seen_jobs table.

    Development helper — resets the dedup state so the next pipeline run
    re-processes every job as if it were new. Not called by n8n; intended
    for manual use via curl or the Swagger UI.
    """
    deleted = clear_all_seen()
    print(f"[db] Cleared {deleted} rows from seen_jobs")
    return {"ok": True, "deleted": deleted}


@app.post("/jobs/fetch-and-score")
async def fetch_and_score():
    """
    Run the full job pipeline and return the top scored jobs.

    This is the main endpoint n8n calls on a schedule. The pipeline runs
    in a single request to keep n8n's workflow minimal (just a cron trigger
    and two HTTP calls).

    Pipeline stages (in order):
      1. Fetch — call Arbeitnow API, get normalized + relevance-filtered jobs
      2. Dedup — skip any job already in the seen_jobs table
      3. Hard discard — drop consultant/outsourcing/on-site roles (pre-LLM)
      4. Score — call Gemini for each surviving job, discard score < 6
      5. Sort — highest match_score first
      6. Top N — keep only the top 3 (configurable via TOP_N_JOBS)

    Each stage logs its input/output count so the full funnel is visible in
    the service logs.

    Error handling:
      - Arbeitnow API failure → 502 (upstream down)
      - Arbeitnow unreachable → 503 (network error)
      - Individual scoring failures are caught inside score_jobs() and
        logged — one bad job does not abort the batch.
    """
    # 1. Fetch
    try:
        jobs = await fetch_jobs()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Arbeitnow API error: {e.response.status_code}",
        )
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Arbeitnow unreachable: {str(e)}",
        )

    # 2. Dedup — check against seen_jobs, then mark survivors as seen
    new_jobs = []
    for job in jobs:
        job_hash = make_hash(job["title"], job["company"])
        if is_seen(job_hash):
            print(f"[dedup] SKIP — {job['title']} @ {job['company']}")
            continue
        mark_seen(job_hash, job["title"], job["company"], job["source"])
        new_jobs.append(job)
    print(f"[dedup] {len(jobs)} in → {len(new_jobs)} new")

    # 3. Hard discard
    filtered = apply_hard_discard(new_jobs)

    # 4. Score via Gemini
    scored = await score_jobs(filtered, cv_text)

    # 5. Sort by match_score descending
    scored.sort(
        key=lambda j: float(j.get("scoring", {}).get("match_score", 0)),
        reverse=True,
    )

    # 6. Top N
    top = scored[:TOP_N_JOBS]

    print(
        f"[pipeline] DONE — {len(jobs)} fetched → {len(new_jobs)} new "
        f"→ {len(filtered)} after filter → {len(scored)} scored "
        f"→ {len(top)} delivered"
    )

    return {
        "stats": {
            "fetched": len(jobs),
            "new": len(new_jobs),
            "after_filter": len(filtered),
            "scored": len(scored),
            "delivered": len(top),
        },
        "jobs": top,
    }


@app.post("/jobs/send-digest")
async def send_digest():
    """
    Run the pipeline and send the results as a Telegram digest.

    This is a convenience endpoint that combines fetch-and-score + Telegram
    delivery in a single call, so n8n only needs one HTTP node.

    If the pipeline returns zero jobs worth showing, a "nothing today"
    message is sent instead — David always gets feedback, even on dry days.

    If the pipeline fails (LLM down, API key missing, network error), an
    error message is sent to Telegram so David knows something broke —
    no silent failures.
    """
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        result = await fetch_and_score()
        top_jobs = result["jobs"]
        stats = result["stats"]
        messages = format_digest(top_jobs, stats)
    except Exception as e:
        # Pipeline failed — send error to Telegram.
        error_message = (
            "🚨 <b>JobWingman pipeline failed</b>\n\n"
            f"Error: <code>{type(e).__name__}: {e}</code>\n\n"
            "Check the service logs for details."
        )
        print(f"[pipeline] FATAL — {type(e).__name__}: {e}")
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": error_message,
                    "parse_mode": HTML_PARSE_MODE,
                },
            )
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline failed: {type(e).__name__}: {e}",
        )

    # Send the digest — may be multiple messages if the content exceeds
    # Telegram's 4096-character limit per message.
    async with httpx.AsyncClient() as client:
        for message in messages:
            resp = await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": HTML_PARSE_MODE,
                },
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=resp.text)

    return {"ok": True, "jobs_sent": len(top_jobs), "stats": stats}
