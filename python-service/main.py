"""
JobWingman — Python FastAPI service.

Responsibilities:
- Load the user's CV once at startup and keep it in memory for LLM prompts.
- Expose HTTP endpoints that n8n workflows call to orchestrate job scoring and delivery.
- Forward messages to Telegram via the Bot API.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
        resp = await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg.text,
            "parse_mode": HTML_PARSE_MODE,
        })
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    return {"ok": True}
