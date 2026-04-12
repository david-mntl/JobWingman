"""
JobWingman — WeWorkRemotely RSS fetcher.

Responsibilities:
- Fetch two RSS feeds (Programming and DevOps/SysAdmin) concurrently.
- Parse the RSS XML and normalize each <item> into the canonical job dict shape.
- Strip HTML from descriptions (WWR descriptions contain HTML markup).
- Pre-filter by title keywords before any LLM cost is incurred.

Why WeWorkRemotely for Phase 3:
  WWR is one of the oldest and most respected remote job boards. It provides
  official RSS feeds that are free, stable, and require no authentication. The
  Programming category covers backend, AI, and general software roles; the
  DevOps/SysAdmin category covers infrastructure and platform engineering.

Why RSS over scraping:
  RSS is an officially supported, stable format. WWR maintains these feeds
  deliberately — they are not scraped HTML. This means the integration is robust
  against site redesigns and carries no terms-of-service risk.

Why xml.etree.ElementTree and not lxml or feedparser:
  ElementTree is stdlib. The RSS 2.0 format used by WWR is simple enough that
  a full-featured XML library would be overkill. feedparser is a popular
  alternative but adds a dependency. ElementTree keeps the footprint minimal.

Title format on WWR RSS:
  WWR uses the format "Company Name: Job Title" in the <title> element.
  We split on the first ": " to separate company from role. If no separator
  is found (malformed entry), the full title is used and company is left empty.
"""

import asyncio
import html
import html.parser
import xml.etree.ElementTree as ET

import httpx

from constants import (
    RELEVANT_TITLE_KEYWORDS,
    WWR_RSS_DEVOPS_URL,
    WWR_RSS_PROGRAMMING_URL,
)
from logger import get_logger
from models.job import Job

logger = get_logger(__name__)

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
    unescaped = html.unescape(raw)
    stripper = _HTMLStripper()
    stripper.feed(unescaped)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# XML namespace helpers
# ---------------------------------------------------------------------------

# WWR RSS uses a custom namespace for the full job description.
# ElementTree requires the full namespace URI when finding namespaced tags.
_WWR_NS = "https://weworkremotely.com/"


def _find_text(item: ET.Element, tag: str, ns: str | None = None) -> str:
    """
    Return the text content of a child element, or "" if not found.

    When ns is provided, the tag is searched under that XML namespace.
    ElementTree's Clark notation: {namespace_uri}tag_name.
    """
    qualified = f"{{{ns}}}{tag}" if ns else tag
    elem = item.find(qualified)
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize(item: ET.Element) -> Job | None:
    """
    Convert one RSS <item> element into a canonical Job instance.

    Returns None if the item is missing required fields (title or link),
    so the caller can skip malformed entries without crashing.

    WWR title format: "Company Name: Job Title"
    We split on the first ": " to separate company from role title.
    If no separator is found, the full title is used and company is "".
    """
    raw_title = _find_text(item, "title")
    link = _find_text(item, "link")

    if not raw_title or not link:
        return None

    # Split "Company: Title" on the first ": " separator
    if ": " in raw_title:
        company, title = raw_title.split(": ", 1)
    else:
        company = ""
        title = raw_title

    # <region> is a WWR-specific tag for the job's location
    region = _find_text(item, "region", ns=_WWR_NS) or "Remote"

    # <description> contains HTML — strip it to plain text
    description = _strip_html(_find_text(item, "description"))

    return Job(
        title=title.strip(),
        company=company.strip(),
        location=region.strip(),
        description=description,
        url=link.strip(),
        source="weworkremotely",
        tags=[],  # WWR RSS does not include structured tags
        remote=True,  # all WWR jobs are remote by platform definition
    )


def _is_relevant(job: Job) -> bool:
    """
    Return True if the job title contains at least one relevant keyword.

    Case-insensitive; all keywords are lowercase.
    """
    title_lower = job.title.lower()
    return any(kw in title_lower for kw in RELEVANT_TITLE_KEYWORDS)


# ---------------------------------------------------------------------------
# Feed parsing
# ---------------------------------------------------------------------------


def _parse_feed(xml_bytes: bytes) -> list[Job]:
    """
    Parse one RSS feed's XML bytes into a list of normalized job dicts.

    ElementTree.fromstring() parses the full XML in memory. WWR feeds contain
    ~100 items — small enough that streaming is not needed.

    Skips any <item> that _normalize() returns None for (malformed entries).
    """
    root = ET.fromstring(xml_bytes)
    items = root.findall(".//item")
    results = []
    for item in items:
        normalized = _normalize(item)
        if normalized is not None:
            results.append(normalized)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def fetch_jobs() -> list[Job]:
    """
    Fetch and return normalized, relevance-filtered jobs from both WWR RSS feeds.

    Flow:
      1. Fetch the Programming and DevOps RSS feeds concurrently.
      2. Parse each feed's XML into normalized job dicts.
      3. Merge both lists.
      4. Drop jobs whose titles don't match any relevant keyword.
      5. Return the combined, filtered list.

    Raises:
      httpx.HTTPStatusError  if WWR returns a non-2xx status on either feed.
      httpx.RequestError     on network-level failures (timeout, DNS, etc.).

    Both exceptions are intentionally not caught here. The orchestrator calls
    this inside asyncio.gather(return_exceptions=True), so a failure contributes
    0 jobs rather than aborting the whole pipeline.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        prog_resp, devops_resp = await asyncio.gather(
            client.get(WWR_RSS_PROGRAMMING_URL),
            client.get(WWR_RSS_DEVOPS_URL),
        )

    prog_resp.raise_for_status()
    devops_resp.raise_for_status()

    prog_jobs = _parse_feed(prog_resp.content)
    devops_jobs = _parse_feed(devops_resp.content)
    all_jobs = prog_jobs + devops_jobs

    relevant = [job for job in all_jobs if _is_relevant(job)]

    logger.info(
        "[weworkremotely] fetched %d total → %d relevant after title filter",
        len(all_jobs),
        len(relevant),
    )
    return relevant
