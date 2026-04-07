"""
JobWingman — RemoteOK job fetcher.

Responsibilities:
- Call the RemoteOK public JSON API and retrieve job listings.
- Normalize each raw API response object into the canonical job dict shape.
- Pre-filter by title keywords before any LLM cost is incurred.
- Strip HTML from descriptions (RemoteOK descriptions contain HTML markup).

Why RemoteOK for Phase 3:
  RemoteOK is a well-established remote job board with a public, unauthenticated
  JSON API. It returns structured data including tags, salary ranges
  (salary_min/salary_max in USD), and company info. All jobs on the platform are
  remote by definition. No API key or rate-limit negotiation required.

Why httpx and not requests:
  See arbeitnow.py — same reasoning applies. The FastAPI service is async;
  httpx AsyncClient plays nicely with the event loop.

Why html.parser and not BeautifulSoup:
  BeautifulSoup would be the natural choice for HTML stripping, but it requires
  an additional dependency. The descriptions here are simple HTML (paragraph tags,
  lists, emphasis) — Python's stdlib html.parser is sufficient and keeps the
  dependency footprint minimal.
"""

import html
import html.parser
import httpx

from constants import RELEVANT_TITLE_KEYWORDS, REMOTEOK_API_URL, REMOTEOK_JOBS_OFFSET
from models.job import Job

# ---------------------------------------------------------------------------
# HTML stripper
# ---------------------------------------------------------------------------


class _HTMLStripper(html.parser.HTMLParser):
    """Minimal HTMLParser subclass that collects only the text content."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts).strip()


def _strip_html(raw: str) -> str:
    """
    Return the plain-text content of an HTML string.

    Strips all tags and decodes HTML entities (e.g. &amp; → &).
    Returns an empty string if the input is empty or None.
    """
    if not raw:
        return ""
    # html.unescape first so entities in attribute values don't leak through
    unescaped = html.unescape(raw)
    stripper = _HTMLStripper()
    stripper.feed(unescaped)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(raw: dict) -> Job:
    """
    Convert a RemoteOK job object into a canonical Job instance.

    RemoteOK field mapping:
      position  → title   (RemoteOK uses "position", not "title")
      company   → company
      location  → location (defaults to "Remote" when absent)
      description → description (HTML — stripped to plain text)
      url       → url
      tags      → tags    (already a list of strings)
      salary_min, salary_max → passed through for the scoring prompt

    Why salary_min/salary_max are passed through:
      RemoteOK provides salary ranges in USD. The scorer can use these to
      populate salary_signal and flag roles below David's floor — without us
      needing to duplicate that logic here.
    """
    return Job(
        title=raw.get("position", "").strip(),
        company=raw.get("company", "").strip(),
        location=raw.get("location", "Remote").strip() or "Remote",
        description=_strip_html(raw.get("description", "")),
        url=raw.get("url", "").strip(),
        source="remoteok",
        tags=raw.get("tags", []),
        remote=True,  # all jobs on RemoteOK are remote by definition
        salary_min=raw.get("salary_min"),  # int USD or None
        salary_max=raw.get("salary_max"),  # int USD or None
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one relevant keyword.
    Case-insensitive, all keywords are lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from RemoteOK.

    Flow:
      1. GET /api  — returns a JSON array; element 0 is metadata, jobs start
         at REMOTEOK_JOBS_OFFSET (index 1).
      2. Skip any element that lacks a "position" key (metadata objects,
         malformed entries).
      3. Normalize each valid job object.
      4. Drop jobs whose titles don't match any relevant keyword.
      5. Return the filtered list.

    Raises:
      httpx.HTTPStatusError  if RemoteOK returns a non-2xx status.
      httpx.RequestError     on network-level failures (timeout, DNS, etc.).

    Both exceptions are intentionally not caught here. The orchestrator calls
    this inside asyncio.gather(return_exceptions=True), so a failure here
    contributes 0 jobs to the run rather than aborting the whole pipeline.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(REMOTEOK_API_URL)
        response.raise_for_status()

    raw_list = response.json()

    # Skip the metadata element(s) at the start of the array
    job_objects = raw_list[REMOTEOK_JOBS_OFFSET:]

    # Skip anything that isn't a real job dict (missing position key)
    valid = [
        item for item in job_objects if isinstance(item, dict) and item.get("position")
    ]

    normalized = [_normalize(raw) for raw in valid]
    relevant = [job for job in normalized if _is_relevant(job)]

    print(
        f"[remoteok] fetched {len(valid)} total "
        f"→ {len(relevant)} relevant after title filter"
    )
    return relevant
