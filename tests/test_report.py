"""Tests for the pytest summary parser and the Markdown report."""

from hier_config_ci import report
from hier_config_ci.runner import PYTEST_FAILED_PATTERN, parse_pytest_counts

PYTEST_OUTPUT = """
FAILED tests/test_a.py::test_one - AssertionError
ERROR tests/test_b.py::test_two
=========== 1 failed, 12 passed, 2 skipped, 1 error, 3 warnings in 1.23s ===========
"""


def test_parse_pytest_counts():
    assert parse_pytest_counts(PYTEST_OUTPUT) == {
        "failed": 1, "passed": 12, "skipped": 2, "error": 1,
    }
    assert parse_pytest_counts("no summary here") == {}


def test_failed_test_ids():
    assert PYTEST_FAILED_PATTERN.findall(PYTEST_OUTPUT) == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]


def test_render_markdown_lists_every_app():
    outcomes = [
        {
            "repo": "netdevops/hier-config-cli", "branch": "main", "status": "tests-failed",
            "python": "3.12.1", "installed": "4.0.0b4", "commit": "abc1234",
            "constraint": {"declared": "^3.3.0", "kind": "version", "accepts": False},
            "tests": {"failed": 1, "passed": 12}, "failed_tests": ["tests/test_a.py::test_one"],
            "notes": ["The declared constraint rejects 4.0.0b4."],
            "steps": [
                {"name": "install", "status": "ok", "seconds": 3.0, "output_tail": ""},
                {"name": "test", "status": "failed", "seconds": 1.0, "output_tail": "boom"},
            ],
        },
        {
            "repo": "netdevops/hier-config-orchestrator", "branch": "develop",
            "status": "no-package", "notes": [], "steps": [],
        },
    ]
    markdown = report.render_markdown(outcomes, "4.0.0b4")
    assert "# hier-config 4.0.0b4 integration report" in markdown
    assert "| hier-config-cli | `main` | 3.12.1 | `^3.3.0` | **no** | ok | - | 1 failed, 12 passed | ❌ tests failed |" in markdown
    assert "➖ no Python package" in markdown
    assert "- `tests/test_a.py::test_one`" in markdown
    assert "boom" in markdown
    assert report.is_failure(outcomes[0]) is True
    assert report.is_failure(outcomes[1]) is False
