"""
JobWingman — on-demand job URL scraper and analyzer (Phase 5).

Responsibilities:
- Fetch the HTML of a job posting URL and convert it to clean plain text.
- Use the LLM to extract structured job fields from that plain text.
- Run the same hard-discard and scoring logic used by the daily pipeline.
- Return a typed result that clearly communicates success or the specific
  reason for failure to the caller.

Why LLM-based extraction instead of site-specific scrapers:
  The user can paste any job URL — LinkedIn, Greenhouse, Lever, a custom
  company careers page. Writing and maintaining one parser per site is
  impractical. Sending the clean page text to the LLM with a structured
  extraction prompt handles all sites without site-specific code. The trade-off
  is one extra LLM call per URL analysis, which is acceptable for a manual flow.

Why a NamedTuple for the result instead of raising exceptions:
  URL analysis can fail in many distinct ways (network error, not a job page,
  below score threshold). Each failure needs a different user-facing message.
  A NamedTuple with explicit error/job fields makes all outcomes visible at the
  call site without exception handling boilerplate, and lets callers send the
  right message to Telegram without knowing the internal failure chain.
"""

from typing import NamedTuple

import httpx
from bs4 import BeautifulSoup

from constants import URL_EXTRACTION_MAX_CHARS
from logger import get_logger
from llm import LLMClient
from models.job import Job
from pipeline.filters import apply_hard_discard
from pipeline.scoring import extract_json, score_job

logger = get_logger(__name__)

# Browser-like User-Agent to avoid trivial bot-detection blocks on job boards.
# Defined here (not in constants.py) because it is only ever used in this module.
_SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# LLM prompt template for extracting structured job fields from page text.
# The model is explicitly instructed to signal when the page is not a job posting
# so callers can return a clear "not a job" message instead of a generic error.
_EXTRACTION_PROMPT_TEMPLATE = """\
You are a job posting extractor. Given the text content of a web page, extract \
the job details and return a JSON object with exactly these fields:
  - title       (string)   Job title
  - company     (string)   Company name
  - location    (string)   Job location (city, country, or "Remote")
  - description (string)   Full job description text
  - remote      (boolean)  true if explicitly marked as remote, false otherwise
  - tags        (list)     Technology/skill tags mentioned (empty list if none)

IMPORTANT: If this page does NOT contain a job posting — it is a blog article, \
documentation page, general website, or any non-job content — respond with ONLY \
this exact JSON and nothing else:
  {{"error": "not_a_job_posting"}}

Return only valid JSON with no extra text, markdown fences, or explanation.

Page content:
{page_text}
"""


# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


class AnalyzeResult(NamedTuple):
    """
    Result of analyze_url().

    Exactly one of job/error will be set:
      - job is not None → success; job has .scoring attached
      - error is not None → failure; error is a human-readable message for Telegram
    """

    job: Job | None
    error: str | None


# ---------------------------------------------------------------------------
# Extraction helpers (used by analyze_url and eval/fixtures/create_fixture)
# ---------------------------------------------------------------------------


async def fetch_page_text(url: str) -> str:
    """
    Fetch a URL and return its content as plain text.

    Strips all HTML tags using BeautifulSoup with the lxml parser.
    Truncates at URL_EXTRACTION_MAX_CHARS to keep LLM prompts tight.

    Raises:
        httpx.HTTPStatusError: on non-2xx HTTP response
        httpx.RequestError:    on network-level failures (timeout, DNS, etc.)
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": _SCRAPER_USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    text = soup.get_text(separator=" ", strip=True)
    return text[:URL_EXTRACTION_MAX_CHARS]


async def extract_job_fields(
    page_text: str, url: str, llm_client: LLMClient
) -> dict | None:
    """
    Ask the LLM to extract structured job fields from page text.

    Returns:
        dict with job fields on success.
        None if the LLM signals the page is not a job posting or JSON parse fails.
    """
    prompt = _EXTRACTION_PROMPT_TEMPLATE.format(page_text=page_text)
    raw = await llm_client.generate(prompt)

    try:
        data = extract_json(raw)
    except ValueError:
        logger.warning("[url_scraper] JSON parse failed for %s | raw: %s", url, raw[:200])
        return None

    if "error" in data:
        logger.info("[url_scraper] LLM reported not a job posting: %s", url)
        return None

    return data


def _build_job(fields: dict, url: str) -> Job:
    """
    Build a Job dataclass from LLM-extracted fields.

    Uses safe .get() calls with sensible defaults so a partially-populated
    extraction result still produces a valid Job for scoring.
    """
    return Job(
        title=fields.get("title") or "Unknown Role",
        company=fields.get("company") or "Unknown Company",
        location=fields.get("location") or "Unknown",
        description=fields.get("description") or "",
        url=url,
        source="manual",
        tags=fields.get("tags") or [],
        remote=bool(fields.get("remote", False)),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def analyze_url(url: str, cv_text: str, llm_client: LLMClient) -> AnalyzeResult:
    """
    Fetch, extract, filter, and score a single job posting URL.

    This is the single entry point for on-demand URL analysis. It mirrors the
    daily pipeline stages (fetch → filter → score) but for one URL instead of
    aggregating multiple sources.

    Deduplication is intentionally skipped — the user explicitly requested this
    URL, so it should always be analyzed regardless of prior exposure.

    Each failure path returns a distinct error message so the caller can send
    an informative reply to the user rather than a generic "something went wrong".

    Args:
        url:        The job posting URL to analyze.
        cv_text:    The user's full CV, injected into the scoring prompt.
        llm_client: Provider-agnostic LLM client (Gemini in production).

    Returns:
        AnalyzeResult(job=<Job>, error=None)    on success
        AnalyzeResult(job=None, error=<str>)    on any failure
    """
    # Stage 1 — fetch page
    try:
        page_text = await fetch_page_text(url)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning("[url_scraper] fetch failed for %s: %s", url, exc)
        return AnalyzeResult(
            job=None,
            error="❌ Could not reach that URL — it may be unavailable or require login.",
        )

    logger.debug("[url_scraper] fetched %d chars from %s", len(page_text), url)

    # Stage 2 — LLM extraction
    try:
        fields = await extract_job_fields(page_text, url, llm_client)
    except Exception as exc:
        logger.warning("[url_scraper] LLM extraction error for %s: %s", url, exc)
        return AnalyzeResult(
            job=None,
            error="❌ Could not extract job details from the page content.",
        )

    if fields is None:
        return AnalyzeResult(
            job=None,
            error="❌ The URL you provided does not contain a job posting.",
        )

    job = _build_job(fields, url)
    logger.debug("[url_scraper] extracted: %s @ %s", job.title, job.company)

    # Stage 3 — hard discard
    passed = apply_hard_discard([job])
    if not passed:
        return AnalyzeResult(
            job=None,
            error="❌ This job was discarded by the hard-filter rules (e.g. on-site only, outsourcing).",
        )

    # Stage 4 — LLM scoring
    try:
        scored = await score_job(job, cv_text, llm_client)
    except Exception as exc:
        logger.warning("[url_scraper] scoring error for %s: %s", url, exc)
        return AnalyzeResult(
            job=None,
            error="❌ Scoring failed — the LLM returned an unexpected response.",
        )

    if scored is None:
        return AnalyzeResult(
            job=None,
            error="❌ Job analyzed but scored below the threshold (match score < 6.0).",
        )

    logger.info(
        "[url_scraper] scored %s @ %s → %.1f",
        scored.title,
        scored.company,
        scored.scoring.get("match_score", 0),
    )
    return AnalyzeResult(job=scored, error=None)
