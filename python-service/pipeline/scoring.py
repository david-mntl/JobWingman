"""
JobWingman — LLM scoring module.

Responsibilities:
- Build the prompt by combining the user's CV with each job's details.
- Call the LLM via the injected client and parse the structured JSON response.
- Discard jobs whose match_score falls below MIN_MATCH_SCORE.
- Return the surviving jobs, each enriched with their scoring data.

Why the CV is passed in rather than imported:
  cv_text is loaded once at startup in main.py and kept in module-level
  state there. Importing it from main.py would create a circular dependency
  (main → scoring → main). Accepting it as a parameter keeps this module
  stateless and independently testable.

Why the LLM client is injected rather than imported:
  scoring.py knows *what* to ask the model, not *which* model to use.
  Receiving an LLMClient instance keeps the business logic decoupled from
  any specific provider — swapping Gemini for Claude requires no changes
  here. It also makes unit testing straightforward: pass a stub client.

Why JSON is extracted with a regex fallback:
  Some models wrap their JSON output in a markdown code block
  (```json ... ```). The extractor strips that wrapper before parsing so
  the response is valid regardless of whether the model adds the fence.
"""

import asyncio
import json

from constants import MIN_MATCH_SCORE, MIN_SALARY_EUR
from logger import get_logger
from llm import LLMClient
from models.job import Job

logger = get_logger(__name__)

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
   Exception — ML research vs ML infrastructure:
   Pure ML research (PhD required, pre-training experiments, publishing in ML
   venues, no engineering/product component) → match_score < 5.0 (David lacks
   PhD-level ML theory).
   ML infrastructure / MLOps (model serving, training pipelines, experiment
   tracking, production ML systems) → treat as a viable engineering role,
   score on other merits. MLOps/infra does NOT qualify for the AI priority
   boost unless the role specifically involves building LLM/agent systems.
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
- Location restriction: If the job explicitly requires US residency, US work
  authorisation, or is physically located in the US/Canada/Asia with no remote
  option → set match_score to 0 (hard discard). If location is ambiguous or
  not stated → do NOT penalise, score based on other factors.

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
  "salary_signal": "<max 15 words. Stated → just state it (e.g. '€95k–115k'). Not stated → 'Not stated — est. €XX–XXk'>",
  "red_flags": ["<max 8 words each, specific to THIS job — no generic filler>", ...],
  "green_flags": ["<max 8 words each, specific to THIS job — no generic filler>", ...],
  "fit_breakdown": {{
    "strong": ["<max 8 words each>", ...],
    "gaps":   ["<max 8 words each>", ...]
  }},
  "company_snapshot": "<max 2 sentences: company name + what they do + one notable fact>",
  "role_summary": ["<max 10 words each>", "<max 10 words each>", "<max 10 words each>"],
  "company_benefits": ["<max 5 words each, e.g. '4-day week', '€5k learning budget'>", ...],
  "confidence": "<high | medium | low>",
  "verdict": "<max 12 words. One short decisive sentence.>"
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
  outsourcing/body-shop even without the explicit keywords. Flag and lower score
  by ~1.0 point. IMPORTANT: NEVER score a job below 6.0 solely because of subtle
  consulting signals — the minimum score for a viable role with consulting red
  flags is 6.0.
- Freelance or contract position — hard discard (match_score = 0)

## Office Presence Penalty (when explicitly stated in the description)
- 100% remote or not specified     → no penalty
- Hybrid ≤ 2 days/week Berlin      → no penalty, but cap score at 9.0
- Hybrid 3 days/week               → cap score at 7.0 regardless of other factors
- Hybrid 4 days/week               → cap score at 6.0
- On-site 5 days / full office     → cap score at 5.0 (will be discarded)

## Benefits Scoring Boost
Each of these, when explicitly mentioned in the posting, adds ~0.5 to the base score:
- 4-day work week (always flag with ⭐)
- Private health insurance / full premium covered
- Public transport card (e.g. Deutschlandticket)
- Home office / equipment budget (≥ €1,000/year)
- Learning / conference budget (≥ €1,000/year)
- Equity / ESOP
Multiple benefits stack. A role with 4-day week + learning budget + ESOP + transport
card could gain up to +2.0 over a comparable role with no stated benefits.

## match_score Rubric
IMPORTANT: A role with clear AI/LLM/agent focus should ALWAYS score at
least 1.0 point higher than an equivalent role without AI focus, all
else being equal. AI relevance is the single most important factor.

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


def _build_prompt(job: Job, cv: str) -> str:
    return _SCORING_PROMPT_TEMPLATE.format(
        cv=cv,
        min_salary=MIN_SALARY_EUR,
        min_salary_k=MIN_SALARY_EUR // 1000,
        title=job.title,
        company=job.company,
        location=job.location,
        remote="Yes" if job.remote else "Not specified",
        tags=", ".join(job.tags) or "none",
        description=job.description,
    )


# ---------------------------------------------------------------------------
# JSON extractor
# ---------------------------------------------------------------------------


def extract_json(raw: str) -> dict:
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
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: extract from first '{' to last '}'
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"No JSON found in model response: {raw[:200]}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def score_job(job: Job, cv: str, llm_client: LLMClient) -> Job | None:
    """
    Score a single job and return the enriched Job, or None if discarded.

    Attaches the full LLM scoring result to job.scoring and returns the same
    Job instance. Returns None if match_score < MIN_MATCH_SCORE — the caller
    should filter out None values from the results list.

    Args:
        job:        Normalised Job instance from the source fetcher.
        cv:         The user's full CV text, injected into the prompt.
        llm_client: Provider-agnostic LLM client used to call the model.

    Raises:
      httpx.HTTPStatusError / httpx.RequestError  on LLM API failures.
      ValueError                                  if the response is unparseable.
    """
    prompt = _build_prompt(job, cv)
    raw = await llm_client.generate(prompt)
    scoring = extract_json(raw)

    match_score = float(scoring.get("match_score", 0))
    job_label = f"{job.title} @ {job.company}"

    if match_score < MIN_MATCH_SCORE:
        logger.debug("[scoring] DISCARD — %s | score: %.1f", job_label, match_score)
        return None

    job.scoring = scoring
    logger.debug("[scoring] PASS — %s | score: %.1f", job_label, match_score)
    return job


async def score_jobs(jobs: list[Job], cv: str, llm_client: LLMClient) -> list[Job]:
    """
    Score a list of jobs sequentially and return only those that pass.

    Sequential (not concurrent) to respect the LLM client's rate limit.
    The client advertises its required inter-request delay via the
    delay_between_calls property; score_jobs honours it without needing
    to know which provider is in use.

    The delay is applied *between* calls (not after the last one) to avoid
    an unnecessary trailing sleep when the batch is done.

    If any job fails to score (LLM error, network error, parse error), the
    exception is NOT caught here — it propagates to the caller. The LLM is
    the core of the pipeline; silently returning zero results is worse than
    failing loudly with one clear error message.

    Args:
        jobs:       List of normalised job dicts to score.
        cv:         The user's full CV text, injected into each prompt.
        llm_client: Provider-agnostic LLM client used to call the model.
    """
    results = []
    for i, job in enumerate(jobs):
        if i > 0:
            await asyncio.sleep(llm_client.delay_between_calls)
        result = await score_job(job, cv, llm_client)
        if result is not None:
            results.append(result)

    logger.info("[scoring] %d in → %d passed scoring", len(jobs), len(results))
    return results
