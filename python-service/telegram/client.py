"""
JobWingman — shared Telegram message sender.

Why a dedicated module:
  Both main.py (pipeline endpoints) and bot.py (the long-polling listener) need
  to send messages to Telegram. Extracting the HTTP call into one place means the
  token, chat ID, and parse mode are handled consistently, and neither module needs
  to import from the other — avoiding circular imports.

Why not a class:
  The sender has no state — it just makes an HTTP call. A plain async function is
  the simplest representation of a stateless operation.
"""

import httpx

from constants import TELEGRAM_PARSE_MODE
from logger import get_logger

logger = get_logger(__name__)

# Telegram Bot API base URL. The token is injected per-call so this module
# holds no credentials itself.
_TELEGRAM_API_BASE = "https://api.telegram.org"


async def send_message(token: str, chat_id: str, text: str) -> None:
    """
    Send a single HTML-formatted message to a Telegram chat.

    Uses Telegram's sendMessage API endpoint. Messages longer than 4096
    characters will be rejected by Telegram — callers are responsible for
    splitting large content before calling this function (see formatter.py).

    Args:
        token:   Telegram Bot API token (from BotFather).
        chat_id: Target chat ID (user or group).
        text:    Message text. HTML tags (<b>, <i>, <a href>) are supported
                 because TELEGRAM_PARSE_MODE is set to "HTML".

    Raises:
        httpx.HTTPStatusError: if Telegram rejects the message (e.g. 400 for
            messages that are too long, 401 for invalid token).
    """
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": TELEGRAM_PARSE_MODE,
            },
        )

    if resp.status_code != 200:
        logger.error(
            "[telegram] send failed — status %d: %s",
            resp.status_code,
            resp.text[:200],
        )
        resp.raise_for_status()
