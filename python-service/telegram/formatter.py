"""
JobWingman — Telegram digest formatter.

Responsibilities:
- Take a list of scored jobs and pipeline stats, produce a single Telegram
  message string ready to be sent via the Bot API.
- Match the exact output format defined in CLAUDE.md: header, job cards
  with emoji flags, stats footer.

Why a dedicated module and not inline in main.py:
  The formatting logic is non-trivial (conditional emoji flags, score
  formatting, multi-line card layout). Keeping it separate makes main.py
  focused on routing and keeps the formatter independently testable — you
  can call format_digest() in a test with mock data and verify the output
  without spinning up FastAPI.

Why plain text with HTML parse mode:
  Telegram's HTML mode supports <b>, <i>, <a href>, but NOT full HTML.
  Markdown mode is fragile with special characters in job titles and
  company names. HTML mode gives us bold/italic/links with predictable
  escaping.
"""

from constants import TELEGRAM_MAX_MESSAGE_LENGTH, TELEGRAM_SEPARATOR
from models.job import Job

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Emoji indicators used in job cards.
EMOJI_STAR = "⭐"
EMOJI_REMOTE = "🏠"
EMOJI_SCORE = "📈"
EMOJI_CONFIDENCE = "🎯"
EMOJI_WARNING = "⚠️"
EMOJI_GREEN = "🟢"
EMOJI_RED = "🔴"
EMOJI_ROLE = "📝"
EMOJI_COMPANY = "🏢"
EMOJI_FIT_STRONG = "✅"
EMOJI_FIT_GAPS = "⚡"
EMOJI_BENEFITS = "🎁"
EMOJI_VERDICT = "💬"
EMOJI_STATS = "📊"
EMOJI_ROBOT = "🤖"
EMOJI_LINK = "🔗"

# Green flags that deserve a dedicated star callout in the card.
STAR_FLAGS = ["4-day week", "4 day week", "four-day week", "32 hours"]

# Message shown when the pipeline finds zero jobs worth sending.
NO_JOBS_MESSAGE = (
    f"{EMOJI_ROBOT} Good morning David — no new jobs worth your attention today.\n\n"
    "The pipeline ran but nothing scored above the threshold. "
    "I'll try again tomorrow."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_star_flag(green_flags: list[str]) -> bool:
    """Check if any green flag deserves a star callout (e.g. 4-day week)."""
    joined = " ".join(green_flags).lower()
    return any(flag in joined for flag in STAR_FLAGS)


def _format_card(index: int, job: Job) -> str:
    """
    Format a single job into a Telegram card string.

    Each card follows the layout from CLAUDE.md:
      1. [Role] — [Company] ([Location])
      ⭐ 4-day week  🏠 Full remote  📈 X.X/10 match  🎯 confidence
      ⚠️ [salary signal]
      🟢 [top green flag]
      🔴 [top red flag if any]
      📝 Role: bullet summary
      🏢 Company snapshot
      ✅ Strong: ... | ⚡ Gaps: ...
      🎁 Benefits
      💬 Verdict
      🔗 Link

    Only non-empty fields are included — a job with no red flags skips
    the red flag line entirely instead of showing an empty one.
    """
    scoring = job.scoring or {}
    title = job.title or "Unknown Role"
    company = job.company or "Unknown Company"
    location = job.location or "Unknown"
    url = job.url
    match_score = scoring.get("match_score", 0)
    salary_signal = scoring.get("salary_signal", "")
    green_flags = scoring.get("green_flags", [])
    red_flags = scoring.get("red_flags", [])
    confidence = scoring.get("confidence", "")
    role_summary = scoring.get("role_summary", [])
    company_snapshot = scoring.get("company_snapshot", "")
    fit_breakdown = scoring.get("fit_breakdown", {})
    company_benefits = scoring.get("company_benefits", [])
    verdict = scoring.get("verdict", "")

    lines = []

    # Header line: number, role, company, location
    lines.append(f"<b>{index}. {title}</b> — {company} ({location})")

    # Indicators line: star flag, remote status, score, confidence
    indicators = []
    if _has_star_flag(green_flags):
        indicators.append(f"{EMOJI_STAR} 4-day week")
    if job.remote:
        indicators.append(f"{EMOJI_REMOTE} Full remote")
    indicators.append(f"{EMOJI_SCORE} {match_score}/10 match")
    if confidence:
        indicators.append(f"{EMOJI_CONFIDENCE} {confidence}")
    lines.append("  ".join(indicators))

    # Salary signal
    if salary_signal:
        lines.append(f"{EMOJI_WARNING} {salary_signal}")

    # Top green flag
    if green_flags:
        lines.append(f"{EMOJI_GREEN} {green_flags[0]}")

    # Top red flag
    if red_flags:
        lines.append(f"{EMOJI_RED} {red_flags[0]}")

    # Role summary — bullets joined on one line for compact display
    if role_summary:
        lines.append(f"\n{EMOJI_ROLE} <b>Role:</b> {' · '.join(role_summary)}")

    # Company snapshot
    if company_snapshot:
        lines.append(f"{EMOJI_COMPANY} {company_snapshot}")

    # Fit breakdown — strong matches and gaps on one line
    strong = fit_breakdown.get("strong", [])
    gaps = fit_breakdown.get("gaps", [])
    if strong or gaps:
        parts = []
        if strong:
            parts.append(f"{EMOJI_FIT_STRONG} Strong: {', '.join(strong)}")
        if gaps:
            parts.append(f"{EMOJI_FIT_GAPS} Gaps: {', '.join(gaps)}")
        lines.append(" | ".join(parts))

    # Company benefits
    if company_benefits:
        lines.append(f"{EMOJI_BENEFITS} {', '.join(company_benefits)}")

    # Verdict
    if verdict:
        lines.append(f"\n{EMOJI_VERDICT} <i>{verdict}</i>")

    # Link
    if url:
        lines.append(f'{EMOJI_LINK} <a href="{url}">View posting</a>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_digest(jobs: list[Job], stats: dict) -> list[str]:
    """
    Format the Telegram digest as a list of message strings.

    Returns a list instead of a single string because the Telegram Bot API
    rejects messages longer than 4096 characters. Each message in the
    returned list is guaranteed to be within that limit.

    Splitting strategy:
      - The header goes into the first message.
      - Job cards are appended one at a time. When adding the next card
        would exceed the limit, a new message is started.
      - The stats footer is appended to the last message (or becomes its
        own message if it doesn't fit).

    If the jobs list is empty, returns a single "nothing today" message.
    """
    if not jobs:
        return [NO_JOBS_MESSAGE]

    count = len(jobs)
    header = f"{EMOJI_ROBOT} Good morning David — {count} new job{'s' if count != 1 else ''} worth your attention\n"

    # Stats footer — built once, appended at the end
    fetched = stats.get("fetched", 0)
    after_filter = stats.get("after_filter", 0)
    delivered = stats.get("delivered", 0)
    footer = f"\n{EMOJI_STATS} Today: {fetched} scanned → {after_filter} passed → {delivered} worth your time"

    # Build individual card blocks (separator + card text)
    card_blocks = []
    for i, job in enumerate(jobs, start=1):
        block = f"{TELEGRAM_SEPARATOR}\n{_format_card(i, job)}"
        card_blocks.append(block)

    # Pack cards into messages that fit within the Telegram limit
    messages: list[str] = []
    current = header

    for block in card_blocks:
        # +1 for the newline that joins current and block
        if len(current) + 1 + len(block) > TELEGRAM_MAX_MESSAGE_LENGTH:
            messages.append(current)
            current = block
        else:
            current = current + "\n" + block

    # Append closing separator + footer to the last chunk
    closing = f"\n{TELEGRAM_SEPARATOR}{footer}"
    if len(current) + len(closing) > TELEGRAM_MAX_MESSAGE_LENGTH:
        messages.append(current)
        messages.append(f"{TELEGRAM_SEPARATOR}{footer}")
    else:
        current += closing
        messages.append(current)

    return messages
