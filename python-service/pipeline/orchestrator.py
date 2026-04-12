"""
JobWingman — Pipeline orchestrator.

Responsibilities:
- Fetch jobs from all configured sources concurrently.
- Run every pipeline stage (dedup → hard discard → LLM scoring → sort → top N)
  in the correct order.
- Return a PipelineResult with the top scored jobs and full funnel statistics.

Why asyncio.gather with return_exceptions=True:
  Multiple job sources are fetched concurrently. If one source is down
  (RemoteOK rate-limits, RemoteRocketship returns 403) the rest of the run
  must not be aborted. return_exceptions=True means exceptions from individual
  sources are returned as values rather than re-raised, so the orchestrator can
  log the failure and continue with whatever sources did respond.

Why the dedup and filter stages run after aggregation (not per-source):
  Cross-source deduplication requires seeing jobs from all sources together.
  If Joblyst and RemoteOK both list the same role, the first one processed
  gets marked as seen and the second is dropped — regardless of source order.
"""

import asyncio
from dataclasses import dataclass, field

from constants import SOURCE_NAMES, TOP_N_JOBS
from logger import get_logger
from models.job import Job
from job_sources.arbeitnow import fetch_jobs as fetch_arbeitnow
from job_sources.joblyst import fetch_jobs as fetch_joblyst
from job_sources.remoteok import fetch_jobs as fetch_remoteok
from job_sources.remoterocketship import fetch_jobs as fetch_remoterocketship
from job_sources.weworkremotely import fetch_jobs as fetch_wwr
from llm import LLMClient
from pipeline.filters import apply_hard_discard
from pipeline.scoring import score_jobs
from storage.database import is_seen, make_hash, mark_seen

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

# The fetcher functions listed here must be in the same order as SOURCE_NAMES
# in constants.py so that log output and exception messages match by index.
_FETCHERS = [
    fetch_joblyst,
    fetch_remoterocketship,
    fetch_wwr,
    fetch_remoteok,
    fetch_arbeitnow,
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class PipelineResult:
    """
    The output of a complete pipeline run.

    jobs:  Top-N scored jobs, sorted by match_score descending.
    stats: Funnel metrics — how many jobs entered and exited each stage.
           Included in every API response so the caller (main.py) can return
           them without knowing anything about the pipeline internals.
    """

    jobs: list[Job] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_pipeline(cv_text: str, llm_client: LLMClient) -> PipelineResult:
    """
    Execute the full job discovery pipeline and return scored results.

    Pipeline stages (in order):
      1. Fetch  — all sources concurrently; source failures are logged and
                  skipped, not propagated.
      2. Dedup  — MD5(title|company) hash checked against seen_jobs; new jobs
                  are marked as seen immediately so concurrent runs cannot
                  double-process the same job.
      3. Filter — hard-discard rules (no LLM cost for obvious rejects).
      4. Score  — LLM evaluation; jobs below MIN_MATCH_SCORE are dropped.
      5. Sort   — by match_score descending.
      6. Top N  — keep only TOP_N_JOBS for the digest.

    Args:
        cv_text:    The user's full CV loaded at startup; injected into every
                    scoring prompt.
        llm_client: Provider-agnostic LLM client (Gemini, Claude, etc.).

    Returns:
        PipelineResult with the top jobs and funnel statistics.
    """
    # --- Stage 1: Fetch all sources concurrently ---
    logger.debug("Stage 1 — fetching from %d sources concurrently", len(_FETCHERS))
    raw_results = await asyncio.gather(
        *[f() for f in _FETCHERS], return_exceptions=True
    )

    all_jobs: list[Job] = []
    for source_name, result in zip(SOURCE_NAMES, raw_results):
        if isinstance(result, Exception):
            logger.error(
                "[fetch] %s FAILED — %s: %s",
                source_name,
                type(result).__name__,
                result,
            )
        else:
            logger.info("[fetch] %s → %d jobs", source_name, len(result))
            all_jobs.extend(result)

    fetched_count = len(all_jobs)
    logger.debug("Stage 1 complete — %d raw jobs aggregated across all sources", fetched_count)

    # --- Stage 2: Dedup ---
    # Jobs are marked seen immediately after the is_seen check so that a
    # duplicate appearing later in the list (from a different source) is
    # correctly identified as seen within the same pipeline run.
    logger.debug("Stage 2 — running dedup on %d jobs", fetched_count)
    new_jobs: list[Job] = []
    for job in all_jobs:
        job_hash = make_hash(job.title, job.company)
        if is_seen(job_hash):
            logger.debug("[dedup] SKIP — %s @ %s", job.title, job.company)
            continue
        mark_seen(job_hash, job.title, job.company, job.source)
        # Attach the hash to the Job so downstream stages can reference it
        # without recomputing it (e.g. Phase 2.5 eval callbacks need the hash).
        job.hash = job_hash
        new_jobs.append(job)

    logger.info("[dedup] %d in → %d new", fetched_count, len(new_jobs))

    # --- Stage 3: Hard discard ---
    logger.debug("Stage 3 — running hard discard on %d jobs", len(new_jobs))
    filtered = apply_hard_discard(new_jobs)

    # --- Stage 4: LLM scoring ---
    logger.debug("Stage 4 — starting LLM scoring on %d jobs", len(filtered))
    scored = await score_jobs(filtered, cv_text, llm_client)

    # --- Stage 5: Sort by match_score descending ---
    logger.debug("Stage 5 — sorting %d scored jobs by match_score", len(scored))
    scored.sort(
        key=lambda j: float((j.scoring or {}).get("match_score", 0)),
        reverse=True,
    )

    # --- Stage 6: Top N ---
    top = scored[:TOP_N_JOBS]
    logger.debug("Stage 6 — top %d selected from %d scored jobs", len(top), len(scored))

    stats = {
        "fetched": fetched_count,
        "new": len(new_jobs),
        "after_filter": len(filtered),
        "scored": len(scored),
        "delivered": len(top),
    }

    logger.info(
        "[pipeline] DONE — %d fetched → %d new → %d after filter → %d scored → %d delivered",
        fetched_count,
        len(new_jobs),
        len(filtered),
        len(scored),
        len(top),
    )

    return PipelineResult(jobs=top, stats=stats)
