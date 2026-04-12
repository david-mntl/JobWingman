"""
JobWingman — SQLite database module.

Responsibilities:
- Open (and create if missing) the SQLite database file.
- Create the `seen_jobs` and `saved_jobs` tables on first run.
- Provide operations used by the deduplication layer:
    - is_seen(hash)  → bool   check if a job was already processed
    - mark_seen(job) → None   insert a new job hash with a 30-day expiry
- Provide operations for the saved-jobs store:
    - save_job(job)           → int        persist a scored job the user wants to keep
    - get_saved_jobs()        → list[Job]  return all saved jobs, newest first
    - delete_saved_job(db_id) → bool       remove a saved job by its row id
- Purge expired seen_jobs rows on every startup so the file does not grow forever.

Why SQLite and not PostgreSQL here:
  SQLite requires zero infrastructure — it is a single file managed by
  Python's built-in `sqlite3` module. PostgreSQL will be adopted when
  JobWingman merges into DailyLifeMate (Phase 6+), which already runs
  Postgres. Until then, SQLite is the right tool: simple schema, sequential
  writes from n8n, no concurrent-write pressure.

Why a module-level connection (not per-request):
  SQLite connections are cheap and the service is single-process. Opening
  the connection once at import time avoids the overhead of reconnecting on
  every API call and keeps the code simple. Thread safety is handled by
  passing check_same_thread=False — FastAPI runs handlers in a thread pool,
  so without this flag SQLite would raise an error on any request that lands
  on a thread different from the one that opened the connection.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from models.job import Job

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH = Path("data/jobwingman.db")

# Number of days before a seen-job record expires and the same job can be
# shown again. 30 days matches the "re-surface stale listings" policy.
SEEN_JOBS_EXPIRY_DAYS = 30

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_SEEN_JOBS = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    hash         TEXT        PRIMARY KEY,   -- MD5(normalized title + company)
    title        TEXT        NOT NULL,
    company      TEXT        NOT NULL,
    source       TEXT        NOT NULL,      -- e.g. "remotive"
    first_seen_at TIMESTAMP  NOT NULL,
    expires_at   TIMESTAMP   NOT NULL
);
"""

# Every field shown in the Telegram job card is stored here so the user can
# retrieve a fully-rendered card later. JSON columns (tags, red_flags, etc.)
# are serialized as text and decoded back to Python objects on read.
#
# Why UNIQUE on hash and INSERT OR IGNORE in save_job():
#   The hash is the same MD5 key used for deduplication. The UNIQUE constraint
#   means the DB itself enforces that the same job cannot be saved twice. If the
#   user taps "Save" on an already-saved job, INSERT OR IGNORE silently skips
#   the insert — first save wins, no error raised.
_CREATE_SAVED_JOBS = """
CREATE TABLE IF NOT EXISTS saved_jobs (
    id               INTEGER    PRIMARY KEY AUTOINCREMENT,
    hash             TEXT       NOT NULL UNIQUE,
    title            TEXT       NOT NULL,
    company          TEXT       NOT NULL,
    location         TEXT       NOT NULL,
    description      TEXT       NOT NULL,
    url              TEXT       NOT NULL,
    source           TEXT       NOT NULL,
    remote           INTEGER    NOT NULL DEFAULT 0,  -- 0/1; SQLite has no BOOLEAN
    salary_min       INTEGER,
    salary_max       INTEGER,
    tags             TEXT       NOT NULL,            -- JSON array
    match_score      REAL       NOT NULL,
    salary_signal    TEXT       NOT NULL,
    red_flags        TEXT       NOT NULL,            -- JSON array
    green_flags      TEXT       NOT NULL,            -- JSON array
    fit_breakdown    TEXT       NOT NULL,            -- JSON object {strong, gaps}
    company_snapshot TEXT       NOT NULL,
    role_summary     TEXT       NOT NULL,            -- JSON array
    company_benefits TEXT       NOT NULL,            -- JSON array
    confidence       TEXT       NOT NULL,
    verdict          TEXT       NOT NULL,
    saved_at         TIMESTAMP  NOT NULL
);
"""

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

# Ensure the data/ directory exists before SQLite tries to create the file.
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
_conn.row_factory = sqlite3.Row   # rows behave like dicts: row["hash"]


def _init() -> None:
    """
    Create tables and purge expired rows.

    Called once at module import. Running CREATE TABLE IF NOT EXISTS is
    idempotent, so it is safe to call on every startup without checking
    whether the table already exists.

    Purging expired rows here (rather than on every insert) keeps the hot
    path fast. The trade-off is that a stale row lingers until the next
    restart, which is acceptable — the expiry window is 30 days, not 30
    seconds.
    """
    _conn.execute(_CREATE_SEEN_JOBS)
    _conn.execute(_CREATE_SAVED_JOBS)
    _conn.execute(
        "DELETE FROM seen_jobs WHERE expires_at < ?",
        (datetime.now(timezone.utc).isoformat(),),
    )
    _conn.commit()


_init()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def make_hash(title: str, company: str) -> str:
    """
    Return an MD5 hex digest of the normalized title + company string.

    Normalization (lowercase + strip) ensures that minor formatting
    differences between sources ("Senior Engineer" vs "senior engineer ")
    do not produce duplicate records for the same job.

    Why MD5 and not SHA-256:
      Collision resistance at cryptographic strength is unnecessary here —
      we are deduplicating job listings, not signing data. MD5 produces a
      shorter, readable 32-char hex string and is faster, which matters when
      processing hundreds of listings per run.
    """
    normalized = f"{title.strip().lower()}|{company.strip().lower()}"
    return hashlib.md5(normalized.encode()).hexdigest()


def is_seen(job_hash: str) -> bool:
    """
    Return True if the hash exists in seen_jobs and has not expired.

    The expiry check is redundant with the purge in _init(), but it acts as
    a safety net for long-running processes that stay alive across the expiry
    boundary without restarting.
    """
    now = datetime.now(timezone.utc).isoformat()
    row = _conn.execute(
        "SELECT 1 FROM seen_jobs WHERE hash = ? AND expires_at > ?",
        (job_hash, now),
    ).fetchone()
    return row is not None


def clear_all_seen() -> int:
    """
    Delete every row from seen_jobs and return the number of rows removed.

    Used during development to reset the dedup state so the pipeline
    re-processes all jobs from scratch. Exposed via DELETE /jobs/clear-db.
    """
    cursor = _conn.execute("SELECT COUNT(*) FROM seen_jobs")
    count = cursor.fetchone()[0]
    _conn.execute("DELETE FROM seen_jobs")
    _conn.commit()
    return count


def mark_seen(job_hash: str, title: str, company: str, source: str) -> None:
    """
    Insert a new seen_jobs record with a 30-day expiry.

    INSERT OR IGNORE means a duplicate hash (race condition between two
    concurrent workflow runs) is silently dropped rather than raising an
    IntegrityError. The first writer wins, which is the correct behaviour.
    """
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=SEEN_JOBS_EXPIRY_DAYS)
    _conn.execute(
        """
        INSERT OR IGNORE INTO seen_jobs (hash, title, company, source, first_seen_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_hash, title, company, source, now.isoformat(), expires.isoformat()),
    )
    _conn.commit()


# ---------------------------------------------------------------------------
# Saved jobs
# ---------------------------------------------------------------------------


def save_job(job: Job) -> int:
    """
    Persist a scored job to saved_jobs and return its row id.

    The UNIQUE constraint on hash and INSERT OR IGNORE together mean that
    saving the same job twice is a silent no-op — the first save wins and
    no IntegrityError is raised. The function always returns a valid id: on
    a fresh insert it is the new row's id; on a conflict it is the id of the
    already-existing row (retrieved via a follow-up SELECT).

    All JSON columns (tags, red_flags, green_flags, fit_breakdown,
    role_summary, company_benefits) are serialised to text before storage and
    decoded back to Python objects by get_saved_jobs().
    """
    scoring = job.scoring or {}
    now = datetime.now(timezone.utc).isoformat()
    _conn.execute(
        """
        INSERT OR IGNORE INTO saved_jobs (
            hash, title, company, location, description, url, source,
            remote, salary_min, salary_max, tags,
            match_score, salary_signal, red_flags, green_flags,
            fit_breakdown, company_snapshot, role_summary,
            company_benefits, confidence, verdict, saved_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?
        )
        """,
        (
            job.hash,
            job.title,
            job.company,
            job.location,
            job.description,
            job.url,
            job.source,
            1 if job.remote else 0,
            job.salary_min,
            job.salary_max,
            json.dumps(job.tags),
            scoring.get("match_score"),
            scoring.get("salary_signal", ""),
            json.dumps(scoring.get("red_flags") or []),
            json.dumps(scoring.get("green_flags") or []),
            json.dumps(scoring.get("fit_breakdown") or {}),
            scoring.get("company_snapshot", ""),
            json.dumps(scoring.get("role_summary") or []),
            json.dumps(scoring.get("company_benefits") or []),
            scoring.get("confidence", ""),
            scoring.get("verdict", ""),
            now,
        ),
    )
    _conn.commit()
    row = _conn.execute(
        "SELECT id FROM saved_jobs WHERE hash = ?", (job.hash,)
    ).fetchone()
    return row["id"]


def get_saved_jobs() -> list[Job]:
    """
    Return all saved jobs as Job objects, ordered by saved_at descending.

    JSON columns are decoded back to Python objects (lists/dicts) so callers
    do not need to know about the text-serialisation format. The scoring dict
    is reconstructed from the individual scoring columns so it matches the
    shape that the formatter expects (same keys as the LLM JSON output).

    db_id is populated from the saved_jobs row id so callers can reference or
    delete the record without a separate lookup.
    """
    rows = _conn.execute(
        "SELECT * FROM saved_jobs ORDER BY saved_at DESC"
    ).fetchall()

    jobs: list[Job] = []
    for row in rows:
        scoring = {
            "match_score": row["match_score"],
            "salary_signal": row["salary_signal"],
            "red_flags": json.loads(row["red_flags"]),
            "green_flags": json.loads(row["green_flags"]),
            "fit_breakdown": json.loads(row["fit_breakdown"]),
            "company_snapshot": row["company_snapshot"],
            "role_summary": json.loads(row["role_summary"]),
            "company_benefits": json.loads(row["company_benefits"]),
            "confidence": row["confidence"],
            "verdict": row["verdict"],
        }
        job = Job(
            title=row["title"],
            company=row["company"],
            location=row["location"],
            description=row["description"],
            url=row["url"],
            source=row["source"],
            tags=json.loads(row["tags"]),
            remote=bool(row["remote"]),
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            hash=row["hash"],
            scoring=scoring,
            db_id=row["id"],
        )
        jobs.append(job)
    return jobs


def delete_saved_job(db_id: int) -> bool:
    """
    Delete a saved job by its integer row id.

    Returns True if a row was deleted, False if no row with that id existed.
    Reserved for future use by a /delete-job <id> command — not yet called
    by any handler. Defined now so the public API is stable when the command
    is added.
    """
    cursor = _conn.execute("DELETE FROM saved_jobs WHERE id = ?", (db_id,))
    _conn.commit()
    return cursor.rowcount > 0
