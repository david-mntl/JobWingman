"""
JobWingman — fixture creation helper.

Given a live job posting URL, fetches the page, extracts the job fields via
the LLM, and appends a skeleton fixture entry to jobs.json. The user then
fills in the "expected" block manually before running the eval.

Why snapshot instead of fetching live at eval time:
  Eval runs must be deterministic and stable. Live URLs go offline, content
  changes, and job boards add bot-detection over time. Storing the extracted
  job data inline in the fixture means the eval will produce the same input
  forever — even years after the original posting was taken down.

Usage:
  cd python-service
  python eval/fixtures/create_fixture.py "https://example.com/jobs/123"
  python eval/fixtures/create_fixture.py "https://example.com/jobs/123" --id f016
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the python-service root importable from this subdirectory.
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_SERVICE_ROOT))

from dotenv import load_dotenv  # noqa: E402 — after path insertion

load_dotenv(_SERVICE_ROOT / ".." / ".env")  # project root .env

from constants import GEMINI_MODEL, GEMINI_MAX_OUTPUT_TOKENS  # noqa: E402
from llm import GeminiClient  # noqa: E402
from job_sources.url_scraper import fetch_page_text, extract_job_fields  # noqa: E402

# Path to the fixtures file this script writes into.
FIXTURES_PATH = Path(__file__).parent / "jobs.json"


async def _create_fixture(url: str, fixture_id: str | None) -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set — check your .env file.")
        sys.exit(1)

    llm_client = GeminiClient(api_key=api_key)

    print(f"\nFetching: {url}")
    try:
        page_text = await fetch_page_text(url)
    except Exception as exc:
        print(f"ERROR: Could not fetch page — {exc}")
        sys.exit(1)

    print(f"Fetched {len(page_text):,} characters. Extracting job fields...")

    fields = await extract_job_fields(page_text, url, llm_client)
    if fields is None:
        print("ERROR: LLM could not extract job fields — this URL may not contain a job posting.")
        sys.exit(1)

    print("\nExtracted fields:")
    print(json.dumps(fields, indent=2, ensure_ascii=False))

    # Determine a safe fixture ID. If the file already has entries, auto-increment.
    existing: list[dict] = []
    if FIXTURES_PATH.exists():
        with open(FIXTURES_PATH, encoding="utf-8") as f:
            existing = json.load(f)

    if fixture_id is None:
        # Find the next f0XX id that is not already taken.
        existing_ids = {item.get("id", "") for item in existing}
        for n in range(1, 200):
            candidate = f"f{n:03d}"
            if candidate not in existing_ids:
                fixture_id = candidate
                break

    skeleton: dict = {
        "id": fixture_id,
        "label": f"TODO — {fields.get('title', 'Unknown')} at {fields.get('company', 'Unknown')}",
        "source_url": url,
        "job": {
            "title": fields.get("title") or "Unknown Role",
            "company": fields.get("company") or "Unknown Company",
            "location": fields.get("location") or "",
            "remote": bool(fields.get("remote", False)),
            "tags": fields.get("tags") or [],
            "description": fields.get("description") or "",
        },
        "expected": {
            "action": "TODO — score | hard_discard | score_discard",
            "score_min": None,
            "score_max": None,
            "must_have_green_flag_containing": None,
            "ai_priority_high": None,
            "notes": "TODO — fill this in before running eval",
        },
    }

    existing.append(skeleton)
    with open(FIXTURES_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    print(f"\nSkeleton fixture '{fixture_id}' appended to {FIXTURES_PATH}")
    print("Next steps:")
    print("  1. Open eval/fixtures/jobs.json")
    print(f"  2. Find fixture '{fixture_id}' and fill in the 'expected' block")
    print("  3. Run: ./eval/run_eval.sh --fixture", fixture_id)


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("Usage: python eval/fixtures/create_fixture.py <url> [--id fXXX]")
        sys.exit(1)

    url = args[0]
    fixture_id: str | None = None
    if "--id" in args:
        idx = args.index("--id")
        if idx + 1 < len(args):
            fixture_id = args[idx + 1]

    asyncio.run(_create_fixture(url, fixture_id))


if __name__ == "__main__":
    main()
