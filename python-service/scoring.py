"""
JobWingman — LLM scoring module.

Responsibilities:
- Build the Gemini prompt by combining the user's CV with each job's details.
- Call the Gemini API and parse the structured JSON scoring response.
- Discard jobs whose match_score falls below MIN_MATCH_SCORE.
- Return the surviving jobs, each enriched with their scoring data.

Why the CV is passed in rather than imported:
  cv_text is loaded once at startup in main.py and kept in module-level
  state there. Importing it from main.py would create a circular dependency
  (main → scoring → main). Accepting it as a parameter keeps this module
  stateless and independently testable.

Why JSON is extracted with a regex fallback:
  Gemini sometimes wraps its JSON output in a markdown code block
  (```json ... ```). The extractor strips that wrapper before parsing so
  the response is valid regardless of whether the model adds the fence.
"""

import json
import os
import re

import httpx

from constants import GEMINI_API_URL, GEMINI_MODEL, MIN_MATCH_SCORE, MIN_SALARY_EUR

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Gemini request timeout in seconds. Scoring a single job involves a large
# prompt (CV + description); 60 s gives the model enough headroom.
GEMINI_TIMEOUT_SECONDS = 60

# Regex that matches an optional ```json fence around the model's output.
# Group 1 captures the raw JSON whether or not the fence is present.
_JSON_FENCE_REGEX = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```|(\{[\s\S]*\})")

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

_SCORING_PROMPT_TEMPLATE = """\
You are a senior engineering recruiter evaluating job postings on behalf of David.
Your job is to score how well this role fits David and return structured JSON.

## David's CV
{cv}

## David's Priorities (in order of importance)
1. AI / LLM / Agentic systems focus — this is the #1 priority. Roles building or
   integrating AI agents, LLM pipelines, or orchestration systems score highest.
2. Remote-friendly — 100% remote is ideal. Hybrid in Berlin with max 2 days/week
   in office is acceptable (neutral, no boost). More than 2 days/week on-site or
   relocation required → strong negative.
3. Stack flexibility — David's primary production language is C#/.NET, but his
   skills are highly transferable. He has hands-on Python (FastAPI, scripting, LLM
   APIs), Java, TypeScript. A job asking for "senior Python" is viable because his
   distributed systems, microservices, and AI experience transfer directly. Do NOT
   penalize heavily for language mismatch if the underlying engineering skills align.
   Only penalize if the role requires deep domain expertise he lacks entirely
   (e.g., PhD-level ML research, Kubernetes platform engineering, mobile dev).
4. Own product over consulting — companies building their own product score higher
   than outsourcing/consulting/staff-augmentation shops.
5. Culture signals — 4-day week, learning budget, equity/ESOP, low-ego culture,
   worker wellbeing are all strong positives.

## David's Hard Constraints
- Location: Berlin, Germany. Open to full remote EU or worldwide.
- Languages: English (fluent), German (C1 working level), Spanish (native).
  German-required roles are fine — do NOT penalize for German requirements.
- Employment: Permanent positions only. Discard freelance/contract roles.
- Salary: If the job *explicitly states* a salary range below €{min_salary}/year,
  discard it (set match_score to 0). If no salary is posted, do NOT discard —
  estimate from company size, location, and role seniority, and flag with ⚠️.
  Anchor estimates to Berlin/EU senior backend/AI engineer market rates (€75k–130k).
- Seniority: Junior roles that explicitly state a salary below €{min_salary_k}k are discarded.
  Junior/mid roles with no stated salary or salary above €95k can still be shown.

## Job to Evaluate
Title:       {title}
Company:     {company}
Location:    {location}
Remote:      {remote}
Tags:        {tags}
Description:
{description}

## Scoring Instructions
Evaluate this job for David and return ONLY a JSON object — no prose, no markdown
outside the JSON, no explanation. Use exactly this structure:

{{
  "match_score": <float 0.0–10.0>,
  "salary_signal": "<string: stated range or estimate with reasoning>",
  "red_flags": ["<string>", ...],
  "green_flags": ["<string>", ...],
  "fit_breakdown": {{
    "strong": ["<skill or experience match>", ...],
    "gaps":   ["<skill or experience gap>", ...]
  }},
  "company_snapshot": "<3-sentence company description>",
  "role_summary": ["<bullet>", "<bullet>", "<bullet>"],
  "company_benefits": ["<benefit>", ...],
  "confidence": "<high | medium | low>",
  "verdict": "<1-sentence honest recommendation>"
}}

## Confidence field
- "high": Job description is detailed, clear requirements and company info.
- "medium": Some info missing but enough to score meaningfully.
- "low": Very thin description, vague requirements — score is a rough guess.
  Flag this explicitly in the verdict.

## Green Flags (boost score, always mention explicitly)
- 4-day work week — always flag with ⭐ explicitly
- 100% remote or remote + 1 month abroad policy
- Learning budget
- Own product (not outsourcing/consulting)
- Agent / LLM / AI focus — building or integrating AI systems
- Low-ego culture, worker wellbeing, equity/ESOP
- Tech stack overlap with David's experience

## Red Flags (lower score, flag — do not auto-discard unless stated)
- No salary range posted → estimate from context, flag with ⚠️
- Pure ML/data science with no engineering component (PhD-heavy, research-only)
- Vague remote ("remote-friendly" without clear policy)
- "Fast-paced startup" filler language with no substance
- Subtle consulting signals: "work with our clients", "project-based engagements",
  "customer-facing consulting", "placed at client sites" — these indicate
  outsourcing/body-shop even without the explicit keywords. Flag and lower score.
- Freelance or contract position — hard discard (match_score = 0)

## match_score Rubric
  9–10  Exceptional — AI/LLM/agent focus, remote, strong culture signals,
        good stack overlap. Near-perfect for David's career trajectory.
  8–8.9 Strong — clear AI or backend alignment, remote or hybrid Berlin,
        minor gaps (e.g., language mismatch but transferable skills).
  7–7.9 Good — solid engineering role, some AI relevance or strong backend
        fit. Worth reviewing, one or two notable gaps.
  6–6.9 Borderline — viable but significant trade-offs. Maybe no AI focus
        but strong backend match, or AI focus but concerning signals.
        Only surface if the positives are concrete, not speculative.
  < 6   Poor fit — do not surface. Missing AI alignment, wrong seniority,
        consulting/outsourcing, on-site required, or explicit salary below floor.
"""


def _build_prompt(job: dict, cv: str) -> str:
    return _SCORING_PROMPT_TEMPLATE.format(
        cv=cv,
        min_salary=MIN_SALARY_EUR,
        min_salary_k=MIN_SALARY_EUR // 1000,
        title=job.get("title", ""),
        company=job.get("company", ""),
        location=job.get("location", ""),
        remote="Yes" if job.get("remote") else "Not specified",
        tags=", ".join(job.get("tags", [])) or "none",
        description=job.get("description", ""),
    )


# ---------------------------------------------------------------------------
# Gemini API call
# ---------------------------------------------------------------------------


async def _call_gemini(prompt: str) -> str:
    """
    Send the prompt to Gemini and return the raw text response.

    Raises:
      httpx.HTTPStatusError  on non-2xx responses from Gemini.
      httpx.RequestError     on network failures.
      KeyError / IndexError  if the response structure is unexpected.
    """
    api_key = os.environ["GEMINI_API_KEY"]
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=api_key)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,  # Low temperature = consistent, structured output
            "maxOutputTokens": 1024,
        },
    }

    async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()

    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# JSON extractor
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    """
    Parse the model's text output into a Python dict.

    Gemini sometimes wraps JSON in a ```json fence even when told not to.
    The regex strips the fence if present; otherwise it matches the bare
    JSON object directly.

    Raises:
      ValueError  if no valid JSON object can be found in the response.
    """
    match = _JSON_FENCE_REGEX.search(raw)
    if not match:
        raise ValueError(f"No JSON found in Gemini response: {raw[:200]}")

    json_str = match.group(1) or match.group(2)
    return json.loads(json_str)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def score_job(job: dict, cv: str) -> dict | None:
    """
    Score a single job and return the enriched job dict, or None if discarded.

    Attaches the full scoring result to the job dict under the key "scoring".
    Returns None if match_score < MIN_MATCH_SCORE — the caller should
    filter out None values from the results list.

    Raises:
      httpx.HTTPStatusError / httpx.RequestError  on Gemini API failures.
      ValueError                                  if the response is unparseable.
    """
    prompt = _build_prompt(job, cv)
    raw = await _call_gemini(prompt)
    scoring = _extract_json(raw)

    match_score = float(scoring.get("match_score", 0))
    job_label = f"{job.get('title', '?')} @ {job.get('company', '?')}"

    if match_score < MIN_MATCH_SCORE:
        print(f"[scoring] DISCARD — {job_label} | score: {match_score}")
        return None

    print(f"[scoring] PASS — {job_label} | score: {match_score}")
    return {**job, "scoring": scoring}


async def score_jobs(jobs: list[dict], cv: str) -> list[dict]:
    """
    Score a list of jobs sequentially and return only those that pass.

    Sequential (not concurrent) to respect Gemini free-tier rate limits.
    Gemini flash free tier allows 15 requests/minute — running jobs one at
    a time keeps us well within that limit for typical batch sizes (10–30
    jobs after filtering).

    None values from score_job (discarded jobs) are filtered out before
    returning.
    """
    results = []
    for job in jobs:
        try:
            result = await score_job(job, cv)
            if result is not None:
                results.append(result)
        except Exception as e:
            # A single job failing to score should not abort the whole batch.
            # Log and continue so the remaining jobs are still processed.
            print(
                f"[scoring] ERROR — {job.get('title', '?')} @ "
                f"{job.get('company', '?')} | {type(e).__name__}: {e}"
            )

    print(f"[scoring] {len(jobs)} in → {len(results)} passed scoring")
    return results
