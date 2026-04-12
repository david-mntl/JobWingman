"""
JobWingman — Arbeitnow API job fetcher.

Responsibilities:
- Call the Arbeitnow public REST API and retrieve job listings.
- Normalize each raw API response object into the canonical job dict shape
  that every downstream module (filter, scoring, formatter) expects.
- Pre-filter by title keywords to keep only roles relevant to David's profile
  before any LLM cost is incurred.

Why Arbeitnow for Phase 1:
  Arbeitnow is a free, unauthenticated public API with an EU focus. It requires no API
  key, has no rate-limit documentation to negotiate, and returns clean JSON.
  Arbeitnow is narrower and higher signal for European remote roles.

Why httpx and not requests:
  The FastAPI service is async. httpx provides an AsyncClient that plays
  nicely with Python's event loop, whereas requests is synchronous and would
  block the event loop on every external call. httpx is already a dependency
  from Phase 0.
"""

import httpx

from constants import RELEVANT_TITLE_KEYWORDS
from logger import get_logger
from models.job import Job

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Arbeitnow API
# ---------------------------------------------------------------------------

# Public, unauthenticated endpoint — no API key required.
# Returns jobs as JSON: { "data": [ { "title", "company_name", ... } ] }
# EU-focused board, ideal for Berlin/remote-EU roles.
ARBEITNOW_API_URL = "https://www.arbeitnow.com/api/job-board-api"

# Arbeitnow returns all jobs in one endpoint — no category param needed.
# We filter by title keywords client-side (see job_sources/arbeitnow.py).
ARBEITNOW_JOBS_KEY = "data"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize(raw: dict) -> Job:
    """
    Convert a raw Arbeitnow job object into a canonical Job instance.

    All downstream modules depend on this shape. Centralizing the field
    mapping here means that if Arbeitnow changes a field name, only this
    function needs updating — nothing else in the pipeline breaks.
    """
    return Job(
        title=raw.get("title", "").strip(),
        company=raw.get("company_name", "").strip(),
        location=raw.get("location", "").strip(),
        description=raw.get("description", "").strip(),
        url=raw.get("url", "").strip(),
        source="arbeitnow",
        tags=raw.get("tags", []),
        remote=raw.get("remote", False),
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one relevant keyword.

    Cheap pre-filter that runs before hard-discard and before any LLM call.
    Arbeitnow covers all industries — this trims the full listing set down
    to roles that could plausibly match David's profile.

    Remote status is intentionally NOT a filter condition here. A hybrid
    role in Berlin is still viable. The `remote` bool captured in the job
    dict is passed downstream as a scoring signal — remote = scoring boost,
    not remote = not excluded.

    Case-insensitive match — the keyword list is all lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from Arbeitnow.

    Flow:
      1. GET /api/job-board-api  (no params needed — returns all active jobs)
      2. Parse the JSON response.
      3. Normalize each raw object to the canonical job dict shape.
      4. Drop jobs whose titles don't match any relevant keyword.
      5. Return the filtered list.

    Raises:
      httpx.HTTPStatusError  if Arbeitnow returns a non-2xx status.
      httpx.RequestError     on network-level failures (timeout, DNS, etc.).

    Both exceptions are intentionally not caught here — the caller (the
    FastAPI endpoint) handles them and returns the appropriate HTTP error
    to n8n, which can then retry or alert.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(ARBEITNOW_API_URL)
        response.raise_for_status()

    raw_jobs = response.json().get(ARBEITNOW_JOBS_KEY, [])
    normalized = [_normalize(raw) for raw in raw_jobs]
    relevant = [job for job in normalized if _is_relevant(job)]

    logger.info(
        "[arbeitnow] fetched %d total → %d relevant after title filter",
        len(raw_jobs),
        len(relevant),
    )
    return relevant
