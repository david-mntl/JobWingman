"""
JobWingman — Hard discard filter.

Responsibilities:
- Run a fast, pre-LLM keyword check on each job's title and description.
- Discard jobs that match the hard-discard criteria defined in CLAUDE.md.
- Return only the jobs that survive, ready for deduplication and scoring.

Why this runs before the LLM:
  Every job that reaches the LLM costs tokens. The hard discard filter is
  pure string matching — it costs nothing and eliminates jobs that would
  never pass scoring anyway (outsourcing roles, on-site mandates, relocation
  requirements). This is the "zero wasted tokens" invariant from CLAUDE.md.

Why a dedicated module and not inline in the endpoint:
  The filter logic references DISCARD_KEYWORDS from constants and applies a
  multi-condition rule that would clutter the endpoint. Isolating it here
  keeps each module focused on one responsibility and makes the filter easy
  to unit-test independently.
"""

from constants import DISCARD_KEYWORDS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Phrases that signal a 100% on-site or relocation requirement.
# Checked against the job location and description fields.
# Kept separate from DISCARD_KEYWORDS because the semantic reason differs:
# these are work-arrangement dealbreakers, not company-type signals.
ONSITE_SIGNALS = [
    "on-site only",
    "onsite only",
    "no remote",
    "relocation required",
    "must relocate",
    "in-office only",
    "in office only",
]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _contains_any(text: str, keywords: list[str]) -> bool:
    """
    Return True if the lowercased text contains any keyword from the list.

    Case-insensitive matching — all keyword lists are lowercase by convention.
    """
    text_lower = text.lower()
    return any(kw in text_lower for kw in keywords)


def _is_hard_discard(job: dict) -> tuple[bool, str]:
    """
    Apply the hard-discard rules from CLAUDE.md and return (discard, reason).

    Rules (either condition is sufficient to discard):
      1. Title or description contains a discard keyword (consultant,
         outsourcing, staff augmentation, body leasing, loaned to client).
      2. Location or description signals a 100% on-site or relocation
         requirement.

    Returning the reason alongside the bool makes the caller's log line
    useful for debugging without requiring a second call into this function.
    """
    searchable = f"{job.get('title', '')} {job.get('description', '')}"

    if _contains_any(searchable, DISCARD_KEYWORDS):
        matched = next(kw for kw in DISCARD_KEYWORDS if kw in searchable.lower())
        return True, f"discard keyword: '{matched}'"

    location_and_desc = f"{job.get('location', '')} {job.get('description', '')}"
    if _contains_any(location_and_desc, ONSITE_SIGNALS):
        matched = next(kw for kw in ONSITE_SIGNALS if kw in location_and_desc.lower())
        return True, f"on-site signal: '{matched}'"

    return False, ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_hard_discard(jobs: list[dict]) -> list[dict]:
    """
    Filter out jobs that match any hard-discard rule.

    Logs each discarded job so the pipeline output shows how many were
    dropped and why — useful when tuning the keyword lists.

    Returns the surviving jobs in the same order they were received.
    """
    kept = []
    for job in jobs:
        discard, reason = _is_hard_discard(job)
        if discard:
            print(
                f"[filter] DISCARD — {job.get('title', '?')} @ "
                f"{job.get('company', '?')} | reason: {reason}"
            )
        else:
            kept.append(job)

    print(f"[filter] {len(jobs)} in → {len(kept)} passed hard discard")
    return kept
