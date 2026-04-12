"""
JobWingman — Joblyst job fetcher.

Responsibilities:
- Fetch multiple pages of the Joblyst job listing with server-side filters applied.
- Extract the embedded Next.js JSON payload from the HTML response.
- Normalize each job object into the canonical job dict shape.
- Pre-filter by title keywords before any LLM cost is incurred.

Why Joblyst for Phase 3:
  Joblyst (joblyst.tech) is an EU-focused job board with an AI categorization
  layer on top of listings. It has rich structured fields (salary in EUR,
  ai_skills, ai_work_mode, seniority level) and most importantly supports
  server-side filtering via URL query params — verified by testing. This means
  we can fetch only Engineering + remote/hybrid roles rather than scraping all
  2,955 listings and filtering client-side.

Why HTML scraping instead of an API:
  Joblyst has no public REST or RSS API. The page is a Next.js SSR application
  that embeds job data in <script> tags as __next_f push calls. Parsing this
  embedded JSON gives us the same structured data the page renders.

Why regex-based extraction and not BeautifulSoup:
  The __next_f payload is inside raw <script> text, not in HTML attributes.
  A simple regex to find the push() calls and then json.loads() on each
  candidate string is faster and has no extra dependencies. The extraction is
  inherently fragile (Next.js hydration format can change on a deploy), but
  the failure mode is detectable: 0 jobs returned + a log line.

Server-side filtering:
  ?mode=hybrid%2Cremote&category=Engineering reduces 2,955 total jobs to ~407.
"""

import asyncio
import json
import re

import httpx

from constants import (
    JOBLYST_BASE_URL,
    JOBLYST_CATEGORY_FILTER,
    JOBLYST_MODE_FILTER,
    JOBLYST_PAGES_TO_FETCH,
    RELEVANT_TITLE_KEYWORDS,
)
from logger import get_logger
from models.job import Job

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Timeout per page request. Joblyst is SSR and renders the full page
# server-side — may be slower than a pure API call.
_REQUEST_TIMEOUT = 30

# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _extract_jobs_from_html(html_text: str) -> list[dict]:
    """
    Extract the job listing array from a Joblyst HTML page.

    Joblyst is a Next.js app using React Server Components (RSC). It hydrates
    the page by appending to the __next_f array via script tags:
        __next_f.push([1, "<rsc_payload_string>"])
    where the RSC payload string contains newline-separated lines in the form:
        <ref_id>:<json_value>

    The push call that contains `initialData` holds the component props with
    the job listing:
        <ref>:["$", "$L<id>", null, {"initialData": {"jobs": [...]}}]

    Strategy:
      1. Find all __next_f.push([...]) calls via regex.
      2. For each push, parse the outer `[number, "string"]` as JSON.
      3. Split the RSC payload string on newlines.
      4. Find the line containing "initialData".
      5. Strip the leading `<ref>:` and parse the rest as JSON.
      6. Extract props[3].initialData.jobs (the React element's props).

    Returns an empty list if no job data is found — the caller logs this as
    a 0-job fetch, which is the detectable failure mode for this fragile
    extraction approach.
    """
    # The push calls are plain __next_f.push (no "self." prefix on Joblyst)
    push_args = re.findall(r"__next_f\.push\(\[(.*?)\]\s*\)", html_text, re.DOTALL)

    for raw_arg in push_args:
        if "initialData" not in raw_arg:
            continue

        # Parse the outer array: [number, "rsc_payload_string"]
        try:
            arr = json.loads(f"[{raw_arg}]")
        except json.JSONDecodeError:
            continue

        if len(arr) < 2 or not isinstance(arr[1], str):
            continue

        rsc_payload = arr[1]

        # Each line in the RSC payload is: <ref_id>:<json_value>
        for line in rsc_payload.split("\n"):
            if "initialData" not in line:
                continue

            # Strip the leading reference ID and colon
            colon_idx = line.find(":")
            if colon_idx == -1:
                continue

            try:
                element = json.loads(line[colon_idx + 1 :])
            except json.JSONDecodeError:
                continue

            # The element is a React tuple: ["$", "$L<id>", null, props]
            # props is element[3]; jobs live at props["initialData"]["jobs"]
            if not isinstance(element, list) or len(element) < 4:
                continue

            props = element[3]
            if not isinstance(props, dict):
                continue

            jobs = props.get("initialData", {}).get("jobs", [])
            if jobs:
                return jobs

    return []


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(raw: dict) -> Job:
    """
    Convert a Joblyst job object into a canonical Job instance.

    Joblyst field mapping:
      title         → title
      company.name  → company   (company is a nested object)
      location      → location
      external_url  → url
      ai_skills     → tags      (AI-categorized skill list)
      ai_work_mode  → remote    (True if "remote" or "hybrid")
      salary_min    → salary_min (EUR, int or None)
      salary_max    → salary_max (EUR, int or None)

    Why description is empty:
      The Joblyst listing page does not include the full job description in the
      embedded JSON — only the list metadata. The LLM will score based on title,
      company, location, tags, and salary. This is lower fidelity than sources
      that include a full description, but the AI-categorized tags and salary
      data compensate significantly.
    """
    company_obj = raw.get("company") or {}
    company_name = company_obj.get("name", "") if isinstance(company_obj, dict) else ""

    work_mode = raw.get("ai_work_mode", "")
    is_remote = work_mode in ("remote", "hybrid")

    return Job(
        title=raw.get("title", "").strip(),
        company=company_name.strip(),
        location=raw.get("location", "").strip(),
        description="",  # not available in the list endpoint
        url=raw.get("external_url", "").strip(),
        source="joblyst",
        tags=raw.get("ai_skills", []) or [],
        remote=is_remote,
        salary_min=raw.get("salary_min"),  # int EUR or None
        salary_max=raw.get("salary_max"),  # int EUR or None
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one relevant keyword.

    Even though Joblyst is pre-filtered to the Engineering category server-side,
    we still apply the title keyword filter because "Engineering" on Joblyst
    includes roles like mechanical or civil engineering that would never pass
    scoring. The keyword filter is cheap and eliminates those before any LLM cost.

    Case-insensitive; all keywords are lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------


async def _fetch_page(page: int, client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch one page of Joblyst results and return normalized job dicts.

    Constructs the URL with server-side filter params applied:
      mode=hybrid%2Cremote  → remote and hybrid work modes only
      category=Engineering  → Engineering category (case-sensitive)
      page={n}              → pagination

    Returns an empty list if the page returns no jobs (graceful end of results)
    or if extraction fails (logged at the caller level).
    """
    url = (
        f"{JOBLYST_BASE_URL}"
        f"?mode={JOBLYST_MODE_FILTER}"
        f"&category={JOBLYST_CATEGORY_FILTER}"
        f"&page={page}"
    )
    response = await client.get(url, timeout=_REQUEST_TIMEOUT)
    response.raise_for_status()

    raw_jobs = _extract_jobs_from_html(response.text)
    return [
        _normalize(raw)
        for raw in raw_jobs
        if isinstance(raw, dict) and raw.get("title")
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from Joblyst.

    Flow:
      1. Fetch JOBLYST_PAGES_TO_FETCH pages concurrently with server-side
         filters (Engineering + remote/hybrid) applied.
      2. Merge all pages into one list.
      3. Drop jobs whose titles don't match any relevant keyword.
      4. Return the filtered list.

    Raises:
      httpx.HTTPStatusError  if Joblyst returns a non-2xx status.
      httpx.RequestError     on network-level failures (timeout, DNS, etc.).

    Both exceptions are intentionally not caught here. The orchestrator calls
    this inside asyncio.gather(return_exceptions=True), so a failure contributes
    0 jobs rather than aborting the whole pipeline.
    """
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        pages = await asyncio.gather(
            *[
                _fetch_page(page, client)
                for page in range(1, JOBLYST_PAGES_TO_FETCH + 1)
            ]
        )

    all_jobs = [job for page_jobs in pages for job in page_jobs]
    relevant = [job for job in all_jobs if _is_relevant(job)]

    logger.info(
        "[joblyst] fetched %d total → %d relevant after title filter",
        len(all_jobs),
        len(relevant),
    )
    return relevant
