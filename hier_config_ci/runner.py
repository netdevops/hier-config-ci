"""Run the integration pipeline for one app against a hier-config build."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hier_config_ci import constraints
from hier_config_ci.registry import App

VERSION_PATTERN = re.compile(r"^\d")
PYTEST_COUNT_PATTERN = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)
PYTEST_FAILED_PATTERN = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
LOCK_HINT = "poetry lock"
OUTPUT_TAIL_LINES = 40
VERIFY_SCRIPT = """
import importlib.metadata, json, sys
metadata = importlib.metadata.metadata("hier-config")
print("VERIFY " + json.dumps({
    "version": metadata["Version"],
    "requires_python": metadata.get("Requires-Python"),
    "python": ".".join(str(part) for part in sys.version_info[:3]),
}))
"""


@dataclass
class Step:
    """One executed command and its result."""

    name: str
    command: str
    returncode: int
    seconds: float
    output: str = field(repr=False)

    @property
    def status(self) -> str:
        """Return 'ok' or 'failed'."""
        return "ok" if self.returncode == 0 else "failed"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form; the output is cut to its tail."""
        tail = "\n".join(self.output.splitlines()[-OUTPUT_TAIL_LINES:])
        return {
            "name": self.name,
            "command": self.command,
            "status": self.status,
            "seconds": round(self.seconds, 1),
            "output_tail": tail,
        }


@dataclass
class Outcome:
    """The result of the pipeline for one app."""

    repo: str
    branch: str
    requested: str
    status: str = "not-run"
    commit: str | None = None
    python: str | None = None
    installed: str | None = None
    constraint: dict[str, Any] | None = None
    tests: dict[str, int] | None = None
    failed_tests: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        data = asdict(self)
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


class StepFailed(Exception):
    """A pipeline step returned a non-zero exit code."""

    def __init__(self, status: str, note: str | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.note = note


class AppRunner:
    """Clone, install, override hier-config, and test one app."""

    def __init__(
        self,
        app: App,
        requested: str,
        workdir: Path,
        results_dir: Path,
        token: str | None = None,
    ) -> None:
        self.app = app
        self.requested = requested
        self.workdir = workdir.resolve()
        self.results_dir = results_dir.resolve()
        self.token = token
        self.repo_dir = self.workdir / "repos" / app.short_name
        self.outcome = Outcome(repo=app.repo, branch=app.branch, requested=requested)
        self.env = _base_environment()
        self.venv_python: str | None = None

    def run(self) -> Outcome:
        """Run the pipeline and write the result JSON."""
        self._banner(f"{self.app.repo} @ {self.app.branch} with hier-config {self.requested}")
        try:
            self._clone()
            if not (self.repo_dir / "pyproject.toml").exists():
                self.outcome.status = "no-package"
                self.outcome.notes.append("The branch has no pyproject.toml, so there is nothing to install or test.")
                return self.outcome
            pyproject = constraints.load_pyproject(self.repo_dir / "pyproject.toml")
            self._record_constraint(pyproject)
            self._create_environment()
            self._install()
            self._force_hier_config()
            library_python = self._verify()
            self._record_python_support(pyproject, library_python)
            self._import_module()
            self._setup()
            self._test()
            self.outcome.status = "passed"
        except StepFailed as failure:
            self.outcome.status = failure.status
            if failure.note:
                self.outcome.notes.append(failure.note)
        except Exception as error:
            self.outcome.status = "error"
            self.outcome.notes.append(f"The runner hit an unexpected error: {error!r}")
        finally:
            self._write_result()
        return self.outcome

    def _clone(self) -> None:
        if self.repo_dir.exists():
            shutil.rmtree(self.repo_dir)
        self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{self.app.repo}.git"
        auth_url = url
        if self.token:
            auth_url = f"https://x-access-token:{self.token}@github.com/{self.app.repo}.git"
        command = [
            "git", "clone", "--quiet", "--depth", "1", "--single-branch",
            "--branch", self.app.branch, auth_url, str(self.repo_dir),
        ]
        display = " ".join(command).replace(auth_url, url)
        step = self._step("clone", command, cwd=self.workdir, display=display)
        if step.returncode != 0:
            note = f"Could not clone branch '{self.app.branch}' of {self.app.repo}."
            if "not found" in step.output.lower():
                note += " The branch does not exist, or the repository needs a token."
            raise StepFailed("clone-failed", note)
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=self.repo_dir, capture_output=True, text=True, check=True,
        )
        self.outcome.commit = commit.stdout.strip()

    def _record_constraint(self, pyproject: dict[str, Any]) -> None:
        constraint = constraints.hier_config_constraint(pyproject)
        accepts = constraint.accepts(self.requested) if _is_version(self.requested) else None
        self.outcome.constraint = {
            "declared": constraint.declared,
            "kind": constraint.kind,
            "accepts": accepts,
        }
        if constraint.kind == "missing":
            self.outcome.notes.append("pyproject.toml does not declare hier-config as a dependency.")
        elif constraint.kind != "version":
            self.outcome.notes.append(
                f"hier-config comes from a {constraint.kind} source ({constraint.declared}); "
                f"this run replaced it with {self.requested}."
            )
        elif accepts is False:
            self.outcome.notes.append(
                f"The declared constraint `{constraint.declared}` rejects {self.requested}. "
                "A release must relax the constraint; this run forced the install."
            )

    def _create_environment(self) -> None:
        interpreter = resolve_python(self.app.python)
        self._require(
            self._step("python", ["poetry", "env", "use", interpreter]),
            "install-failed",
            f"Poetry could not create a Python {self.app.python} environment.",
        )
        info = subprocess.run(
            ["poetry", "env", "info", "--executable"],
            cwd=self.repo_dir, env=self.env, capture_output=True, text=True, check=True,
        )
        self.venv_python = info.stdout.strip()
        for key, value in self.app.env:
            self.env[key] = value.replace("{venv_python}", self.venv_python)

    def _install(self) -> None:
        step = self._step("install", self.app.install)
        if step.returncode != 0 and LOCK_HINT in step.output.lower():
            self.outcome.notes.append("poetry.lock was stale; the run regenerated it before the install.")
            self._require(self._step("lock", "poetry lock"), "install-failed", "poetry lock failed.")
            step = self._step("install", self.app.install)
        self._require(step, "install-failed", "The app's own install command failed before hier-config was replaced.")

    def _force_hier_config(self) -> None:
        spec = f"hier-config=={self.requested}" if _is_version(self.requested) else self.requested
        command = _installer_command(self.venv_python, spec)
        self._require(
            self._step("force-hier-config", command),
            "install-failed",
            f"Could not install hier-config {self.requested} into the app environment.",
        )

    def _verify(self) -> constraints.Constraint:
        step = self._step("verify", [self.venv_python, "-c", VERIFY_SCRIPT], display="python -c <verify hier-config metadata>")
        self._require(step, "install-failed", "hier-config metadata was not readable after the install.")
        line = next(line for line in step.output.splitlines() if line.startswith("VERIFY "))
        info = json.loads(line.removeprefix("VERIFY "))
        self.outcome.installed = info["version"]
        self.outcome.python = info["python"]
        if _is_version(self.requested) and info["version"] != self.requested:
            raise StepFailed(
                "version-mismatch",
                f"Expected hier-config {self.requested} but {info['version']} is installed.",
            )
        requires_python = info.get("requires_python") or "*"
        return constraints.Constraint(
            declared=requires_python, kind="version",
            specifier=constraints.poetry_to_pep440(requires_python),
        )

    def _record_python_support(self, pyproject: dict[str, Any], library_python: constraints.Constraint) -> None:
        app_python = constraints.python_constraint(pyproject)
        unsupported = constraints.unsupported_python_minors(app_python, library_python)
        if unsupported:
            self.outcome.notes.append(
                f"The app declares Python `{app_python.declared}` but hier-config {self.requested} "
                f"requires `{library_python.declared}`; Python {', '.join(unsupported)} can no longer resolve."
            )

    def _import_module(self) -> None:
        if not self.app.module:
            return
        self._require(
            self._step("import", [self.venv_python, "-c", f"import {self.app.module}"]),
            "import-failed",
            f"`import {self.app.module}` fails with hier-config {self.requested}.",
        )

    def _setup(self) -> None:
        for index, command in enumerate(self.app.setup, start=1):
            self._require(
                self._step(f"setup-{index}", command),
                "setup-failed",
                f"Setup command failed: {command}",
            )

    def _test(self) -> None:
        step = self._step("test", self.app.test)
        self.outcome.tests = parse_pytest_counts(step.output)
        self.outcome.failed_tests = PYTEST_FAILED_PATTERN.findall(step.output)
        if step.returncode != 0:
            summary = _tests_summary(self.outcome.tests)
            raise StepFailed("tests-failed", f"The test command exited with {step.returncode} ({summary}).")

    def _require(self, step: Step, status: str, note: str) -> None:
        if step.returncode != 0:
            raise StepFailed(status, note)

    def _step(
        self,
        name: str,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        display: str | None = None,
    ) -> Step:
        shown = display or (command if isinstance(command, str) else shlex.join(command))
        self._banner(f"[{self.app.short_name}] {name}: {shown}")
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            shell=isinstance(command, str),
            cwd=cwd or self.repo_dir,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        lines = []
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            lines.append(line)
        returncode = process.wait()
        step = Step(
            name=name, command=shown, returncode=returncode,
            seconds=time.monotonic() - started, output="".join(lines),
        )
        self.outcome.steps.append(step)
        print(f"--> {name}: {step.status} ({step.seconds:.0f}s)", flush=True)
        return step

    def _banner(self, text: str) -> None:
        print(f"\n=== {text}", flush=True)

    def _write_result(self) -> None:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"{self.app.short_name}.json"
        path.write_text(json.dumps(self.outcome.to_dict(), indent=2) + "\n")
        print(f"=== {self.app.repo}: {self.outcome.status} (result in {path})", flush=True)


def resolve_python(version: str) -> str:
    """Return the path of a Python interpreter for a version such as 3.12.

    Raises:
        RuntimeError: If no interpreter can be found or installed.
    """
    if shutil.which("uv"):
        found = _uv_find(version)
        if found is None:
            subprocess.run(["uv", "python", "install", version], check=False)
            found = _uv_find(version)
        if found:
            return found
    on_path = shutil.which(f"python{version}")
    if on_path:
        return on_path
    raise RuntimeError(f"no Python {version} interpreter found; install uv or python{version}")


def _uv_find(version: str) -> str | None:
    result = subprocess.run(
        ["uv", "python", "find", "--system", version],
        capture_output=True, text=True, check=False, env=_base_environment(),
    )
    return result.stdout.strip() or None


def parse_pytest_counts(output: str) -> dict[str, int]:
    """Return the counts from the last pytest summary line in output."""
    for line in reversed(output.splitlines()):
        matches = PYTEST_COUNT_PATTERN.findall(line)
        if matches and ("passed" in line or "failed" in line or "error" in line):
            return {name.rstrip("s") if name.startswith("error") else name: int(count) for count, name in matches}
    return {}


def _tests_summary(tests: dict[str, int] | None) -> str:
    if not tests:
        return "no pytest summary found"
    return ", ".join(f"{count} {name}" for name, count in tests.items())


def _is_version(requested: str) -> bool:
    return bool(VERSION_PATTERN.match(requested))


def _installer_command(venv_python: str, spec: str) -> list[str]:
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", venv_python, "--reinstall-package", "hier-config", spec]
    return [venv_python, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-cache-dir", spec]


def _base_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    env.update(
        POETRY_VIRTUALENVS_IN_PROJECT="true",
        POETRY_NO_INTERACTION="1",
        PYTHONUNBUFFERED="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env
