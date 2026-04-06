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
TOP_N_JOBS = 5

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
