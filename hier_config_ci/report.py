"""Render integration results as a Markdown report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PASS = "passed"
NEUTRAL_STATUSES = {"no-package"}
STATUS_MARKS = {
    PASS: "✅ passed",
    "tests-failed": "❌ tests failed",
    "import-failed": "❌ import failed",
    "install-failed": "❌ install failed",
    "setup-failed": "❌ setup failed",
    "version-mismatch": "❌ wrong version installed",
    "clone-failed": "❌ clone failed",
    "no-package": "➖ no Python package",
    "error": "❌ runner error",
}
OUTPUT_TAIL_LINES = 40
MAX_FAILED_TESTS = 30


def load_outcomes(results_dir: Path) -> list[dict[str, Any]]:
    """Read every result JSON file in results_dir, sorted by repo."""
    outcomes = [
        json.loads(path.read_text()) for path in sorted(results_dir.glob("*.json"))
    ]
    return sorted(outcomes, key=lambda outcome: outcome["repo"])


def is_failure(outcome: dict[str, Any]) -> bool:
    """Return True if the outcome counts as a failed integration."""
    return outcome["status"] not in NEUTRAL_STATUSES | {PASS}


def render_markdown(outcomes: list[dict[str, Any]], requested: str) -> str:
    """Return the full Markdown report."""
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    failed = sum(1 for outcome in outcomes if is_failure(outcome))
    passed = sum(1 for outcome in outcomes if outcome["status"] == PASS)
    lines = [
        f"# hier-config {requested} integration report",
        "",
        f"Generated {generated}. "
        f"{passed} of {len(outcomes)} apps passed, {failed} failed.",
        "",
        *_summary_table(outcomes, requested),
        "",
        "## Findings",
        "",
        *_findings(outcomes),
        "",
        "## Details",
        "",
    ]
    for outcome in outcomes:
        lines.extend(_details(outcome))
    return "\n".join(lines) + "\n"


def _summary_table(outcomes: list[dict[str, Any]], requested: str) -> list[str]:
    rows = [
        f"| App | Branch | Python | Declared pin | Accepts {requested} "
        "| Install | Import | Tests | Result |",
        "|-----|--------|--------|--------------|---------|---------|--------|-------|--------|",
    ]
    for outcome in outcomes:
        steps = {step["name"]: step["status"] for step in outcome.get("steps", [])}
        constraint = outcome.get("constraint") or {}
        rows.append(
            "| "
            + " | ".join(
                [
                    outcome["repo"].rsplit("/", 1)[-1],
                    f"`{outcome['branch']}`",
                    outcome.get("python") or "-",
                    f"`{constraint.get('declared', '-')}`",
                    _accepts_mark(constraint.get("accepts")),
                    _step_mark(steps.get("install")),
                    _step_mark(steps.get("import")),
                    _tests_cell(outcome.get("tests")),
                    STATUS_MARKS.get(outcome["status"], outcome["status"]),
                ]
            )
            + " |"
        )
    return rows


def _accepts_mark(accepts: bool | None) -> str:
    if accepts is None:
        return "n/a"
    return "yes" if accepts else "**no**"


def _step_mark(status: str | None) -> str:
    if status is None:
        return "-"
    return "ok" if status == "ok" else "**failed**"


def _tests_cell(tests: dict[str, int] | None) -> str:
    if not tests:
        return "-"
    parts = [f"{count} {name}" for name, count in tests.items() if count]
    return ", ".join(parts) or "0 collected"


def _findings(outcomes: list[dict[str, Any]]) -> list[str]:
    lines = []
    for outcome in outcomes:
        name = outcome["repo"].rsplit("/", 1)[-1]
        notes = outcome.get("notes") or []
        summary = STATUS_MARKS.get(outcome["status"], outcome["status"])
        lines.append(f"- **{name}** (`{outcome['branch']}`): {summary}.")
        lines.extend(f"  - {note}" for note in notes)
    return lines


def _details(outcome: dict[str, Any]) -> list[str]:
    name = outcome["repo"]
    status = STATUS_MARKS.get(outcome["status"], outcome["status"])
    lines = [
        "<details>",
        f"<summary><b>{name}</b> (<code>{outcome['branch']}</code>) — {status}</summary>",
        "",
        f"Commit `{outcome.get('commit') or 'n/a'}`, "
        f"Python {outcome.get('python') or 'n/a'}, "
        f"installed hier-config `{outcome.get('installed') or 'n/a'}`.",
        "",
        "| Step | Status | Seconds |",
        "|------|--------|---------|",
    ]
    for step in outcome.get("steps", []):
        lines.append(f"| {step['name']} | {step['status']} | {step['seconds']:.0f} |")
    failed_tests = outcome.get("failed_tests") or []
    if failed_tests:
        lines.extend(["", f"Failed tests ({len(failed_tests)}):", ""])
        lines.extend(f"- `{test}`" for test in failed_tests[:MAX_FAILED_TESTS])
        if len(failed_tests) > MAX_FAILED_TESTS:
            lines.append(f"- … and {len(failed_tests) - MAX_FAILED_TESTS} more")
    failed_step = next(
        (step for step in outcome.get("steps", []) if step["status"] != "ok"), None
    )
    if failed_step and failed_step.get("output_tail"):
        lines.extend(
            [
                "",
                f"Output tail of step `{failed_step['name']}`:",
                "",
                "```text",
                failed_step["output_tail"].rstrip(),
                "```",
            ]
        )
    lines.extend(["", "</details>", ""])
    return lines
