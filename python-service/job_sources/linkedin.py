"""
JobWingman — LinkedIn job fetcher (via python-jobspy).

Responsibilities:
- Run one LinkedIn guest-search scrape per term in LINKEDIN_SEARCH_TERMS.
- Aggregate and dedup results by job_url across terms.
- Normalize each DataFrame row into the canonical Job shape.
- Apply a strict in-source title filter (AI/LLM only — no backend or
  full-stack) before the job reaches the global pipeline.

Why python-jobspy:
  LinkedIn is the most restrictive source to scrape — it fingerprints TLS,
  rate-limits aggressively (~10 pages per IP), and its markup changes. Rather
  than rebuild all of that ourselves, we lean on JobSpy, which already wraps
  LinkedIn's public guest endpoint (linkedin.com/jobs-guest/) with the right
  headers, backoff, and HTML parsing. This matches the "swap it if it breaks"
  posture: JobSpy is actively maintained and the failure mode (0 jobs + log
  line) is the same as our other scrapers.

Why wrap JobSpy in asyncio.to_thread:
  scrape_jobs is synchronous and uses tls-client / requests under the hood.
  The orchestrator runs all fetchers concurrently via asyncio.gather, so
  blocking the event loop here would serialize every other source behind
  LinkedIn. to_thread offloads the blocking call to a worker thread and
  keeps the gather truly concurrent.

Why per-term scrapes instead of one combined query:
  LinkedIn's boolean keyword support ("AI Engineer" OR "LLM Engineer") works
  but is prone to ranking-bias toward one term. Two separate scrapes of
  LINKEDIN_RESULTS_PER_TERM each guarantee balanced coverage and let the
  url-based dedup remove overlap cleanly.

Why an empty description is acceptable:
  LINKEDIN_FETCH_DESCRIPTION=False keeps us to one HTTP request per page
  rather than one per job — roughly halving rate-limit pressure. JobSpy
  still returns a short description from the listing card when available;
  the scorer handles empty descriptions the same way it handles Joblyst.

Failure posture:
  Any JobSpy exception (rate-limited, markup change, network error) is
  caught and logged; fetch_jobs returns []. The orchestrator calls every
  fetcher inside asyncio.gather(return_exceptions=True), so even if the
  exception escaped, a LinkedIn outage would contribute 0 jobs instead of
  breaking the whole run.
"""

import asyncio

from jobspy import scrape_jobs

from constants import (
    LINKEDIN_FETCH_DESCRIPTION,
    LINKEDIN_HOURS_OLD,
    LINKEDIN_IS_REMOTE,
    LINKEDIN_LOCATION,
    LINKEDIN_RESULTS_PER_TERM,
    LINKEDIN_SEARCH_TERMS,
    LINKEDIN_TITLE_KEYWORDS,
)
from logger import get_logger
from models.job import Job

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# JobSpy's site identifier for LinkedIn. Passed as a single-element list to
# scrape_jobs(site_name=...) so future additions (e.g. Indeed via JobSpy)
# would only require appending to the list.
_JOBSPY_SITE_LINKEDIN = "linkedin"

# Canonical source tag attached to every Job produced by this module. Must
# match the entry in constants.SOURCE_NAMES so orchestrator logging lines
# up by index.
_SOURCE_TAG = "linkedin"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _cell(row, key: str, default=""):
    """
    Safely extract a value from a JobSpy DataFrame row.

    JobSpy returns pandas Series rows where missing values show up as NaN
    rather than None. Comparing NaN to anything is always False, and pushing
    NaN into downstream string operations raises. This helper converts any
    missing/NaN/None value to the supplied default (empty string by default).
    """
    value = row.get(key, default)
    # pandas NaN is the only float that is not equal to itself.
    if value is None or (isinstance(value, float) and value != value):
        return default
    return value


def _to_int(value) -> int | None:
    """Coerce a JobSpy salary cell to int or None if missing."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize(row) -> Job:
    """
    Convert a JobSpy DataFrame row into a canonical Job instance.

    JobSpy LinkedIn field mapping:
      title          → title
      company        → company
      location       → location  (city, country string; may be empty for remote-only)
      description    → description  (short summary unless LINKEDIN_FETCH_DESCRIPTION)
      job_url        → url  (canonical LinkedIn posting URL)
      is_remote      → remote
      min_amount     → salary_min  (currency varies — LLM reads currency field context)
      max_amount     → salary_max

    Why tags is empty:
      JobSpy exposes job_function and company_industry, but both are single
      strings rather than lists. Mapping them into tags risks false negatives
      (e.g. job_function="Engineering" adding no signal). Leave empty — the
      scorer has title + summary + location to work with.
    """
    return Job(
        title=str(_cell(row, "title")).strip(),
        company=str(_cell(row, "company")).strip(),
        location=str(_cell(row, "location", "Remote")).strip() or "Remote",
        description=str(_cell(row, "description")),
        url=str(_cell(row, "job_url")).strip(),
        source=_SOURCE_TAG,
        tags=[],
        remote=bool(_cell(row, "is_remote", False)),
        salary_min=_to_int(row.get("min_amount")),
        salary_max=_to_int(row.get("max_amount")),
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one LinkedIn-specific
    relevance keyword.

    LINKEDIN_TITLE_KEYWORDS is deliberately stricter than the global
    RELEVANT_TITLE_KEYWORDS: only AI/LLM/agent/ML titles pass. This is
    because the search term "AI Engineer" on LinkedIn still surfaces
    backend and platform roles tagged with AI keywords in their
    description, which are explicitly out of scope for this source.

    Case-insensitive; keywords are lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in LINKEDIN_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Per-term scrape
# ---------------------------------------------------------------------------


def _scrape_term(term: str):
    """
    Run a single synchronous JobSpy scrape for one search term.

    Isolated in its own function so asyncio.to_thread has a clean target.
    All parameters are read from constants — no literals here.

    Returns the raw pandas DataFrame. Caller handles normalization and
    filtering. An empty DataFrame is returned on any JobSpy exception so
    a single bad term (e.g. transient 429) doesn't block the other term.
    """
    try:
        return scrape_jobs(
            site_name=[_JOBSPY_SITE_LINKEDIN],
            search_term=term,
            location=LINKEDIN_LOCATION,
            is_remote=LINKEDIN_IS_REMOTE,
            hours_old=LINKEDIN_HOURS_OLD,
            results_wanted=LINKEDIN_RESULTS_PER_TERM,
            linkedin_fetch_description=LINKEDIN_FETCH_DESCRIPTION,
        )
    except Exception as exc:
        logger.warning(
            "[linkedin] scrape FAILED for term=%r — %s: %s",
            term,
            type(exc).__name__,
            exc,
        )
        # Return a falsy sentinel; caller handles "no rows" the same way
        # it would an empty DataFrame.
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from LinkedIn.

    Flow:
      1. For each term in LINKEDIN_SEARCH_TERMS run scrape_jobs in a worker
         thread (asyncio.to_thread), concurrently with the other terms via
         asyncio.gather.
      2. Concatenate the DataFrames into a flat row iterable.
      3. Dedup by job_url across terms — LinkedIn often returns the same
         posting under both "AI Engineer" and "LLM Engineer".
      4. Normalize each row into a Job instance.
      5. Drop jobs whose titles don't match LINKEDIN_TITLE_KEYWORDS.

    Returns the filtered list. On total failure (both scrapes raise) logs
    and returns an empty list so the orchestrator treats this source as
    contributing zero jobs.
    """
    dataframes = await asyncio.gather(
        *[asyncio.to_thread(_scrape_term, term) for term in LINKEDIN_SEARCH_TERMS]
    )

    # In-source dedup key = (normalized title, normalized company). URL-only
    # dedup is insufficient because LinkedIn cross-posts identical roles under
    # different city URLs (e.g. the same "AI/ML Engineer - Remote @ YO IT
    # Consulting" posting appears once per city targeted). Collapsing by
    # (title, company) locally keeps our 60-result budget spent on distinct
    # roles. The orchestrator's global MD5(title|company) dedup will re-run
    # the same check across all sources — this local pass just saves budget.
    seen_keys: set[tuple[str, str]] = set()
    all_jobs: list[Job] = []

    for term, df in zip(LINKEDIN_SEARCH_TERMS, dataframes):
        if df is None or df.empty:
            logger.info("[linkedin] term=%r → 0 jobs", term)
            continue

        term_jobs = 0
        dropped_dupes = 0
        for _, row in df.iterrows():
            job = _normalize(row)
            if not job.title or not job.url:
                continue
            key = (job.title.lower().strip(), job.company.lower().strip())
            if key in seen_keys:
                dropped_dupes += 1
                continue
            seen_keys.add(key)
            all_jobs.append(job)
            term_jobs += 1

        logger.info(
            "[linkedin] term=%r → %d unique jobs (dropped %d cross-post dupes, "
            "%d unique across terms so far)",
            term,
            term_jobs,
            dropped_dupes,
            len(all_jobs),
        )

    relevant = [job for job in all_jobs if _is_relevant(job)]

    logger.info(
        "[linkedin] fetched %d total → %d relevant after title filter",
        len(all_jobs),
        len(relevant),
    )
    return relevant
