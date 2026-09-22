"""Read dependency constraints from an app's pyproject.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

HIER_CONFIG = "hier-config"
PYTHON_MINORS = tuple(f"3.{minor}" for minor in range(8, 15))


@dataclass(frozen=True)
class Constraint:
    """A dependency constraint as an app declares it."""

    declared: str
    kind: str
    specifier: SpecifierSet | None = None

    def accepts(self, version: str) -> bool | None:
        """Return whether the constraint permits version, or None if unknown."""
        if self.specifier is None:
            return None
        try:
            return self.specifier.contains(Version(version), prereleases=True)
        except InvalidVersion:
            return None


MISSING = Constraint(declared="(none)", kind="missing")


def load_pyproject(path: Path) -> dict[str, Any]:
    """Parse a pyproject.toml file."""
    return tomllib.loads(path.read_text())


def hier_config_constraint(pyproject: dict[str, Any]) -> Constraint:
    """Return the app's constraint on hier-config from PEP 621 or Poetry."""
    for requirement in pyproject.get("project", {}).get("dependencies", ()):
        constraint = _from_pep508(requirement)
        if constraint is not None:
            return constraint
    poetry = pyproject.get("tool", {}).get("poetry", {})
    for name, value in poetry.get("dependencies", {}).items():
        if canonicalize_name(name) == HIER_CONFIG:
            return _from_poetry(value)
    return MISSING


def python_constraint(pyproject: dict[str, Any]) -> Constraint:
    """Return the app's constraint on the Python version."""
    requires = pyproject.get("project", {}).get("requires-python")
    if requires:
        return _from_specifier(requires)
    poetry_python = (
        pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
    )
    if poetry_python:
        return _from_poetry(poetry_python)
    return MISSING


def unsupported_python_minors(app: Constraint, library: Constraint) -> list[str]:
    """Return the Python minors the app accepts and the library rejects."""
    if app.specifier is None or library.specifier is None:
        return []
    return [
        minor
        for minor in PYTHON_MINORS
        if app.accepts(minor) and not library.accepts(minor)
    ]


def _from_pep508(requirement: str) -> Constraint | None:
    try:
        parsed = Requirement(requirement)
    except InvalidRequirement:
        return None
    if canonicalize_name(parsed.name) != HIER_CONFIG:
        return None
    if parsed.url:
        return Constraint(declared=parsed.url, kind="url")
    return Constraint(
        declared=str(parsed.specifier) or "*",
        kind="version",
        specifier=parsed.specifier,
    )


def _from_poetry(value: Any) -> Constraint:
    if isinstance(value, str):
        return _from_specifier(value)
    if isinstance(value, dict):
        for source in ("git", "url", "path"):
            if source in value:
                return Constraint(declared=str(value[source]), kind=source)
        return _from_specifier(str(value.get("version", "*")))
    return Constraint(declared=str(value), kind="unparsed")


def _from_specifier(spec: str) -> Constraint:
    try:
        return Constraint(declared=spec, kind="version", specifier=poetry_to_pep440(spec))
    except (InvalidSpecifier, ValueError):
        return Constraint(declared=spec, kind="unparsed")


def poetry_to_pep440(spec: str) -> SpecifierSet:
    """Convert a Poetry version spec to a PEP 440 specifier set."""
    parts = [part.strip() for part in spec.split(",") if part.strip()]
    return SpecifierSet(",".join(_convert_part(part) for part in parts))


def _convert_part(part: str) -> str:
    if part == "*":
        return ""
    if part.startswith("^"):
        return _caret(part[1:])
    if part.startswith("~") and not part.startswith("~="):
        return _tilde(part[1:])
    if part[0].isdigit():
        return f"=={part}"
    return part


def _caret(version: str) -> str:
    components = [int(component) for component in version.split(".")]
    bump_index = next(
        (index for index, value in enumerate(components) if value != 0),
        len(components) - 1,
    )
    return f">={version},<{_bumped(components, bump_index)}"


def _tilde(version: str) -> str:
    components = [int(component) for component in version.split(".")]
    bump_index = min(1, len(components) - 1)
    return f">={version},<{_bumped(components, bump_index)}"


def _bumped(components: list[int], index: int) -> str:
    upper = components[: index + 1]
    upper[index] += 1
    return ".".join(str(value) for value in upper)
