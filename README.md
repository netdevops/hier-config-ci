# hier-config-ci

Ecosystem release orchestrator for the
[hier_config](https://github.com/netdevops/hier_config) downstream apps.
When hier_config publishes a release, this repo releases the whole app
ecosystem: it bumps each app's version, merges the release PR, publishes
the GitHub (pre)release, and reports the PyPI publish outcome. Read the
Docs rebuilds automatically from the new tags via its GitHub integration.

## Architecture

```text
netdevops/hier_config                netdevops/hier-config-ci
+---------------------+  repository  +--------------------------------+
| release published   |  dispatch    | release-ecosystem.yml          |
| (vX.Y.Z, prerelease | -----------> |  plan: read apps.yml, choose   |
| or stable)          |  event:      |        branch + bump, build    |
+---------------------+  hier-config |        job matrix              |
                         -release    |  release: one job per app      |
   (manual runs also                 |        (fail-fast: false)      |
    possible via                     |  summary: aggregate table      |
    workflow_dispatch)               +---------------+----------------+
                                                     |
                              scripts/release_app.sh | per app, via gh
                                                     v
              +--------------------------------------+---------------+
              | app repo (e.g. netdevops/hier-config-mcp)            |
              |                                                      |
              | 1. dispatch prepare-release.yml on the target branch |
              |    -> bumps version, pushes release/vX.Y.Z branch,   |
              |       opens PR, creates DRAFT release vX.Y.Z         |
              | 2. squash-merge the release PR                       |
              | 3. publish the draft release (tags branch head)      |
              | 4. release.yml (on: release published) uploads to    |
              |    PyPI using the app's PYPI_TOKEN secret            |
              | 5. Read the Docs builds the new tag automatically    |
              +------------------------------------------------------+
```

## End-to-end flow

1. hier_config's release workflow sends a `repository_dispatch` to this
   repo with event type `hier-config-release` and a payload of
   `{"version": "X.Y.Z", "prerelease": true|false}`.
2. `release-ecosystem.yml` maps the payload to a plan:
   - `prerelease: true` → channel **prerelease**, bump **prerelease**
     (each app's prerelease branch, e.g. `next`).
   - `prerelease: false` → channel **stable**, bump **patch** (each
     app's default branch).
3. The `plan` job reads `apps.yml` and emits a job matrix; the
   `release` job runs `scripts/release_app.sh` once per app with
   `fail-fast: false`, so one app's failure never blocks the others.
4. For each app the script drives the app repo's own workflows
   (dispatch `prepare-release.yml` → wait → merge the release PR →
   publish the draft release → wait for `release.yml`/PyPI). All
   per-app release logic lives in the app repos; this repo only
   orchestrates.
5. The `summary` job writes an app → version → PyPI status table to the
   run summary.

Each app job emits one machine-readable line the workflow aggregates:

```text
RESULT|<repo>|<version>|<pr_url>|<release_url>|<status>
```

Status values: `published`, `already-on-pypi`, `already-prepared`,
`failed`.

## Required secrets and setup

| Where | Secret | Purpose |
|-------|--------|---------|
| this repo | `ECOSYSTEM_GITHUB_TOKEN` | PAT with `repo` + `workflow` scope over the netdevops app repos. **Must belong to an org admin** — each app's `prepare-release.yml` has an admin-permission gate on the dispatching actor. The default `GITHUB_TOKEN` cannot cross repos; the workflow fails early with a clear error if this secret is missing. |
| netdevops/hier_config | `ECOSYSTEM_DISPATCH_TOKEN` (or the same PAT) | Token used to send the `repository_dispatch` to this repo (needs `repo` scope on hier-config-ci). |
| each app repo | `PYPI_TOKEN` | Used by the app's own `release.yml` to upload to PyPI. Must be an account-scoped token or a per-project token valid for that project. |

Read the Docs needs no secret: each app's RTD project follows the repo
via the GitHub integration and builds new tags automatically.

### Sending the dispatch from hier_config

Add a step to hier_config's release workflow (runs on
`release: published`):

```yaml
- name: Trigger ecosystem release
  env:
    GH_TOKEN: ${{ secrets.ECOSYSTEM_DISPATCH_TOKEN }}
  run: |
    gh api repos/netdevops/hier-config-ci/dispatches \
      -f event_type=hier-config-release \
      -F "client_payload[version]=${{ github.event.release.tag_name }}" \
      -F "client_payload[prerelease]=${{ github.event.release.prerelease }}"
```

## Running manually

Actions → **Release ecosystem** → *Run workflow*:

- `bump` — major | minor | patch | prerelease (default `patch`).
- `channel` — `stable` targets each app's default branch, `prerelease`
  targets its prerelease branch.
- `apps` — optional comma-separated filter; accepts short names
  (`hier-config-mcp`) or full `owner/repo`. Empty = all apps.

Example: rerun only the CLI app as a prerelease → bump `prerelease`,
channel `prerelease`, apps `hier-config-cli`.

## Integration testing

Before (and after) releasing, this repo can validate a hier-config build
against every downstream app: it clones each app at a chosen branch,
installs it, replaces its hier-config with the version under test, runs
the app's own test suite, and writes a Markdown report that defines what
works and what does not. The registry lives in
[`integration.yml`](integration.yml); the CLI lives in the
`hier-config-ci` package.

### Running locally

```bash
uv run hier-config-ci plan            # show the selection without running
uv run hier-config-ci test            # clone, install, test, and report
```

`test` writes one JSON result per app to `.integration/results/` and a
Markdown report to `.integration/report.md`, and exits non-zero if any
app failed.

Select the version and the branches:

```bash
# a specific hier-config build (any pip spec, e.g. a git URL or path)
uv run hier-config-ci test --hier-config 4.0.0b4

# one branch for every app
uv run hier-config-ci test --branch next

# per-app branches (repeatable or comma-separated)
uv run hier-config-ci test --branch hier-config-cli=next,hco-core=master

# a subset of apps
uv run hier-config-ci test hier-config-cli hier-config-mcp

# re-render a report from existing results
uv run hier-config-ci report .integration/results --output report.md
```

Each app's `python`, `install`, `setup`, `test`, and `env` can be
tuned in `integration.yml`; the `defaults:` block applies to every app.

### Running in GitHub Actions

Actions → **Integration test** → *Run workflow*:

- `hier-config` — version or pip spec under test; empty uses the
  `hier_config` value from `integration.yml`.
- `apps` — comma-separated filter; empty tests every app.
- `branches` — branch overrides (`next` for all, or
  `hier-config-cli=next`); empty uses each app's `branch` in
  `integration.yml`.

The workflow runs one job per app (`fail-fast: false`), then aggregates
the results into a Markdown report. The report is written to the run
summary and uploaded as the `integration-report` artifact; per-app result
JSONs are uploaded as `results-*` artifacts.

### Interpreting the report

The summary table marks each app ✅ passed, ❌ failed (with the step that
failed: install, import, setup, tests, or clone), or ➖ no Python
package. It also shows the app's declared hier-config pin and whether it
would accept the version under test — a common finding is a stale pin
(e.g. `^3.3.0`) that rejects a 4.x release even though the code and tests
pass once the newer build is forced in.

## Adding an app

1. Give the app repo the standard workflows on **all** release branches
   (default + prerelease): `prepare-release.yml`
   (`workflow_dispatch`, input `bump`) and `release.yml`
   (`on: release: published` → PyPI).
2. Add the `PYPI_TOKEN` secret to the app repo and connect Read the
   Docs.
3. Append one entry to [`apps.yml`](apps.yml):

```yaml
  - repo: netdevops/hier-config-new
    default_branch: main
    prerelease_branch: next
```

## Known caveats

- **Admin gate**: each app's `prepare-release.yml` checks that the
  dispatching actor is an org admin. `ECOSYSTEM_GITHUB_TOKEN` must be a
  PAT owned by an org admin or the dispatch is rejected.
- **CI does not run on bot-created PRs**: workflows are not triggered
  on the release PRs opened by `prepare-release.yml`, so the
  orchestrator merges them without waiting for checks — same as the
  manual flow. If an app enables required status checks on its release
  branches, the merge will be blocked and the app job will fail.
- **Merge-before-publish ordering**: the script always merges the
  release PR *before* publishing the draft release, so the tag is
  created on the bumped branch head. Publishing first would tag a
  commit without the version bump.
- **Idempotency**: predicting the next version without cloning is
  impractical, so the script detects reruns after the fact — if
  `prepare-release.yml` fails because the release branch/tag already
  exists, the app is reported as `already-prepared` (not a failure). A
  PyPI "File already exists" failure is reported as `already-on-pypi`.
- **Timeouts**: every wait is bounded — 3 minutes for a dispatched run
  to appear, 10 minutes for a run to complete. Nothing hangs forever.
- **Concurrency**: the workflow uses a `release-ecosystem` concurrency
  group so two ecosystem releases never run at once.

## Repository contents

| Path | Purpose |
|------|---------|
| `apps.yml` | Declarative app registry (repo + branches) for the release orchestrator. |
| `scripts/release_app.sh` | Full release chain for one app (gh + jq). |
| `.github/workflows/release-ecosystem.yml` | Orchestrator workflow. |
| `integration.yml` | Registry (apps, branches, commands) for the integration test. |
| `hier_config_ci/` | Integration CLI: `plan`, `test`, `report` subcommands. |
| `.github/workflows/integration.yml` | GitHub Actions integration test workflow. |
