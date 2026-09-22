"""Command line entry point for the integration tooling."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from hier_config_ci import registry as registry_module
from hier_config_ci import report as report_module
from hier_config_ci.runner import AppRunner

DEFAULT_REGISTRY = Path("integration.yml")
DEFAULT_WORKDIR = Path(".integration")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch to a subcommand."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as error:
        parser.error(str(error))
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hier-config-ci",
        description="Test the hier_config app ecosystem against one hier-config build.",
    )
    subparsers = parser.add_subparsers(required=True)

    plan = subparsers.add_parser("plan", help="show the apps and branches that a run would test")
    _add_selection_arguments(plan)
    plan.add_argument("--json", action="store_true", help="print a JSON matrix for GitHub Actions")
    plan.set_defaults(func=_plan)

    test = subparsers.add_parser("test", help="clone, install, and test the selected apps")
    _add_selection_arguments(test)
    test.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR, help="clone and result directory")
    test.add_argument("--results", type=Path, help="result JSON directory (default: <workdir>/results)")
    test.add_argument("--report", type=Path, help="Markdown report path (default: <workdir>/report.md)")
    test.add_argument("--no-report", action="store_true", help="do not write the Markdown report")
    test.set_defaults(func=_test)

    report = subparsers.add_parser("report", help="render result JSON files as a Markdown report")
    report.add_argument("results", type=Path, help="directory with result JSON files")
    report.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    report.add_argument("--hier-config", help="version label for the report title")
    report.add_argument("--output", type=Path, help="write the report to this file")
    report.set_defaults(func=_report)
    return parser


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("apps", nargs="*", help="app names to include; default is every app")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY, help="registry YAML file")
    parser.add_argument("--hier-config", help="hier-config version, or a pip spec such as a git URL or path")
    parser.add_argument(
        "--branch", action="append", default=[],
        help="branch override: 'next' for every app, or 'hier-config-cli=next'; repeatable or comma-separated",
    )


def _selection(args: argparse.Namespace) -> tuple[str, list[registry_module.App]]:
    registry = registry_module.load_registry(args.registry)
    overrides = registry_module.parse_branch_overrides(args.branch)
    apps = registry_module.select_apps(registry, args.apps, overrides)
    return args.hier_config or registry.hier_config, apps


def _plan(args: argparse.Namespace) -> int:
    requested, apps = _selection(args)
    if args.json:
        matrix = [{"repo": app.repo, "branch": app.branch, "python": app.python} for app in apps]
        print(json.dumps({"hier_config": requested, "include": matrix}))
        return 0
    print(f"hier-config: {requested}")
    for app in apps:
        print(f"  {app.repo:45} {app.branch:12} python {app.python}")
    return 0


def _test(args: argparse.Namespace) -> int:
    requested, apps = _selection(args)
    results_dir = args.results or args.workdir / "results"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    outcomes = [
        AppRunner(app, requested, args.workdir, results_dir, token).run().to_dict()
        for app in apps
    ]
    if not args.no_report:
        report_path = args.report or args.workdir / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_module.render_markdown(outcomes, requested))
        print(f"\nReport written to {report_path}")
    print("\nSummary:")
    for outcome in outcomes:
        print(f"  {outcome['repo']:45} {outcome['branch']:12} {outcome['status']}")
    return 1 if any(report_module.is_failure(outcome) for outcome in outcomes) else 0


def _report(args: argparse.Namespace) -> int:
    outcomes = report_module.load_outcomes(args.results)
    requested = args.hier_config or (
        outcomes[0]["requested"] if outcomes else registry_module.load_registry(args.registry).hier_config
    )
    markdown = report_module.render_markdown(outcomes, requested)
    if args.output:
        args.output.write_text(markdown)
    else:
        sys.stdout.write(markdown)
    return 1 if any(report_module.is_failure(outcome) for outcome in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
