"""Load the integration registry and select the apps to test."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PYTHON = "3.12"
DEFAULT_INSTALL = "poetry install --no-interaction"
DEFAULT_TEST = 'poetry run pytest -q -rfE -o addopts=""'
ALL_APPS = "*"


@dataclass(frozen=True)
class App:
    """One downstream repository and the commands that test it."""

    repo: str
    branch: str
    python: str = DEFAULT_PYTHON
    module: str | None = None
    install: str = DEFAULT_INSTALL
    setup: tuple[str, ...] = ()
    test: str = DEFAULT_TEST
    env: tuple[tuple[str, str], ...] = ()

    @property
    def short_name(self) -> str:
        """Return the repository name without the owner."""
        return self.repo.rsplit("/", 1)[-1]

    def matches(self, name: str) -> bool:
        """Return True if name is the short name or the owner/name."""
        return name in (self.repo, self.short_name)


@dataclass(frozen=True)
class Registry:
    """The default hier-config version and the registered apps."""

    hier_config: str
    apps: tuple[App, ...]


def load_registry(path: Path) -> Registry:
    """Read the registry YAML file."""
    data = yaml.safe_load(path.read_text()) or {}
    defaults = data.get("defaults") or {}
    apps = tuple(_build_app(entry, defaults) for entry in data.get("apps") or ())
    return Registry(hier_config=str(data["hier_config"]), apps=apps)


def _build_app(entry: dict[str, Any], defaults: dict[str, Any]) -> App:
    merged = {**defaults, **entry}
    env = merged.get("env") or {}
    return App(
        repo=merged["repo"],
        branch=str(merged["branch"]),
        python=str(merged.get("python", DEFAULT_PYTHON)),
        module=merged.get("module"),
        install=merged.get("install", DEFAULT_INSTALL),
        setup=tuple(merged.get("setup") or ()),
        test=merged.get("test", DEFAULT_TEST),
        env=tuple((key, str(value)) for key, value in env.items()),
    )


def split_names(specs: Iterable[str]) -> list[str]:
    """Split comma-separated specs into a flat list of non-empty names."""
    names = []
    for spec in specs:
        names.extend(item.strip() for item in spec.split(",") if item.strip())
    return names


def parse_branch_overrides(specs: Iterable[str]) -> dict[str, str]:
    """Map app names to branches; a bare branch applies to every app."""
    overrides: dict[str, str] = {}
    for item in split_names(specs):
        name, separator, branch = item.partition("=")
        if separator:
            overrides[name.strip()] = branch.strip()
        else:
            overrides[ALL_APPS] = item
    return overrides


def select_apps(
    registry: Registry,
    names: Iterable[str] = (),
    branch_overrides: dict[str, str] | None = None,
) -> list[App]:
    """Return the apps that match names, with branch overrides applied.

    Raises:
        ValueError: If a name or an override does not match a registered app.
    """
    wanted = split_names(names)
    overrides = dict(branch_overrides or {})
    default_branch = overrides.pop(ALL_APPS, None)
    _reject_unknown(registry, [*wanted, *overrides])
    selected = []
    for app in registry.apps:
        if wanted and not any(app.matches(name) for name in wanted):
            continue
        branch = _override_for(app, overrides) or default_branch or app.branch
        selected.append(replace(app, branch=branch))
    return selected


def _override_for(app: App, overrides: dict[str, str]) -> str | None:
    for name, branch in overrides.items():
        if app.matches(name):
            return branch
    return None


def _reject_unknown(registry: Registry, names: Iterable[str]) -> None:
    unknown = [
        name
        for name in names
        if not any(app.matches(name) for app in registry.apps)
    ]
    if unknown:
        raise ValueError(f"unknown app(s): {', '.join(unknown)}")
