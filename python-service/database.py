"""
JobWingman — SQLite database module.

Responsibilities:
- Open (and create if missing) the SQLite database file.
- Create the `seen_jobs` table on first run.
- Provide two operations used by the deduplication layer:
    - is_seen(hash)  → bool   check if a job was already processed
    - mark_seen(job) → None   insert a new job hash with a 30-day expiry
- Purge expired rows on every startup so the file does not grow forever.

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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
