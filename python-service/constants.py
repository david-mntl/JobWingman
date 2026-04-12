"""
JobWingman — project-wide constants.

Every magic value lives here. No raw strings, numbers, or repeated literals
anywhere else in the codebase. If a value needs to change (API URL, score
threshold, expiry window) it changes in exactly one place.

Why a dedicated module instead of inline constants per file:
  Several modules need the same values (e.g. MIN_MATCH_SCORE is used by
  the scoring module AND the endpoint that filters results before sending).
  A shared module eliminates the risk of the two copies drifting apart.
"""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Default log level used when the LOG_LEVEL environment variable is not set.
# Override at runtime: LOG_LEVEL=DEBUG uvicorn main:app ...
# Valid values (case-insensitive): DEBUG, INFO, WARNING, ERROR, CRITICAL.
LOG_LEVEL_DEFAULT = "DEBUG"

# ---------------------------------------------------------------------------
# Relevance filter keywords
# ---------------------------------------------------------------------------

# Job title must contain at least one of these to pass the relevance pre-filter.
# Applied by every source fetcher before hard-discard and LLM scoring.
# All lowercase; matching is case-insensitive.
RELEVANT_TITLE_KEYWORDS = [
    "ai",
    "llm",
    "backend",
    "back-end",
    "back end",
    "engineer",
    "developer",
    "software",
    "platform",
    "infrastructure",
    "agent",
    "agentic",
    "ki",
]

# ---------------------------------------------------------------------------
# Hard-discard filter keywords
# ---------------------------------------------------------------------------

# If ANY of these appear in the job title or description, the job is
# discarded before the LLM is called — zero tokens wasted.
# All lowercase; matching is case-insensitive.
DISCARD_KEYWORDS = [
    "consultant",
    "outsourcing",
    "staff augmentation",
    "body leasing",
    "on-site only",
    "relocation required",
    "teilzeit",
]

# ---------------------------------------------------------------------------
# Data paths
# ---------------------------------------------------------------------------

# Relative path from the python-service root to the candidate CV file.
# Loaded once at startup and injected into every scoring prompt.
CV_PATH = "data/cv.txt"

# ---------------------------------------------------------------------------
# Eval layer
# ---------------------------------------------------------------------------

# Minimum judge overall_quality score (1–5) required for a fixture to PASS.
# If the judge returns a quality below this AND the fixture's assertion
# status was PASS, the result is downgraded to FAIL. This turns the judge
# from a diagnostic tool into a real quality gate.
# 3 = "score off by >1 point OR one key dimension ignored" on the judge's
# 5-point scale — anything below that signals genuinely poor output.
JUDGE_MIN_QUALITY = 3

# Human-readable version tag for the current scoring prompt. Bump this
# (e.g. "v2.0") whenever _SCORING_PROMPT_TEMPLATE in pipeline/scoring.py
# is changed in a meaningful way. Eval reports are named after this version
# so you can diff results across prompt iterations.
PROMPT_VERSION = "v1.0"

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Jobs with a match_score below this value are never shown to the user.
# Defined in CLAUDE.md: "Hard discard if match_score < 6"
MIN_MATCH_SCORE = 6.0

# Minimum acceptable annual salary in EUR. Jobs that *explicitly* post a
# salary range below this are discarded. Jobs that omit salary are NOT
# discarded — estimates are unreliable and should only be flagged, not used
# for hard filtering.
MIN_SALARY_EUR = 95_000

# Number of top-scored jobs included in the daily Telegram digest.
TOP_N_JOBS = 30

# ---------------------------------------------------------------------------
# Gemini LLM
# ---------------------------------------------------------------------------


# Maximum tokens Gemini may produce for a single scoring response. The
# scoring JSON includes ~15 fields, some with arrays; 1024 tokens was too
# tight and caused truncation (no closing brace → parse failure). 4096
# gives comfortable headroom without hitting free-tier limits.
GEMINI_MAX_OUTPUT_TOKENS = 4096

# Delay in seconds between consecutive Gemini scoring calls. Gemini free
# tier allows 15 requests/minute — a 5-second gap means max 12 req/min,
# staying safely under the limit even with retries.
GEMINI_DELAY_BETWEEN_CALLS = 5

# Number of retry attempts when Gemini returns 429 (rate limited).
GEMINI_MAX_RETRIES = 3

# Base delay in seconds for exponential backoff on 429 responses.
# Retry 1 waits 10s, retry 2 waits 20s, retry 3 waits 40s.
GEMINI_RETRY_BASE_DELAY = 10

# Number of retry attempts when Gemini returns 503 (service unavailable /
# high demand). Kept separate from 429 so both counters are independent.
GEMINI_503_MAX_RETRIES = 5

# Base delay in seconds for exponential backoff on 503 responses.
# Retry 1 waits 3s, retry 2 waits 6s, … retry 5 waits 48s.
# Shorter than the 429 base because 503 spikes tend to clear quickly.
GEMINI_503_RETRY_BASE_DELAY = 3

GEMINI_MODEL = "gemini-3.1-flash-lite-preview"

# Per-request HTTP timeout in seconds. Scoring a single job involves a large
# prompt (CV + description); 60s gives the model enough headroom.
GEMINI_TIMEOUT_SECONDS = 60

# API endpoint template — {model} and {key} filled in at call time.
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models"
    "/{model}:generateContent?key={key}"
)

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

# How many days a seen_jobs record is valid before the same job can
# be surfaced again. Must match SEEN_JOBS_EXPIRY_DAYS in database.py.
DEDUP_EXPIRY_DAYS = 30

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

# Telegram Bot API hard limit — messages longer than this are rejected
# with HTTP 400 "message is too long".
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Separator line used between job cards in the digest message.
TELEGRAM_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━"

# Parse mode sent to the Telegram Bot API.
# HTML allows <b>, <i>, <a href> tags in messages.
TELEGRAM_PARSE_MODE = "HTML"

# ---------------------------------------------------------------------------
# Phase 5 — Bot listener + URL analysis
# ---------------------------------------------------------------------------

# How many days a pending_jobs row is kept before it is automatically pruned
# on startup. After this window the original "Save job" button will no longer
# work (the job data is gone), so 14 days is a reasonable trade-off between
# storage size and button longevity.
PENDING_JOBS_TTL_DAYS = 14

# Maximum characters of clean page text (after HTML stripping) sent to the
# LLM for job extraction. 20000 chars captures any real job description with
# room to spare, while preventing oversized prompts on content-heavy pages.
URL_EXTRACTION_MAX_CHARS = 20000

# Telegram long-poll timeout in seconds passed to the getUpdates API call.
# Telegram holds the HTTP connection open for this long before returning an
# empty response. Reduces API call frequency compared to short-polling.
BOT_POLL_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Source registry (Phase 3)
# ---------------------------------------------------------------------------

# Canonical source identifiers — order matches the aggregation call order in
# the orchestrator and appears in log output. Joblyst and RemoteRocketship are
# listed first because they carry the most relevant pre-filtered data for
# David's profile.
SOURCE_NAMES = [
    "joblyst",
    "remoterocketship",
    "weworkremotely",
    "remoteok",
    "arbeitnow",
]

# ---------------------------------------------------------------------------
# Joblyst (https://www.joblyst.tech)
# ---------------------------------------------------------------------------

# Base URL for the job listing page. Server-side filtering is supported via
# query params — verified: unfiltered returns 2,955 jobs; with the filters
# below it returns ~407 Engineering remote/hybrid roles.
JOBLYST_BASE_URL = "https://www.joblyst.tech/jobs"

# mode=hybrid%2Cremote is a comma-separated value that the server accepts.
# %2C is the URL-encoded comma — both values must be present to include
# hybrid roles (David is open to hybrid in Berlin).
JOBLYST_MODE_FILTER = "hybrid%2Cremote"

# Category filter — "Engineering" (capital E) is the correct server-side value.
# Lowercase "engineering" returns 0 results (case-sensitive).
JOBLYST_CATEGORY_FILTER = "Engineering"

# How many pages to fetch per pipeline run. 50 jobs/page × 2 pages = 100
# pre-filtered jobs — sufficient daily coverage without hammering the server.
JOBLYST_PAGES_TO_FETCH = 2

# ---------------------------------------------------------------------------
# RemoteRocketship (https://www.remoterocketship.com)
# ---------------------------------------------------------------------------

# Base URL for the job listing page.
REMOTEROCKETSHIP_BASE_URL = "https://www.remoterocketship.com"

# Filter query string confirmed to be respected server-side. Using the exact
# URL the user specified — it returns AI Engineer roles in Europe/Worldwide
# with Berlin as city context, hybrid jobs included.
# Verified: unfiltered returns "Sales Representative" jobs; with these params
# it returns "AI Engineer", "Generative AI Architect", "AI Architect".
REMOTEROCKETSHIP_FILTER_PARAMS = (
    "sort=DateAdded"
    "&jobTitle=AI+Engineer"
    "&locations=Europe%2CWorldwide"
    "&showHybridJobs=true"
    "&locationCity=Berlin"
)

# How many pages to fetch per pipeline run. 20 jobs/page × 3 pages = 60
# pre-filtered jobs.
REMOTEROCKETSHIP_PAGES_TO_FETCH = 3


# ---------------------------------------------------------------------------
# WeWorkRemotely (https://weworkremotely.com)
# ---------------------------------------------------------------------------

# RSS feeds for the two most relevant WWR categories. Both are free,
# unauthenticated, and return standard RSS 2.0 XML.
# Programming covers backend, AI, and general software roles.
# DevOps covers infrastructure and platform engineering roles.
WWR_RSS_PROGRAMMING_URL = (
    "https://weworkremotely.com/categories/remote-programming-jobs.rss"
)
WWR_RSS_DEVOPS_URL = (
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"
)

# ---------------------------------------------------------------------------
# RemoteOK (https://remoteok.com)
# ---------------------------------------------------------------------------

# Public JSON API — no authentication required. Returns a JSON array where
# the first element is a metadata object (not a job), followed by job objects.
REMOTEOK_API_URL = "https://remoteok.com/api"

# Index of the first real job in the API response array. Element 0 is
# metadata ({"legal": "..."}); jobs start at index 1.
REMOTEOK_JOBS_OFFSET = 1
