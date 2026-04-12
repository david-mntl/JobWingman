"""
JobWingman — eval runner.

Loads the fixture dataset, scores each job using the live scoring prompt,
checks the result against the fixture's expected constraints, optionally runs
the LLM-as-judge on each result, and writes a markdown report.

Why this is a standalone script and not a pytest suite:
  pytest is the right tool for deterministic unit tests. LLM output is not
  deterministic — the same prompt can return 8.2 one run and 8.5 the next.
  A standalone eval runner produces a report you compare across prompt
  versions, not a binary pass/fail that would be flaky in CI.

Usage:
  cd python-service
  python eval/run_eval.py                          # full mode (default)
  python eval/run_eval.py --no-judge               # score assertions only
  python eval/run_eval.py --fixture f004           # single fixture, full mode
  python eval/run_eval.py --fixture f004 --no-judge

Or via the shell wrapper:
  ./eval/run_eval.sh
  ./eval/run_eval.sh --no-judge
  ./eval/run_eval.sh --fixture f004
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the python-service root importable from this subdirectory.
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SERVICE_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_SERVICE_ROOT / ".." / ".env")

from constants import PROMPT_VERSION, MIN_MATCH_SCORE, CV_PATH, JUDGE_MIN_QUALITY  # noqa: E402
from llm import GeminiClient  # noqa: E402
from models.job import Job  # noqa: E402
from pipeline.filters import apply_hard_discard  # noqa: E402
from pipeline.scoring import score_job  # noqa: E402
from eval.judge import judge_scoring  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_FIXTURES_PATH = Path(__file__).parent / "fixtures" / "jobs.json"
_CV_FILE = _SERVICE_ROOT / CV_PATH
_TEST_RESULTS_ROOT = Path(__file__).parent / "test_results"

# Append-only JSONL log of eval runs — one line per run, tracked in git.
_EVAL_HISTORY_PATH = _TEST_RESULTS_ROOT / "eval_history.jsonl"

# ---------------------------------------------------------------------------
# ANSI colour helpers (no third-party dependency)
# ---------------------------------------------------------------------------

_GREEN = "\033[92m"
_RED = "\033[91m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _green(s: str) -> str:
    return f"{_GREEN}{s}{_RESET}"


def _red(s: str) -> str:
    return f"{_RED}{s}{_RESET}"


def _bold(s: str) -> str:
    return f"{_BOLD}{s}{_RESET}"


def _dim(s: str) -> str:
    return f"{_DIM}{s}{_RESET}"


def _cyan(s: str) -> str:
    return f"{_CYAN}{s}{_RESET}"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FixtureResult:
    fixture_id: str
    label: str
    expected_action: str
    expected_score_min: float | None
    expected_score_max: float | None
    actual_score: float | None  # None when discarded before scoring
    status: str  # "PASS" | "FAIL"
    failure_reason: str = ""
    judge: dict | None = None  # full judge dict; None when not run
    scoring_result: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def _load_fixtures(fixture_id: str | None) -> list[dict]:
    if not _FIXTURES_PATH.exists():
        print(_red(f"ERROR: Fixtures file not found at {_FIXTURES_PATH}"))
        sys.exit(1)

    with open(_FIXTURES_PATH, encoding="utf-8") as f:
        all_fixtures: list[dict] = json.load(f)

    if fixture_id is not None:
        matched = [fx for fx in all_fixtures if fx.get("id") == fixture_id]
        if not matched:
            ids = [fx.get("id") for fx in all_fixtures]
            print(_red(f"ERROR: Fixture '{fixture_id}' not found. Available: {ids}"))
            sys.exit(1)
        return matched

    return all_fixtures


def _build_job(fixture: dict) -> Job:
    """Build a Job dataclass from the fixture's job data."""
    j = fixture["job"]
    return Job(
        title=j.get("title", "Unknown"),
        company=j.get("company", "Unknown"),
        location=j.get("location", ""),
        description=j.get("description", ""),
        url=fixture.get("source_url") or "https://eval.fixture/no-url",
        source="eval_fixture",
        tags=j.get("tags") or [],
        remote=bool(j.get("remote", False)),
    )


# ---------------------------------------------------------------------------
# Assertion logic
# ---------------------------------------------------------------------------


def _check_result(fixture: dict, job: Job, scored: Job | None) -> FixtureResult:
    """
    Compare the actual scoring outcome against the fixture's expected block.
    Returns a FixtureResult with status PASS or FAIL.
    """
    expected = fixture.get("expected", {})
    action = expected.get("action", "score")
    score_min = expected.get("score_min")
    score_max = expected.get("score_max")
    must_contain = expected.get("must_have_green_flag_containing")

    actual_score: float | None = None
    scoring_result: dict = {}

    if scored is not None and scored.scoring:
        actual_score = float(scored.scoring.get("match_score", 0))
        scoring_result = scored.scoring

    base = FixtureResult(
        fixture_id=fixture["id"],
        label=fixture.get("label", ""),
        expected_action=action,
        expected_score_min=score_min,
        expected_score_max=score_max,
        actual_score=actual_score,
        status="PASS",
        scoring_result=scoring_result,
    )

    # hard_discard fixtures are checked before this function is called
    # (apply_hard_discard runs synchronously). If we reach here for a
    # hard_discard fixture, it means the filter did NOT catch it → FAIL.
    if action == "hard_discard":
        base.status = "FAIL"
        base.failure_reason = (
            "Expected hard_discard but job was not caught by apply_hard_discard()"
        )
        return base

    if action == "score_discard":
        if scored is None:
            # Correctly discarded by the LLM (score < MIN_MATCH_SCORE)
            if score_max is not None and score_max <= 0.1:
                # For explicit score=0 cases (salary / freelance), we can't
                # inspect the actual score because score_job returns None
                # when below threshold. We treat the discard itself as PASS.
                pass
            return base  # PASS — job was correctly discarded
        # Job survived scoring but should have been discarded
        base.status = "FAIL"
        base.failure_reason = (
            f"Expected discard (score_max={score_max}) but job passed scoring with "
            f"score={actual_score:.1f}"
        )
        return base

    # action == "score"
    if scored is None:
        base.status = "FAIL"
        base.failure_reason = (
            f"Expected score in {score_min}–{score_max} but job was discarded "
            f"(score below {MIN_MATCH_SCORE})"
        )
        return base

    # Check score range
    if score_min is not None and actual_score is not None and actual_score < score_min:
        base.status = "FAIL"
        base.failure_reason = (
            f"Score {actual_score:.1f} below expected minimum {score_min}"
        )
        return base

    if score_max is not None and actual_score is not None and actual_score > score_max:
        base.status = "FAIL"
        base.failure_reason = (
            f"Score {actual_score:.1f} above expected maximum {score_max}"
        )
        return base

    # Check green flag presence
    if must_contain:
        green_flags: list[str] = scoring_result.get("green_flags", [])
        combined = " ".join(green_flags).lower()
        if must_contain.lower() not in combined:
            base.status = "FAIL"
            base.failure_reason = f"Expected a green flag containing '{must_contain}' but got: {green_flags}"
            return base

    return base  # PASS


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _score_display(result: FixtureResult) -> str:
    """Human-readable score string for the terminal and report."""
    if result.expected_action == "hard_discard":
        return "hard discard"
    if result.actual_score is None:
        return "discarded"
    return f"{result.actual_score:.1f}"


def _expected_display(result: FixtureResult) -> str:
    if result.expected_action == "hard_discard":
        return "hard_discard"
    if result.expected_action == "score_discard":
        sm = result.expected_score_max
        return f"< {sm}" if sm is not None else "discard"
    lo = result.expected_score_min
    hi = result.expected_score_max
    if lo is not None and hi is not None:
        return f"{lo}–{hi}"
    return "score"


def _judge_display(result: FixtureResult) -> str:
    if result.judge is None:
        return "—"
    quality = result.judge.get("overall_quality", "?")
    return f"{quality}/5"


def _write_report(
    results: list[FixtureResult],
    mode: str,
    fixture_id: str | None,
    run_dt: datetime,
) -> Path:
    """Write a markdown report and return the path it was written to."""
    if fixture_id:
        subfolder = _TEST_RESULTS_ROOT / "single"
        filename = (
            f"{fixture_id}_{PROMPT_VERSION}_{run_dt.strftime('%Y-%m-%d_%H%M%S')}.md"
        )
    elif mode == "full":
        subfolder = _TEST_RESULTS_ROOT / "full"
        filename = f"{PROMPT_VERSION}_{run_dt.strftime('%Y-%m-%d_%H%M%S')}.md"
    else:
        subfolder = _TEST_RESULTS_ROOT / "score"
        filename = f"{PROMPT_VERSION}_{run_dt.strftime('%Y-%m-%d_%H%M%S')}.md"

    subfolder.mkdir(parents=True, exist_ok=True)
    report_path = subfolder / filename

    passed = sum(1 for r in results if r.status == "PASS")
    failed = len(results) - passed

    judge_scores = [
        r.judge.get("overall_quality", 0)
        for r in results
        if r.judge is not None
        and isinstance(r.judge.get("overall_quality"), (int, float))
    ]
    avg_judge = sum(judge_scores) / len(judge_scores) if judge_scores else None

    score_deltas = []
    for r in results:
        if (
            r.actual_score is not None
            and r.expected_score_min is not None
            and r.expected_score_max is not None
        ):
            midpoint = (r.expected_score_min + r.expected_score_max) / 2
            score_deltas.append(abs(r.actual_score - midpoint))
    avg_delta = sum(score_deltas) / len(score_deltas) if score_deltas else None

    lines: list[str] = [
        f"# Eval Report — {PROMPT_VERSION} — {run_dt.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Mode: **{mode}**",
        "",
        "## Summary",
        f"- Fixtures run: {len(results)} | Passed: {passed} | Failed: {failed}",
    ]
    if avg_delta is not None:
        lines.append(f"- Avg score delta from expected midpoint: {avg_delta:.2f}")
    if avg_judge is not None:
        lines.append(f"- Judge avg quality score: {avg_judge:.1f} / 5.0")
    lines.append(f"- Prompt version: {PROMPT_VERSION}")
    lines.append("")

    # Results table
    lines += [
        "## Results Table",
        "",
        "| ID | Label | Expected | Actual | Status | Judge |",
        "|----|-------|---------|--------|--------|-------|",
    ]
    for r in results:
        status_icon = "✅" if r.status == "PASS" else "❌"
        label_short = r.label[:45] + "…" if len(r.label) > 45 else r.label
        lines.append(
            f"| {r.fixture_id} | {label_short} | {_expected_display(r)} "
            f"| {_score_display(r)} | {status_icon} | {_judge_display(r)} |"
        )

    # Failures detail
    failures = [r for r in results if r.status == "FAIL"]
    if failures:
        lines += ["", "## Failed Fixtures", ""]
        for r in failures:
            lines += [
                f"### ❌ {r.fixture_id} — {r.label}",
                f"**Expected:** {_expected_display(r)} | **Actual:** {_score_display(r)}",
                f"**Failure reason:** {r.failure_reason}",
            ]
            if r.judge:
                verdict = r.judge.get("verdict", "")
                issues = r.judge.get("issues", [])
                if verdict:
                    lines.append(f"**Judge verdict:** {verdict}")
                if issues:
                    issues_text = "; ".join(issues)
                    lines.append(f"**Judge issues:** {issues_text}")
                q = r.judge.get("overall_quality")
                lines.append(f"**Judge quality:** {q}/5")
            lines.append("")

    # Full judge detail for all fixtures (full mode only)
    if mode == "full" and any(r.judge for r in results):
        lines += ["## Judge Detail (all fixtures)", ""]
        for r in results:
            if r.judge is None:
                continue
            lines += [
                f"### {r.fixture_id} — {r.label}",
                f"Overall quality: **{r.judge.get('overall_quality', '?')}/5**",
                "",
                "| Dimension | Result |",
                "|-----------|--------|",
                f"| Score in expected range | {r.judge.get('score_in_expected_range', '—')} |",
                f"| AI priority correct | {r.judge.get('ai_priority_correct', '—')} |",
                f"| Office penalty applied | {r.judge.get('office_penalty_applied', 'N/A')} |",
                f"| ML research penalty applied | {r.judge.get('ml_research_penalty_applied', 'N/A')} |",
                f"| Benefits boost applied | {r.judge.get('benefits_boost_applied', 'N/A')} |",
                f"| Location rule correct | {r.judge.get('location_rule_correct', '—')} |",
                f"| Output concise | {r.judge.get('output_concise', '—')} |",
                "",
            ]
            issues = r.judge.get("issues", [])
            if issues:
                lines.append("**Issues:**")
                for issue in issues:
                    lines.append(f"- {issue}")
                lines.append("")
            verdict = r.judge.get("verdict", "")
            if verdict:
                lines.append(f"**Verdict:** {verdict}")
            lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# Main eval loop
# ---------------------------------------------------------------------------


async def _run(fixture_id: str | None, run_judge: bool) -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print(_red("ERROR: GEMINI_API_KEY not set — check your .env file."))
        sys.exit(1)

    if not _CV_FILE.exists():
        print(_red(f"ERROR: CV not found at {_CV_FILE}"))
        sys.exit(1)

    cv = _CV_FILE.read_text(encoding="utf-8").strip()
    llm_client = GeminiClient(api_key=api_key)
    fixtures = _load_fixtures(fixture_id)
    run_dt = datetime.now()

    mode = "full" if run_judge else "score"
    mode_label = "full (score + judge)" if run_judge else "score only"

    print()
    print(_bold(f"JobWingman Eval — {PROMPT_VERSION}  [{mode_label}]"))
    print(_dim("━" * 60))

    results: list[FixtureResult] = []
    total = len(fixtures)

    for idx, fixture in enumerate(fixtures, start=1):
        fx_id = fixture.get("id", f"f{idx:03d}")
        label = fixture.get("label", "")
        expected = fixture.get("expected", {})
        action = expected.get("action", "score")

        prefix = f"[{idx}/{total}] {_cyan(fx_id)}"
        label_display = label[:48] + "…" if len(label) > 48 else label
        print(f"{prefix}  {_dim(label_display)}", end="", flush=True)

        job = _build_job(fixture)
        result: FixtureResult

        # --- Hard discard check (synchronous, free) ---
        if action == "hard_discard":
            survivors = apply_hard_discard([job])
            was_discarded = len(survivors) == 0
            result = FixtureResult(
                fixture_id=fx_id,
                label=label,
                expected_action=action,
                expected_score_min=None,
                expected_score_max=None,
                actual_score=None,
                status="PASS" if was_discarded else "FAIL",
                failure_reason=""
                if was_discarded
                else "apply_hard_discard() did not catch this job",
                scoring_result={},
                judge=None,
            )
            status_str = (
                _green("✅ PASS  hard_discard")
                if result.status == "PASS"
                else _red("❌ FAIL  not discarded")
            )
            print(f"\r{prefix}  {_dim(label_display):<50}  {status_str}")
            results.append(result)
            continue

        # --- LLM scoring ---
        # Add inter-call delay between fixtures (not before the very first call)
        if idx > 1:
            await asyncio.sleep(llm_client.delay_between_calls)

        # Hard-discard pre-filter still runs even for non-hard_discard fixtures
        # so the scorer is not called unnecessarily for jobs that trip the filter.
        survivors = apply_hard_discard([job])
        if not survivors:
            # Job was caught by hard filter but expected action was score/score_discard
            if action == "score_discard":
                # Hard discard is a stricter outcome — treat as PASS for score_discard
                result = FixtureResult(
                    fixture_id=fx_id,
                    label=label,
                    expected_action=action,
                    expected_score_min=expected.get("score_min"),
                    expected_score_max=expected.get("score_max"),
                    actual_score=None,
                    status="PASS",
                    failure_reason="",
                    scoring_result={},
                    judge=None,
                )
            else:
                result = FixtureResult(
                    fixture_id=fx_id,
                    label=label,
                    expected_action=action,
                    expected_score_min=expected.get("score_min"),
                    expected_score_max=expected.get("score_max"),
                    actual_score=None,
                    status="FAIL",
                    failure_reason=f"Expected scored action '{action}' but apply_hard_discard() discarded it",
                    scoring_result={},
                    judge=None,
                )
            status_str = (
                _green("✅ PASS") if result.status == "PASS" else _red("❌ FAIL")
            )
            print(f"\r{prefix}  {_dim(label_display):<50}  {status_str}")
            results.append(result)
            continue

        try:
            scored = await score_job(job, cv, llm_client)
        except Exception as exc:
            result = FixtureResult(
                fixture_id=fx_id,
                label=label,
                expected_action=action,
                expected_score_min=expected.get("score_min"),
                expected_score_max=expected.get("score_max"),
                actual_score=None,
                status="FAIL",
                failure_reason=f"score_job() raised {type(exc).__name__}: {exc}",
                scoring_result={},
                judge=None,
            )
            status_str = _red(f"❌ FAIL  scoring error — {type(exc).__name__}: {exc}")
            print(f"\r{prefix}  {_dim(label_display):<50}  {status_str}")
            results.append(result)
            continue

        result = _check_result(fixture, job, scored)

        # --- LLM judge (full mode only, skip hard_discard) ---
        if run_judge:
            await asyncio.sleep(llm_client.delay_between_calls)
            scoring_for_judge = result.scoring_result if result.scoring_result else {}
            result.judge = await judge_scoring(
                fixture, scoring_for_judge, llm_client, cv
            )

            # --- Judge quality gate ---
            # If the fixture passed score-range assertions but the judge rated
            # output quality below the minimum threshold, downgrade to FAIL.
            # An already-FAIL result keeps its original (more specific) reason.
            judge_quality = result.judge.get("overall_quality", 0)
            if (
                result.status == "PASS"
                and isinstance(judge_quality, (int, float))
                and judge_quality < JUDGE_MIN_QUALITY
            ):
                result.status = "FAIL"
                result.failure_reason = (
                    f"Judge quality {judge_quality}/5 below minimum {JUDGE_MIN_QUALITY}"
                )

        status_str: str
        score_part = (
            f"{result.actual_score:.1f}"
            if result.actual_score is not None
            else "discarded"
        )
        exp_part = f"(exp {_expected_display(result)})"
        judge_part = f"Judge: {_judge_display(result)}" if run_judge else ""

        if result.status == "PASS":
            status_str = _green(f"✅ PASS  {score_part} {exp_part}")
        else:
            status_str = _red(f"❌ FAIL  {score_part} {exp_part}")
            if result.failure_reason:
                status_str += _red(f" — {result.failure_reason}")

        line = f"\r{prefix}  {_dim(label_display):<50}  {status_str}"
        if judge_part:
            line += f"  {_dim(judge_part)}"
        print(line)

        results.append(result)

    # --- Summary ---
    passed = sum(1 for r in results if r.status == "PASS")
    failed = total - passed

    judge_scores = [
        r.judge.get("overall_quality", 0)
        for r in results
        if r.judge is not None
        and isinstance(r.judge.get("overall_quality"), (int, float))
    ]
    avg_judge_str = (
        f" | Avg judge: {sum(judge_scores) / len(judge_scores):.1f}/5.0"
        if judge_scores
        else ""
    )

    print(_dim("━" * 60))
    summary = f"Results: {_green(str(passed))}/{total} passed{avg_judge_str}"
    if failed:
        summary += f" | {_red(str(failed))} failed"
    print(summary)

    report_path = _write_report(results, mode, fixture_id, run_dt)
    print(f"Report saved: {_cyan(str(report_path.relative_to(_SERVICE_ROOT.parent)))}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    args = sys.argv[1:]
    run_judge = "--no-judge" not in args

    fixture_id: str | None = None
    if "--fixture" in args:
        idx = args.index("--fixture")
        if idx + 1 < len(args):
            fixture_id = args[idx + 1]
        else:
            print(_red("ERROR: --fixture requires a fixture ID (e.g. --fixture f004)"))
            sys.exit(1)

    asyncio.run(_run(fixture_id=fixture_id, run_judge=run_judge))


if __name__ == "__main__":
    main()
