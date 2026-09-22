"""Tests for the pyproject constraint parser."""

import pytest

from hier_config_ci import constraints


@pytest.mark.parametrize(
    ("spec", "version", "expected"),
    [
        ("^3.2.0", "4.0.0b4", False),
        ("^3.2.0", "3.9.1", True),
        ("^3.2.0,<4.0", "3.2.0", True),
        (">=4.0.0b1,<5.0", "4.0.0b4", True),
        ("<4.0", "4.0.0b4", False),
        ("~3.2", "3.3.0", False),
        ("~3.2", "3.2.9", True),
        ("^0.2.3", "0.3.0", False),
        ("^0.2.3", "0.2.9", True),
        ("*", "4.0.0b4", True),
        ("3.2.0", "3.2.0", True),
        ("3.2.0", "3.2.1", False),
    ],
)
def test_poetry_specs_accept_versions(spec, version, expected):
    constraint = constraints.Constraint(
        declared=spec, kind="version", specifier=constraints.poetry_to_pep440(spec)
    )
    assert constraint.accepts(version) is expected


def test_pep621_dependency_with_version():
    pyproject = {"project": {"dependencies": ["hier-config>=3.3,<4"]}}
    constraint = constraints.hier_config_constraint(pyproject)
    assert constraint.kind == "version"
    assert constraint.accepts("4.0.0b4") is False


def test_pep621_dependency_with_git_url():
    pyproject = {
        "project": {"dependencies": ["hier-config @ git+https://github.com/netdevops/hier_config.git"]}
    }
    constraint = constraints.hier_config_constraint(pyproject)
    assert constraint.kind == "url"
    assert constraint.accepts("4.0.0b4") is None


def test_poetry_dependency_as_table():
    pyproject = {
        "tool": {
            "poetry": {
                "dependencies": {
                    "python": ">=3.10,<4.0",
                    "hier_config": {"version": ">=4.0.0b1,<5.0", "allow-prereleases": True},
                }
            }
        }
    }
    constraint = constraints.hier_config_constraint(pyproject)
    assert constraint.declared == ">=4.0.0b1,<5.0"
    assert constraint.accepts("4.0.0b4") is True


def test_missing_dependency():
    assert constraints.hier_config_constraint({}) is constraints.MISSING


def test_unsupported_python_minors():
    app = constraints.python_constraint(
        {"tool": {"poetry": {"dependencies": {"python": ">=3.10,<4.0"}}}}
    )
    library = constraints.Constraint(
        declared=">=3.11", kind="version", specifier=constraints.poetry_to_pep440(">=3.11")
    )
    assert constraints.unsupported_python_minors(app, library) == ["3.10"]
