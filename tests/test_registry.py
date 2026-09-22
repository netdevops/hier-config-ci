"""Tests for the registry loader and app selection."""

import pytest

from hier_config_ci import registry

REGISTRY_TEXT = """
hier_config: 4.0.0b4
defaults:
  python: "3.12"
apps:
  - repo: netdevops/hier-config-cli
    branch: main
    module: hier_config_cli
  - repo: netdevops/hier-config-mcp
    branch: develop
    python: "3.11"
    setup:
      - echo setup
    env:
      EXAMPLE: "{venv_python}"
"""


@pytest.fixture
def loaded(tmp_path):
    path = tmp_path / "integration.yml"
    path.write_text(REGISTRY_TEXT)
    return registry.load_registry(path)


def test_load_registry_applies_defaults(loaded):
    cli, mcp = loaded.apps
    assert loaded.hier_config == "4.0.0b4"
    assert cli.python == "3.12"
    assert cli.install == registry.DEFAULT_INSTALL
    assert mcp.python == "3.11"
    assert mcp.setup == ("echo setup",)
    assert mcp.env == (("EXAMPLE", "{venv_python}"),)


def test_select_by_short_or_full_name(loaded):
    assert [app.repo for app in registry.select_apps(loaded, ["hier-config-mcp"])] == [
        "netdevops/hier-config-mcp"
    ]
    assert len(registry.select_apps(loaded, ["netdevops/hier-config-cli,hier-config-mcp"])) == 2


def test_branch_overrides(loaded):
    overrides = registry.parse_branch_overrides(["next", "hier-config-cli=release"])
    selected = registry.select_apps(loaded, branch_overrides=overrides)
    assert [app.branch for app in selected] == ["release", "next"]


def test_unknown_names_are_rejected(loaded):
    with pytest.raises(ValueError, match="unknown app"):
        registry.select_apps(loaded, ["nope"])
    with pytest.raises(ValueError, match="unknown app"):
        registry.select_apps(loaded, branch_overrides={"nope": "next"})
