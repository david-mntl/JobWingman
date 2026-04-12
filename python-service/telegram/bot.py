"""
JobWingman — Telegram long-polling bot listener.

Responsibilities:
- Poll Telegram's getUpdates API for incoming messages.
- Route commands and URLs to the appropriate handlers.
- Trigger the n8n pipeline webhook when the user sends /run.
- Trigger on-demand job URL analysis when the user pastes a job URL.

Why long-polling instead of a webhook:
  Webhooks require a publicly accessible HTTPS URL. During local development the service
  runs inside Docker with no public endpoint. Long-polling works in any network environment:
  the bot reaches out to Telegram, so no inbound connectivity is needed.

Why run inside FastAPI's lifespan instead of a separate process:
  The bot needs access to the same analyze_fn closure that holds cv_text and
  the LLM client. Running as a background asyncio task inside the same process
  shares those resources without any inter-process communication overhead.

Why accept analyze_fn as a callback:
  The bot needs to call analyze_url() from url_scraper.py, which itself needs
  cv_text and llm_client from main.py. Importing main.py from bot.py would
  create a circular dependency. Accepting a callback at construction time
  ("dependency injection") breaks the cycle: main.py creates the closure and
  passes it in — bot.py just calls whatever function it was given.
"""

import asyncio
import traceback

import httpx

from constants import BOT_POLL_TIMEOUT
from logger import get_logger
from telegram.client import send_message
from telegram.formatter import format_single_job, format_saved_jobs_list

logger = get_logger(__name__)

# Telegram Bot API base URL. Token injected per-call.
_TELEGRAM_API_BASE = "https://api.telegram.org"

# Seconds to wait before retrying after a polling error (network failure, etc.)
_POLL_ERROR_BACKOFF = 5

# Prefix for the callback_data field on the "Save job" inline button.
# Full value: CALLBACK_SAVE_JOB_PREFIX + job.hash
# e.g. "save:d41d8cd98f00b204e9800998ecf8427e" = 37 bytes, under Telegram's
# 64-byte callback_data limit. Exported so main.py can import it and produce
# matching callback_data when building buttons for the digest delivery.
CALLBACK_SAVE_JOB_PREFIX = "save:"


def _make_save_markup(job_hash: str) -> dict:
    """
    Build a Telegram InlineKeyboardMarkup dict for the Save job button.

    reply_markup shape expected by the Telegram Bot API:
      {"inline_keyboard": [[{"text": "...", "callback_data": "..."}]]}
    The outer list is rows; the inner list is buttons in that row.
    We use a single row with a single button.
    """
    return {
        "inline_keyboard": [[
            {"text": "💾 Save job", "callback_data": f"{CALLBACK_SAVE_JOB_PREFIX}{job_hash}"}
        ]]
    }


# Help text sent when the user sends an unrecognised message.
_HELP_TEXT = (
    "JobWingman commands:\n\n"
    "/run — trigger the full job pipeline now (no need to wait for the 7am cron)\n\n"
    "/list-jobs — view your saved jobs\n\n"
    "Or paste any job posting URL to get an instant scored analysis."
)


class TelegramBotListener:
    """
    Long-polling Telegram bot that listens for commands and job URLs.

    The bot only processes messages from the authorised TELEGRAM_CHAT_ID.
    Messages from any other chat are silently ignored — this prevents the bot
    from acting on messages if it is accidentally added to a group.

    Attributes:
        _token:              Telegram Bot API token.
        _chat_id:            Authorised chat ID (string for comparison).
        _n8n_webhook_url:    n8n webhook URL to POST when /run is received.
        _analyze_fn:         Async callback for URL analysis; injected from main.py
                             to avoid circular imports. Signature:
                             async (url: str) -> AnalyzeResult
        _save_fn:            Callable[[Job], int] — injected from main.py to avoid
                             circular imports; called to persist a job the user saves.
        _get_saved_jobs_fn:  Callable[[], list[Job]] — injected from main.py; returns
                             all saved jobs for the /list-jobs command.
        _pending_jobs:       Shared dict[str, Job] reference owned by main.py.
                             Keyed by job.hash. Both the digest delivery path and this
                             bot's URL handler populate it; the save callback consumes it.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        n8n_webhook_url: str,
        analyze_fn,
        save_fn,            # Callable[[Job], int]  — injected from main.py to avoid
                            # circular imports; called to persist a job the user saves
        get_saved_jobs_fn,  # Callable[[], list[Job]] — injected from main.py; returns
                            # all saved jobs for the /list-jobs command
        pending_jobs: dict, # Shared dict[str, Job] reference owned by main.py.
                            # Keyed by job.hash. Both the digest delivery path and this
                            # bot's URL handler populate it; the save callback consumes it.
    ) -> None:
        self._token = token
        self._chat_id = str(chat_id)
        self._n8n_webhook_url = n8n_webhook_url
        self._analyze_fn = analyze_fn
        self._save_fn = save_fn
        self._get_saved_jobs_fn = get_saved_jobs_fn
        self._pending_jobs = pending_jobs

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        """
        Start the polling loop as a background asyncio task.

        Returns the Task so the caller (main.py lifespan) can cancel it
        cleanly on shutdown.
        """
        task = asyncio.create_task(self._poll(), name="telegram-bot-poll")
        logger.info("[bot] polling started")
        return task

    # -----------------------------------------------------------------------
    # Polling loop
    # -----------------------------------------------------------------------

    async def _poll(self) -> None:
        """
        Infinite long-polling loop.

        Fetches updates from Telegram, processes each one, and advances the
        offset so each update is only processed once. On any network or
        unexpected error, logs the traceback and waits _POLL_ERROR_BACKOFF
        seconds before retrying — a single failed request never kills the loop.
        """
        offset = 0
        while True:
            try:
                updates = await self._get_updates(offset)
                for update in updates:
                    await self._handle_update(update)
                    offset = update["update_id"] + 1
            except asyncio.CancelledError:
                logger.info("[bot] polling cancelled — shutting down")
                return
            except Exception:
                logger.error("[bot] polling error:\n%s", traceback.format_exc())
                await asyncio.sleep(_POLL_ERROR_BACKOFF)

    async def _get_updates(self, offset: int) -> list[dict]:
        """
        Call Telegram's getUpdates API with long-polling.

        The timeout parameter tells Telegram to hold the connection open for
        BOT_POLL_TIMEOUT seconds before returning an empty list. This is far
        more efficient than short-polling (offset=0, no timeout).

        Returns an empty list on any network error so the loop can continue.
        """
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/getUpdates"
        params = {"offset": offset, "timeout": BOT_POLL_TIMEOUT}

        try:
            async with httpx.AsyncClient(timeout=BOT_POLL_TIMEOUT + 10) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                return data.get("result", [])
        except httpx.HTTPStatusError as exc:
            logger.warning("[bot] getUpdates HTTP error: %s", exc)
            return []
        except httpx.RequestError as exc:
            logger.warning("[bot] getUpdates network error: %s", exc)
            return []

    # -----------------------------------------------------------------------
    # Update routing
    # -----------------------------------------------------------------------

    async def _handle_update(self, update: dict) -> None:
        """
        Route a single Telegram update to the correct handler.

        Ignores:
          - Updates without a "message" field (e.g. callback_query, edited_message)
          - Messages from any chat other than the authorised TELEGRAM_CHAT_ID
          - Messages with no text field
        """
        if "callback_query" in update:
            await self._handle_save_callback(update["callback_query"])
            return

        message = update.get("message")
        if not message:
            return

        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            logger.debug("[bot] ignored message from unauthorised chat %s", chat_id)
            return

        text = (message.get("text") or "").strip()
        if not text:
            return

        logger.debug("[bot] received: %r", text[:80])

        try:
            if text.startswith("/run"):
                await self._handle_run()
            elif text.startswith("/list-jobs"):
                await self._handle_list_jobs()
            elif text.startswith("http://") or text.startswith("https://"):
                await self._handle_url(text)
            else:
                await self._send(_HELP_TEXT)
        except Exception:
            logger.error("[bot] handler error:\n%s", traceback.format_exc())
            await self._send("❌ An unexpected error occurred. Check the service logs.")

    # -----------------------------------------------------------------------
    # Handlers
    # -----------------------------------------------------------------------

    async def _handle_run(self) -> None:
        """
        Trigger the daily pipeline via the n8n webhook.

        The bot POSTs to the n8n webhook URL with a short timeout — it does not
        wait for the pipeline to complete (that takes minutes). The user will
        receive the digest in Telegram once the pipeline finishes.
        """
        await self._send("⏳ Triggering pipeline via n8n…")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(self._n8n_webhook_url)
                resp.raise_for_status()
            await self._send("✅ Pipeline started — digest incoming shortly.")
            logger.info("[bot] /run triggered n8n webhook successfully")
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            logger.error("[bot] n8n webhook call failed: %s", exc)
            await self._send(
                "❌ Could not reach the n8n webhook — is n8n running?\n"
                f"URL: <code>{self._n8n_webhook_url}</code>"
            )

    async def _handle_save_callback(self, callback_query: dict) -> None:
        """
        Handle a callback_query update triggered by the user tapping an inline button.

        Telegram sends a callback_query (rather than a message) when the user taps
        an inline keyboard button. The `data` field contains whatever string we put
        in `callback_data` when we built the markup — in our case that is
        CALLBACK_SAVE_JOB_PREFIX + job.hash.

        Why answerCallbackQuery must be called immediately:
          Telegram shows a loading spinner on the button until the bot calls
          answerCallbackQuery. If we never answer, the spinner stays visible
          indefinitely and the button appears stuck. Telegram requires the answer
          within 10 seconds of receiving the callback.

        Why we look up the job in _pending_jobs by hash:
          callback_data is limited to 64 bytes, which is enough for our "save:<hash>"
          scheme (37 bytes) but not for a full serialised Job object. The full Job is
          cached in _pending_jobs (keyed by hash) when the URL analysis result is sent,
          so the save handler can retrieve it without re-fetching or re-parsing anything.
        """
        cq_id = callback_query.get("id", "")
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id != self._chat_id:
            return

        await self._answer_callback_query(cq_id)

        callback_data = callback_query.get("data", "")
        if not callback_data.startswith(CALLBACK_SAVE_JOB_PREFIX):
            return

        job_hash = callback_data[len(CALLBACK_SAVE_JOB_PREFIX):]
        job = self._pending_jobs.get(job_hash)
        if job is None:
            await self._send("⚠️ Could not save — try running again.")
            return

        try:
            db_id = self._save_fn(job)
            job.db_id = db_id
            await self._send(f"✅ Job saved: {job.title} — {job.company}")
        except Exception:
            logger.error("[bot] save_job failed:\n%s", traceback.format_exc())
            await self._send("❌ Failed to save job. Check the service logs.")

    async def _answer_callback_query(self, callback_query_id: str) -> None:
        """
        Call Telegram's answerCallbackQuery endpoint to dismiss the loading spinner.

        Telegram shows a loading indicator on an inline button immediately after it
        is tapped. The bot must call answerCallbackQuery to dismiss it; if skipped,
        the UI shows a spinner indefinitely. Failure here is cosmetic — the save
        operation still proceeds — so we log a warning but do not raise.
        """
        url = f"{_TELEGRAM_API_BASE}/bot{self._token}/answerCallbackQuery"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json={"callback_query_id": callback_query_id})
        except Exception as exc:
            logger.warning("[bot] answerCallbackQuery failed: %s", exc)

    async def _handle_list_jobs(self) -> None:
        """
        Fetch and send all saved jobs.

        Delegates to _get_saved_jobs_fn (injected at construction) rather than
        importing storage.database directly — the same pattern used for analyze_fn
        — to avoid circular imports between bot.py and the storage layer.
        format_saved_jobs_list() may return multiple messages (one per job) so we
        send each one individually.
        """
        jobs = self._get_saved_jobs_fn()
        messages = format_saved_jobs_list(jobs)
        for msg in messages:
            await self._send(msg)

    async def _handle_url(self, url: str) -> None:
        """
        Analyze a job posting URL and return a scored card.

        Delegates to the injected analyze_fn (analyze_url from url_scraper.py).
        The result always contains a message — either the scored job card on
        success, or a specific error message explaining what went wrong.
        """
        await self._send("🔍 Analyzing job posting…")
        result = await self._analyze_fn(url)

        if result.error:
            await self._send(result.error)
        else:
            from storage.database import make_hash  # deferred import — avoids circular
            if not result.job.hash:
                result.job.hash = make_hash(result.job.title, result.job.company)
            self._pending_jobs[result.job.hash] = result.job
            try:
                await send_message(
                    self._token,
                    self._chat_id,
                    format_single_job(result.job),
                    reply_markup=_make_save_markup(result.job.hash),
                )
            except Exception:
                logger.error("[bot] send with markup failed:\n%s", traceback.format_exc())
                await self._send(format_single_job(result.job))

    # -----------------------------------------------------------------------
    # Internal send helper
    # -----------------------------------------------------------------------

    async def _send(self, text: str) -> None:
        """
        Send a message to the authorised chat.

        Logs but does not raise on failure — a failed reply should never crash
        the polling loop or prevent the next update from being processed.
        """
        try:
            await send_message(self._token, self._chat_id, text)
        except Exception as exc:
            logger.error("[bot] send failed: %s", exc)
