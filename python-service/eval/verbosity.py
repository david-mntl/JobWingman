"""
JobWingman — verbosity and structural compliance checks.

Pure-function module that measures whether the LLM's scoring output respects
the word limits and structural constraints specified in the scoring prompt.

Why mechanical checks instead of relying on the LLM judge alone:
  The judge returns a subjective ``output_concise: bool`` based on vibes.
  This module counts actual words per field and compares against the limits
  defined in constants.py. The result is a quantitative metric you can track
  across prompt versions — "verdict averaged 14.3 words (max 12)" is more
  actionable than "output_concise: False".

Why word counting uses ``len(text.split())``:
  The prompt says "max 8 words" and the LLM interprets "words" the same way
  Python's split() does. Edge cases (hyphens, emoji, en-dashes) are not
  worth a tokeniser dependency — the goal is a human-readable proxy, not a
  precise NLP measurement.

Why WARN and not FAIL:
  LLM output is non-deterministic. A word limit of 8 might be exceeded by
  1 word on 20% of runs. Hard-failing on that would make evals flaky. The
  aggregate violation rate across all fixtures is the actionable signal — if
  it spikes after a prompt change, that tells you something broke.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from constants import (
    COMPANY_SNAPSHOT_MAX_SENTENCES,
    EXPECTED_LIST_LENGTHS,
    VERBOSITY_LIMITS,
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

# All expected top-level keys in a valid scoring JSON response.
_EXPECTED_KEYS = [
    "match_score",
    "salary_signal",
    "red_flags",
    "green_flags",
    "fit_breakdown",
    "company_snapshot",
    "role_summary",
    "company_benefits",
    "confidence",
    "verdict",
]

# Valid values for the confidence field.
_VALID_CONFIDENCE_VALUES = {"high", "medium", "low"}


@dataclass
class VerbosityViolation:
    """A single field (or list item) that exceeded its word limit."""

    field: str
    item_index: int | None  # None for scalar fields
    word_count: int
    max_words: int
    text_snippet: str  # first 60 chars for the report


@dataclass
class VerbosityReport:
    """Verbosity check results for a single scoring output."""

    violations: list[VerbosityViolation] = field(default_factory=list)
    # field name -> list of word counts (one per scalar, one per list item).
    # Used to compute averages in the aggregate summary.
    field_word_counts: dict[str, list[int]] = field(default_factory=dict)
    total_fields_checked: int = 0
    total_violations: int = 0


@dataclass
class StructureReport:
    """Structural compliance results for a single scoring output."""

    missing_fields: list[str] = field(default_factory=list)
    # (field, actual_length, expected_length)
    wrong_lengths: list[tuple[str, int, int]] = field(default_factory=list)
    # Human-readable issue strings (e.g. "confidence='maybe' not in {high,medium,low}")
    invalid_values: list[str] = field(default_factory=list)
    # (field, actual_sentence_count, max_sentences)
    sentence_violations: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def total_issues(self) -> int:
        return (
            len(self.missing_fields)
            + len(self.wrong_lengths)
            + len(self.invalid_values)
            + len(self.sentence_violations)
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(data: dict, dotted_path: str):
    """
    Traverse a dict using a dotted key path.

    Example: _resolve_path({"fit_breakdown": {"strong": [...]}}, "fit_breakdown.strong")
    returns the list at fit_breakdown.strong.

    Returns None if any intermediate key is missing.
    """
    parts = dotted_path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _count_words(text: str) -> int:
    """Count words using simple whitespace splitting."""
    return len(text.split())


def _count_sentences(text: str) -> int:
    """
    Count sentences using a simple heuristic: split on sentence-ending
    punctuation followed by whitespace or end-of-string.

    Handles: "Sentence one. Sentence two." -> 2
             "Sentence one. Sentence two"   -> 2 (trailing without period)
    """
    if not text.strip():
        return 0
    # Split on period/exclamation/question followed by space or end-of-string.
    sentences = re.split(r"[.!?](?:\s|$)", text.strip())
    # Filter out empty strings from the split result.
    return len([s for s in sentences if s.strip()])


def _snippet(text: str, max_len: int = 60) -> str:
    """Truncate text for report display."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_verbosity(scoring_result: dict) -> VerbosityReport:
    """
    Check every field in the scoring result against its word limit from
    VERBOSITY_LIMITS. Returns a VerbosityReport with violations and
    per-field word counts for aggregate statistics.

    Args:
        scoring_result: The parsed JSON dict returned by score_job().

    Returns:
        VerbosityReport with all violations and word count data.
    """
    report = VerbosityReport()

    for field_path, spec in VERBOSITY_LIMITS.items():
        max_words = spec["max_words"]
        kind = spec["kind"]
        value = _resolve_path(scoring_result, field_path)

        if value is None:
            # Missing field — handled by check_structure(), not here.
            continue

        if kind == "scalar":
            if not isinstance(value, str):
                continue
            wc = _count_words(value)
            report.field_word_counts[field_path] = [wc]
            report.total_fields_checked += 1
            if wc > max_words:
                report.violations.append(
                    VerbosityViolation(
                        field=field_path,
                        item_index=None,
                        word_count=wc,
                        max_words=max_words,
                        text_snippet=_snippet(value),
                    )
                )
                report.total_violations += 1

        elif kind == "list_item":
            if not isinstance(value, list):
                continue
            counts: list[int] = []
            for idx, item in enumerate(value):
                if not isinstance(item, str):
                    continue
                wc = _count_words(item)
                counts.append(wc)
                report.total_fields_checked += 1
                if wc > max_words:
                    report.violations.append(
                        VerbosityViolation(
                            field=field_path,
                            item_index=idx,
                            word_count=wc,
                            max_words=max_words,
                            text_snippet=_snippet(item),
                        )
                    )
                    report.total_violations += 1
            report.field_word_counts[field_path] = counts

    return report


def check_structure(scoring_result: dict) -> StructureReport:
    """
    Check that the scoring result has all expected keys, correct list
    lengths, valid enum values, and sentence counts within limits.

    Args:
        scoring_result: The parsed JSON dict returned by score_job().

    Returns:
        StructureReport with all structural issues found.
    """
    report = StructureReport()

    # --- Missing top-level keys ---
    for key in _EXPECTED_KEYS:
        if key not in scoring_result:
            report.missing_fields.append(key)

    # --- fit_breakdown sub-keys ---
    fb = scoring_result.get("fit_breakdown")
    if isinstance(fb, dict):
        for sub_key in ("strong", "gaps"):
            if sub_key not in fb:
                report.missing_fields.append(f"fit_breakdown.{sub_key}")

    # --- Expected list lengths ---
    for field_name, expected_len in EXPECTED_LIST_LENGTHS.items():
        value = scoring_result.get(field_name)
        if isinstance(value, list) and len(value) != expected_len:
            report.wrong_lengths.append((field_name, len(value), expected_len))

    # --- Confidence enum validation ---
    confidence = scoring_result.get("confidence")
    if confidence is not None and confidence not in _VALID_CONFIDENCE_VALUES:
        report.invalid_values.append(
            f"confidence='{confidence}' not in {_VALID_CONFIDENCE_VALUES}"
        )

    # --- company_snapshot sentence count ---
    snapshot = scoring_result.get("company_snapshot")
    if isinstance(snapshot, str):
        sentence_count = _count_sentences(snapshot)
        if sentence_count > COMPANY_SNAPSHOT_MAX_SENTENCES:
            report.sentence_violations.append(
                ("company_snapshot", sentence_count, COMPANY_SNAPSHOT_MAX_SENTENCES)
            )

    return report


def format_verbosity_summary(
    verbosity_reports: list[VerbosityReport],
    structure_reports: list[StructureReport],
    fixture_ids: list[str],
    fixture_labels: list[str],
) -> list[str]:
    """
    Produce markdown lines for the aggregate verbosity and structure
    sections of the eval report.

    Args:
        verbosity_reports: One VerbosityReport per fixture (aligned by index).
        structure_reports: One StructureReport per fixture (aligned by index).
        fixture_ids:       Fixture IDs, aligned by index.
        fixture_labels:    Fixture labels, aligned by index.

    Returns:
        List of markdown-formatted strings ready to join with newlines.
    """
    lines: list[str] = []

    # ------------------------------------------------------------------
    # Per-fixture verbosity warnings
    # ------------------------------------------------------------------
    fixtures_with_violations = [
        (i, r)
        for i, r in enumerate(verbosity_reports)
        if r is not None and r.total_violations > 0
    ]

    if fixtures_with_violations:
        lines += ["## Verbosity Warnings", ""]
        for idx, report in fixtures_with_violations:
            label = fixture_labels[idx]
            label_short = label[:50] + "..." if len(label) > 50 else label
            lines.append(f"### {fixture_ids[idx]} — {label_short}")
            for v in report.violations:
                if v.item_index is not None:
                    lines.append(
                        f"- {v.field}[{v.item_index}]: {v.word_count} words "
                        f"(max {v.max_words}) — \"{v.text_snippet}\""
                    )
                else:
                    lines.append(
                        f"- {v.field}: {v.word_count} words "
                        f"(max {v.max_words}) — \"{v.text_snippet}\""
                    )
            lines.append("")

    # ------------------------------------------------------------------
    # Aggregate verbosity stats
    # ------------------------------------------------------------------
    total_checked = sum(
        r.total_fields_checked for r in verbosity_reports if r is not None
    )
    total_violations = sum(
        r.total_violations for r in verbosity_reports if r is not None
    )
    violation_rate = (
        (total_violations / total_checked * 100) if total_checked > 0 else 0.0
    )

    lines += ["## Verbosity Summary", ""]
    lines.append(
        f"- Fields checked: {total_checked} | "
        f"Violations: {total_violations} ({violation_rate:.1f}%)"
    )

    # Worst offending fields (by violation count)
    field_violation_counts: dict[str, int] = defaultdict(int)
    for r in verbosity_reports:
        if r is None:
            continue
        for v in r.violations:
            field_violation_counts[v.field] += 1

    if field_violation_counts:
        sorted_offenders = sorted(
            field_violation_counts.items(), key=lambda x: x[1], reverse=True
        )
        offender_parts = [f"{name} ({count})" for name, count in sorted_offenders[:5]]
        lines.append(f"- Worst offenders: {', '.join(offender_parts)}")

    # Average word counts per field
    aggregated: dict[str, list[int]] = defaultdict(list)
    for r in verbosity_reports:
        if r is None:
            continue
        for field_name, counts in r.field_word_counts.items():
            aggregated[field_name].extend(counts)

    if aggregated:
        avg_parts: list[str] = []
        for field_name in VERBOSITY_LIMITS:
            all_counts = aggregated.get(field_name, [])
            if all_counts:
                avg = sum(all_counts) / len(all_counts)
                max_w = VERBOSITY_LIMITS[field_name]["max_words"]
                avg_parts.append(f"{field_name}: {avg:.1f}/{max_w}")
        if avg_parts:
            lines.append(f"- Avg words — {', '.join(avg_parts)}")

    lines.append("")

    # ------------------------------------------------------------------
    # Per-fixture structure warnings
    # ------------------------------------------------------------------
    fixtures_with_issues = [
        (i, r)
        for i, r in enumerate(structure_reports)
        if r is not None and r.total_issues > 0
    ]

    if fixtures_with_issues:
        lines += ["## Structure Warnings", ""]
        for idx, report in fixtures_with_issues:
            fid = fixture_ids[idx]
            for mf in report.missing_fields:
                lines.append(f"- {fid}: missing field '{mf}'")
            for field_name, actual, expected in report.wrong_lengths:
                lines.append(
                    f"- {fid}: {field_name} has {actual} items (expected {expected})"
                )
            for issue in report.invalid_values:
                lines.append(f"- {fid}: {issue}")
            for field_name, actual, max_s in report.sentence_violations:
                lines.append(
                    f"- {fid}: {field_name} has {actual} sentences (max {max_s})"
                )
        lines.append("")

    return lines
