"""
JobWingman — RemoteRocketship job fetcher.

Responsibilities:
- Fetch multiple pages from RemoteRocketship with the user-specified server-side
  filter params applied.
- Extract the embedded Next.js __NEXT_DATA__ JSON payload from the HTML response.
- Normalize each job object into the canonical job dict shape.
- Pre-filter by title keywords before any LLM cost is incurred.

Why RemoteRocketship for Phase 3:
  RemoteRocketship aggregates 160k+ remote job listings from multiple sources
  and is updated with ~30k new jobs per week. The user-specified filter URL
  (AI Engineer, Europe/Worldwide, Berlin, hybrid included) was confirmed to
  work server-side — the server returns AI-specific roles rather than the
  unfiltered default (which returns unrelated Sales roles).

Why HTML scraping and not the API:
  RemoteRocketship returns HTTP 403 on all /api/* paths — they actively block
  direct API access. HTML viewing works fine. The page uses the standard
  Next.js __NEXT_DATA__ script tag pattern, which is simpler to parse than
  Joblyst's __next_f push calls.

Why a browser-like User-Agent is required:
  The site may reject requests without a recognisable browser User-Agent.
  We use a Chrome-on-Linux UA string — the same one that works in standard
  browsing. No cookies or session tokens are required for the listing page.

Filter URL (user-specified, server-side confirmed):
  ?page={n}&sort=DateAdded&jobTitle=AI+Engineer
    &locations=Europe%2CWorldwide&showHybridJobs=true&locationCity=Berlin

  Verified by comparing unfiltered (Sales Representative results) vs filtered
  (AI Engineer, Generative AI Architect, AI Architect results) — completely
  different job sets, proving server-side respect of these params.
"""

import asyncio
import json
import re

import httpx

from constants import (
    RELEVANT_TITLE_KEYWORDS,
    REMOTEROCKETSHIP_BASE_URL,
    REMOTEROCKETSHIP_FILTER_PARAMS,
    REMOTEROCKETSHIP_PAGES_TO_FETCH,
)
from models.job import Job

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUEST_TIMEOUT = 30

# Key path into the __NEXT_DATA__ JSON structure where job listings live.
# Full path: props → pageProps → initialJobOpenings
_NEXT_DATA_JOBS_PATH = ("props", "pageProps", "initialJobOpenings")

# Browser-like headers required — the site returns 403 on API paths and may
# reject obviously non-browser requests. These headers mimic a standard
# Chrome request on Linux.
_REMOTEROCKETSHIP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _extract_jobs_from_html(html_text: str) -> list[dict]:
    """
    Extract the job listing array from a RemoteRocketship HTML page.

    RemoteRocketship uses the standard Next.js pattern of embedding page props
    in a <script id="__NEXT_DATA__" type="application/json"> tag. This is
    simpler than Joblyst's __next_f push calls — the full JSON is in a single
    script tag, parseable in one json.loads() call.

    The job listing lives at: props.pageProps.initialJobOpenings

    Returns an empty list if the tag is missing or the structure has changed.
    """
    match = re.search(
        r'<script\s+id="__NEXT_DATA__"\s+type="application/json"\s*>(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    # Walk the expected path: props → pageProps → initialJobOpenings
    obj = data
    for key in _NEXT_DATA_JOBS_PATH:
        if not isinstance(obj, dict):
            return []
        obj = obj.get(key)
        if obj is None:
            return []

    if not isinstance(obj, list):
        return []

    return obj


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(raw: dict) -> Job:
    """
    Convert a RemoteRocketship job object into a canonical Job instance.

    RemoteRocketship field mapping (verified against live API response):
      roleTitle             → title  (NOT "title" — the field is "roleTitle")
      company.name          → company  (nested object)
      location              → location  (city/country string, e.g. "Poland")
      jobDescriptionSummary → description  (short summary; full description not in list view)
      url                   → url  (direct application link)
      techStack             → tags  (list of tech strings, e.g. ["Python", "Java"])

    Why remote is always True:
      RemoteRocketship is a remote-only job board. The filter URL includes
      showHybridJobs=true — all returned jobs are remote or hybrid.

    Why locationType is not used for the location field:
      locationType is "remote" for all returned jobs (same info as the remote
      bool). The location field (e.g. "Poland") gives richer context for the
      scoring prompt — it tells the scorer whether the company is in Europe.
    """
    company_obj = raw.get("company") or {}
    company_name = (
        company_obj.get("name", "")
        if isinstance(company_obj, dict)
        else str(company_obj)
    )

    # Build a job posting URL from the base + slug if a direct URL is present
    url = raw.get("url") or ""
    slug = raw.get("slug") or ""
    if not url and slug:
        url = f"{REMOTEROCKETSHIP_BASE_URL}/jobs/{slug}"

    return Job(
        title=(raw.get("roleTitle") or raw.get("categorizedJobTitle") or "").strip(),
        company=company_name.strip(),
        location=(raw.get("location") or "Remote").strip(),
        description=raw.get("jobDescriptionSummary") or "",
        url=url.strip(),
        source="remoterocketship",
        tags=raw.get("techStack") or [],
        remote=True,  # remote-only platform
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one relevant keyword.

    Even though the fetch URL uses jobTitle=AI+Engineer server-side, applying
    a title keyword filter here catches any noise the server-side filter lets
    through and removes genuinely irrelevant roles.
    Case-insensitive; all keywords are lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------


async def _fetch_page(page: int, client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch one page of RemoteRocketship results and return normalized job dicts.

    The filter params are appended to the base URL as-is — they were specified
    by the user and verified to work server-side. Only the page number varies.
    """
    url = f"{REMOTEROCKETSHIP_BASE_URL}/?page={page}&{REMOTEROCKETSHIP_FILTER_PARAMS}"
    response = await client.get(url, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()

    raw_jobs = _extract_jobs_from_html(response.text)
    return [
        _normalize(raw)
        for raw in raw_jobs
        if isinstance(raw, dict)
        and (raw.get("roleTitle") or raw.get("categorizedJobTitle"))
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from RemoteRocketship.

    Flow:
      1. Fetch REMOTEROCKETSHIP_PAGES_TO_FETCH pages concurrently using the
         user-specified server-side filter params.
      2. Merge all pages into one list.
      3. Drop jobs whose titles don't match any relevant keyword.
      4. Return the combined, filtered list.

    Raises:
      httpx.HTTPStatusError  if RemoteRocketship returns a non-2xx status.
      httpx.RequestError     on network-level failures (timeout, DNS, etc.).

    Both exceptions are intentionally not caught here. The orchestrator calls
    this inside asyncio.gather(return_exceptions=True), so a failure contributes
    0 jobs rather than aborting the whole pipeline.
    """
    async with httpx.AsyncClient(
        timeout=_REQUEST_TIMEOUT,
        headers=_REMOTEROCKETSHIP_HEADERS,
    ) as client:
        pages = await asyncio.gather(
            *[
                _fetch_page(page, client)
                for page in range(1, REMOTEROCKETSHIP_PAGES_TO_FETCH + 1)
            ]
        )

    all_jobs = [job for page_jobs in pages for job in page_jobs]
    relevant = [job for job in all_jobs if _is_relevant(job)]

    print(
        f"[remoterocketship] fetched {len(all_jobs)} total "
        f"→ {len(relevant)} relevant after title filter"
    )
    return relevant
