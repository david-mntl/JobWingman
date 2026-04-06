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

import asyncio
import json
import os

import httpx

from constants import (
    GEMINI_API_URL,
    GEMINI_DELAY_BETWEEN_CALLS,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_MAX_RETRIES,
    GEMINI_MODEL,
    GEMINI_RETRY_BASE_DELAY,
    MIN_MATCH_SCORE,
    MIN_SALARY_EUR,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Validate at import time — if the key is missing, the service crashes on
# startup with a clear message instead of silently failing 27 jobs later.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Add it to .env and restart the container."
    )

# Gemini request timeout in seconds. Scoring a single job involves a large
# prompt (CV + description); 60 s gives the model enough headroom.
GEMINI_TIMEOUT_SECONDS = 60

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

    Retries on HTTP 429 (rate limited) with exponential backoff:
      Retry 1 waits 10s, retry 2 waits 20s, retry 3 waits 40s.
    After GEMINI_MAX_RETRIES attempts, the 429 propagates to the caller.

    Why retry only on 429 and not on other errors:
      429 is transient — the quota window resets and the next call succeeds.
      Other errors (400 bad request, 401 auth, 500 upstream) are either
      permanent or indicate a real upstream problem, and retrying would
      waste time and tokens.

    Raises:
      httpx.HTTPStatusError  on non-2xx responses from Gemini (after retries).
      httpx.RequestError     on network failures.
      KeyError / IndexError  if the response structure is unexpected.
    """
    url = GEMINI_API_URL.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,  # Low temperature = consistent, structured output
            "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
        },
    }

    async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT_SECONDS) as client:
        for attempt in range(GEMINI_MAX_RETRIES + 1):
            response = await client.post(url, json=payload)

            if response.status_code != 429:  # Rate limited
                response.raise_for_status()
                print(response.json())
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]

            # 429 — rate limited. Back off and retry unless we're out of attempts.
            if attempt >= GEMINI_MAX_RETRIES:
                response.raise_for_status()  # raises HTTPStatusError with the 429

            wait = GEMINI_RETRY_BASE_DELAY * (2**attempt)
            print(
                f"[scoring] 429 rate-limited — retry {attempt + 1}/"
                f"{GEMINI_MAX_RETRIES} after {wait}s"
            )
            await asyncio.sleep(wait)

    # Unreachable: the loop either returns on success or raises on final 429.
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# JSON extractor
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> dict:
    """
    Parse the model's text output into a Python dict.

    Strategy (in order):
      1. Try json.loads() on the cleaned text.
      2. If that fails, fall back to extracting the substring from the first
         '{' to the last '}' — handles cases where the model added extra
         prose before or after the JSON.

    Raises:
      ValueError  if no valid JSON object can be found in the response.
    """
    text = raw.strip()
    # try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Step fallback: extract from first '{' to last '}'
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No JSON found in Gemini response: {raw[:200]}")


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
    Gemini flash free tier allows 15 requests/minute; a fixed delay of
    GEMINI_DELAY_BETWEEN_CALLS between jobs keeps us comfortably under
    the limit. Retries on 429 are handled inside _call_gemini().

    The delay is applied *between* calls (not after the last one) to avoid
    an unnecessary trailing sleep when the batch is done.

    If any job fails to score (LLM error, network error, parse error), the
    exception is NOT caught here — it propagates to the caller. The LLM is
    the core of the pipeline; if it is broken, silently returning zero
    results is worse than failing loudly with one clear error message.
    """
    results = []
    for i, job in enumerate(jobs):
        if i > 0:
            await asyncio.sleep(GEMINI_DELAY_BETWEEN_CALLS)
        result = await score_job(job, cv)
        if result is not None:
            results.append(result)

    print(f"[scoring] {len(jobs)} in → {len(results)} passed scoring")
    return results
