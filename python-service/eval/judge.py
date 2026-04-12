"""
JobWingman — LLM-as-judge module.

Given a fixture's job data, its expected behaviour, and the scoring result
produced by the current prompt, a second LLM call reviews the scoring
quality and returns a structured verdict.

Why a second LLM call instead of hard-coded assertions:
  Hard assertions can check whether the score is in the expected range and
  whether specific keywords appear in flags — but they cannot reason about
  *why* the score is wrong or whether the overall scoring logic was sound.
  The judge prompt gives the model the full CV and asks it to assess
  specific calibration dimensions: AI priority, office penalty, ML research
  penalty, benefits boost, and output conciseness. This produces richer
  diagnostics than numeric checks alone and demonstrates a real-world
  evaluation technique used in production LLM systems (LLM-as-judge / RAGAS).

Why the judge receives the full CV:
  Scoring quality can only be assessed with knowledge of the candidate.
  "Is a 7.5 score appropriate for a backend-only role?" depends entirely
  on what David's target is. Giving the judge the same CV the scorer sees
  means it can make the same contextual judgements a human reviewer would.

Output schema:
  {
    "overall_quality": int (1–5),
    "score_in_expected_range": bool,
    "ai_priority_correct": bool,
    "office_penalty_applied": bool | null,
    "ml_research_penalty_applied": bool | null,
    "benefits_boost_applied": bool | null,
    "location_rule_correct": bool,
    "output_concise": bool,
    "issues": [str, ...],
    "verdict": str
  }
"""

import json

from llm import LLMClient
from pipeline.scoring import extract_json

# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

_JUDGE_PROMPT_TEMPLATE = """\
You are an expert calibration reviewer for a job-scoring AI system.
Your task is to assess whether a scoring result was accurate for a specific
engineering candidate. You have access to the candidate's full CV so you can
make the same contextual judgements a human recruiter would.

## Candidate's Full CV
{cv}

## Job Being Evaluated
Title:    {title}
Company:  {company}
Location: {location}
Remote:   {remote}
Tags:     {tags}
Description:
{description}

## Scoring Result to Review
{scoring_result}

## Expected Behaviour for This Fixture
{expected_criteria}

---

Review the scoring result against the candidate profile and the expected
behaviour. Return ONLY a JSON object — no prose, no markdown fences, nothing
outside the braces:

{{
  "overall_quality": <int 1–5>,
  "score_in_expected_range": <bool>,
  "ai_priority_correct": <bool — was AI/LLM focus correctly weighted as top priority?>,
  "office_penalty_applied": <bool | null — null if this fixture has no office-days constraint>,
  "ml_research_penalty_applied": <bool | null — null if this fixture is not a pure ML research role>,
  "benefits_boost_applied": <bool | null — null if this fixture has no notable benefits package>,
  "location_rule_correct": <bool — was the location handled correctly (EU/remote ok, US-only = discard, ambiguous = include)?>,
  "output_concise": <bool — true if fields are specific and grounded in the job, false if they contain generic filler or padding>,
  "issues": ["<specific problem found in the scoring>", ...],
  "verdict": "<one-sentence summary: biggest calibration issue, or confirmation that the scoring was accurate>"
}}

## Quality Scale
5 — Score in expected range, all flags specific and grounded in the job description,
    output is tight, every dimension handled correctly.
4 — Score in expected range, one minor flag issue (generic phrasing, minor omission).
3 — Score off by more than 1.0 point from the expected midpoint, OR one key dimension
    was ignored (e.g., office penalty should have fired but didn't).
2 — Score off by more than 2.0 points, OR multiple dimensions handled incorrectly.
1 — Fundamentally wrong result: a job that should have been discarded was shown, a
    near-perfect match was discarded, or the score is wildly out of range (off by > 3).

## Dimension Guidance
- AI priority: Roles building or integrating AI/LLM/agentic systems should score
  higher than equivalent non-AI backend roles, all else equal.
- Office penalty: 2 days/week in Berlin = acceptable (no score penalty). 3+ days =
  clear penalty into 6.0–7.0 range. 5 days = should score below 6.0.
- ML research vs ML infra: PhD-level model training / pure research = discard.
  ML infrastructure / model serving / MLOps = acceptable engineering role.
- Benefits boost: 4-day week, health insurance, learning budget, equipment budget,
  ESOP — each is a genuine positive and should be reflected in the score.
- Location: No location stated = do not penalise. US-only + no remote = discard.
  Remote EU/worldwide = ideal.
- Output conciseness: red_flags and green_flags should name specific things from the
  job description. Phrases like "Fast-paced environment" or "Great team culture"
  that appear in every generic job ad do not count as specific flags.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_expected_criteria_text(expected: dict) -> str:
    """
    Convert the fixture's expected dict into a human-readable description
    for the judge prompt. This tells the judge what the correct outcome
    should have been so it can assess whether scoring diverged from it.
    """
    lines = [f"Action: {expected.get('action', 'score')}"]

    action = expected.get("action", "score")
    if action == "hard_discard":
        lines.append(
            "The job should have been caught by the pre-LLM keyword filter and never scored."
        )
    elif action == "score_discard":
        score_max = expected.get("score_max")
        lines.append(
            f"The job should have received a match_score below {score_max if score_max is not None else 6.0} "
            f"and been discarded."
        )
    else:
        score_min = expected.get("score_min")
        score_max = expected.get("score_max")
        if score_min is not None and score_max is not None:
            lines.append(f"Expected score range: {score_min}–{score_max}")
        if expected.get("must_have_green_flag_containing"):
            lines.append(
                f"Must have a green flag mentioning: '{expected['must_have_green_flag_containing']}'"
            )
        if expected.get("ai_priority_high") is True:
            lines.append(
                "AI/LLM focus should be explicitly recognised as the primary strength."
            )
        if expected.get("ai_priority_high") is False:
            lines.append(
                "No AI focus — score should reflect a good-but-not-ideal backend match."
            )

    if expected.get("notes"):
        lines.append(f"Fixture notes: {expected['notes']}")

    # When the fixture specifies which judge dimensions matter, instruct the
    # judge to focus only on those and return null for the rest. This reduces
    # noise from irrelevant dimensions and focuses the LLM's attention.
    judge_dims = expected.get("judge_dimensions")
    if judge_dims:
        dims_list = ", ".join(judge_dims)
        lines.append(
            f"IMPORTANT — Focus dimensions: Only evaluate these dimensions: "
            f"[{dims_list}]. For all other dimensions in the output JSON, "
            f"return null. Your overall_quality score should be based ONLY on "
            f"the listed dimensions."
        )

    return "\n".join(f"  {line}" for line in lines)


async def judge_scoring(
    fixture: dict,
    scoring_result: dict,
    llm_client: LLMClient,
    cv: str,
) -> dict:
    """
    Call the LLM judge to review a scoring result against the fixture's expected
    behaviour. Returns the parsed judge verdict dict.

    Args:
        fixture:        The full fixture entry from jobs.json (includes job + expected).
        scoring_result: The dict returned by score_job() (job.scoring), or an empty
                        dict if the job was discarded before the LLM was called.
        llm_client:     LLM client to use for the judge call.
        cv:             The candidate's full CV text (same text the scorer received).

    Returns:
        A dict matching the judge output schema (see module docstring).
        On parse failure, returns a minimal error dict.
    """
    job = fixture["job"]
    expected_text = _build_expected_criteria_text(fixture.get("expected", {}))

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        cv=cv,
        title=job.get("title", "Unknown"),
        company=job.get("company", "Unknown"),
        location=job.get("location", "Not stated"),
        remote="Yes" if job.get("remote") else "No / not specified",
        tags=", ".join(job.get("tags", [])) or "none",
        description=job.get("description", ""),
        scoring_result=json.dumps(scoring_result, indent=2, ensure_ascii=False),
        expected_criteria=expected_text,
    )

    raw = await llm_client.generate(prompt)

    try:
        return extract_json(raw)
    except ValueError:
        return {
            "overall_quality": 0,
            "score_in_expected_range": False,
            "ai_priority_correct": False,
            "office_penalty_applied": None,
            "ml_research_penalty_applied": None,
            "benefits_boost_applied": None,
            "location_rule_correct": False,
            "output_concise": False,
            "issues": [f"Judge LLM returned unparseable response: {raw[:200]}"],
            "verdict": "Judge parse error — check raw LLM output.",
        }
