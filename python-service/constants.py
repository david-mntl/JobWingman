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
    "senior",
    "platform",
    "infrastructure",
    "agent",
    "agentic",
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
]

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Jobs with a match_score below this value are never shown to the user.
# Defined in CLAUDE.md: "Hard discard if match_score < 6"
MIN_MATCH_SCORE = 6.0

# Number of top-scored jobs included in the daily Telegram digest.
TOP_N_JOBS = 3

# ---------------------------------------------------------------------------
# Gemini LLM
# ---------------------------------------------------------------------------

# Free-tier model — fast enough for scoring, zero cost during development.
GEMINI_MODEL = "gemini-2.5-flash"

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

# Separator line used between job cards in the digest message.
TELEGRAM_SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━"

# Parse mode sent to the Telegram Bot API.
# HTML allows <b>, <i>, <a href> tags in messages.
TELEGRAM_PARSE_MODE = "HTML"
