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
| `apps.yml` | Declarative app registry (repo + branches). |
| `scripts/release_app.sh` | Full release chain for one app (gh + jq). |
| `.github/workflows/release-ecosystem.yml` | Orchestrator workflow. |
