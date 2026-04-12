"""
JobWingman — Canonical job data model.

Why dataclass and not Pydantic BaseModel:
  Pydantic is the right choice at API boundaries (user input validation, JSON
  deserialization). Internally, between pipeline stages that we fully control,
  Pydantic's validation overhead and schema machinery are unnecessary. A
  stdlib dataclass is lighter and has no extra dependencies.

Why pipeline-added fields (hash, scoring) live on the same class:
  Introducing a separate ScoredJob or DedupedJob subclass would require the
  orchestrator, scorer, and formatter to each know about multiple types. Keeping
  one type flowing through the entire pipeline is simpler at this project scale.
  hash and scoring start as None and are populated by the orchestrator and
  scoring stage respectively.

FastAPI serialization:
  FastAPI's jsonable_encoder handles stdlib dataclasses natively by calling
  dataclasses.asdict() recursively.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Canonical job model
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """
    Canonical representation of a job posting used across every pipeline stage.

    Fields set by job sources (all required except those with defaults):
      title       Job title as displayed on the source board.
      company     Company name.
      location    Location string as provided by the source (city, country, or
                  "Remote"). Not normalised — the scorer interprets it.
      description Full job description in plain text. Empty string when the
                  source does not provide one in its listing view (e.g. Joblyst).
      url         Direct link to the job posting.
      source      Short identifier for the originating source ("arbeitnow",
                  "remoteok", etc.). Used for logging and dedup attribution.
      tags        List of tag/skill strings. Empty list when the source does
                  not provide structured tags (e.g. WeWorkRemotely RSS).
      remote      True if the source explicitly marks the role as remote.
                  False means "not specified", not "on-site required".
      salary_min  Minimum salary in the source's currency (int or None).
                  Available from RemoteOK (USD) and Joblyst (EUR).
      salary_max  Maximum salary in the source's currency (int or None).

    Fields set by the pipeline (start as None):
      hash        MD5 deduplication hash set by the orchestrator's dedup stage.
                  Format: MD5(normalised_title + "|" + normalised_company).
      scoring     Dict containing the LLM's full JSON scoring output, set by
                  the scoring stage. Keys: match_score, salary_signal,
                  red_flags, green_flags, fit_breakdown, company_snapshot,
                  role_summary, company_benefits, confidence, verdict.
    """

    # --- Required source fields ---
    title: str
    company: str
    location: str
    description: str
    url: str
    source: str

    # --- Optional source fields (have defaults) ---
    tags: list[str] = field(default_factory=list)
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None

    # --- Pipeline-added fields (always start as None) ---
    hash: str | None = None
    scoring: dict | None = None
