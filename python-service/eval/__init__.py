"""
JobWingman — eval package.

Offline evaluation tooling for the LLM scoring prompt. Nothing in this
package is imported by or coupled to the live FastAPI service — it is
a standalone test harness that reuses the scoring and filtering modules
as a library.

Contents:
  eval/fixtures/jobs.json         — 15 labelled test jobs (the regression suite)
  eval/fixtures/create_fixture.py — snapshot a live job URL into fixtures
  eval/judge.py                   — LLM-as-judge: reviews a scoring result
  eval/run_eval.py                — CLI runner: scores, asserts, generates report
  eval/run_eval.sh                — user-friendly shell wrapper with colored output
  eval/test_results/              — generated markdown reports (gitignored)
"""
