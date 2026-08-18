#!/usr/bin/env bash
#
# release_app.sh -- run the full release chain for one downstream app.
#
# Usage:
#   release_app.sh <owner/repo> <branch> <bump>
#
#   bump: major | minor | patch | prerelease
#
# Requires: gh (authenticated via GH_TOKEN), jq.
#
# The chain (all remote, via the app repo's own workflows):
#   1. Dispatch prepare-release.yml on <branch> with the requested bump.
#   2. Wait for that run; if it fails because the release branch/tag
#      already exists, report "already-prepared" and stop cleanly.
#   3. Locate the release/v* PR it opened and the draft release; take the
#      new version from the PR title ("chore(release): prepare X.Y.Z").
#   4. Squash-merge the PR (delete the release branch).
#   5. Publish the draft release (this tags the merged branch head and
#      triggers the app's release.yml -> PyPI).
#   6. Wait for that release.yml run and report the PyPI outcome.
#
# Emits exactly one machine-readable line on stdout:
#   RESULT|<repo>|<version>|<pr_url>|<release_url>|<status>
# Status values:
#   published        release published and PyPI upload succeeded
#   already-on-pypi  release published; PyPI rejected a duplicate file
#   already-prepared prepare-release found the release branch/tag exists
#   failed           anything else (details on stderr)
set -euo pipefail

POLL_INTERVAL=10
FIND_RUN_TIMEOUT=180 # seconds to wait for a dispatched run to appear
RUN_TIMEOUT=600      # seconds to wait for a run to complete

usage() {
  echo "usage: $0 <owner/repo> <branch> <bump>" >&2
  exit 2
}

[[ $# -eq 3 ]] || usage
REPO=$1
BRANCH=$2
BUMP=$3

case "$BUMP" in
  major | minor | patch | prerelease) ;;
  *)
    echo "error: invalid bump '$BUMP'" >&2
    usage
    ;;
esac

: "${GH_TOKEN:?GH_TOKEN must be set (PAT with repo+workflow scope)}"

VERSION=""
PR_URL=""
RELEASE_URL=""
RESULT_EMITTED=""

log() { echo "[$REPO] $*" >&2; }

emit() {
  printf 'RESULT|%s|%s|%s|%s|%s\n' \
    "$REPO" "${VERSION:-unknown}" "${PR_URL:-n/a}" "${RELEASE_URL:-n/a}" "$1"
  RESULT_EMITTED=1
}

on_exit() {
  local code=$?
  if [[ $code -ne 0 && -z $RESULT_EMITTED ]]; then
    emit failed
  fi
}
trap on_exit EXIT

# UTC timestamp <n> seconds ago (skew margin for createdAt comparisons).
# GNU date on runners; BSD fallback for local macOS use.
utc_ago() {
  local n=$1
  date -u -d "${n} seconds ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null ||
    date -u -v "-${n}S" +%Y-%m-%dT%H:%M:%SZ
}

# newest_run <workflow-file> <since-iso> <event> <branch-or-empty>
# Prints the databaseId of the newest matching run, or nothing.
newest_run() {
  local workflow=$1 since=$2 event=$3 branch=$4
  local args=(-R "$REPO" --workflow "$workflow" --event "$event" --limit 10
    --json databaseId,createdAt,event)
  [[ -n $branch ]] && args+=(--branch "$branch")
  gh run list "${args[@]}" | jq -r --arg since "$since" '
    map(select(.createdAt >= $since))
    | sort_by(.createdAt) | last | .databaseId // empty'
}

# wait_for_new_run <workflow-file> <since-iso> <event> <branch-or-empty>
# Prints the run id once one appears; fails after FIND_RUN_TIMEOUT.
wait_for_new_run() {
  local workflow=$1 since=$2 event=$3 branch=$4
  local deadline=$((SECONDS + FIND_RUN_TIMEOUT)) run_id=""
  while ((SECONDS < deadline)); do
    run_id=$(newest_run "$workflow" "$since" "$event" "$branch")
    if [[ -n $run_id ]]; then
      echo "$run_id"
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done
  log "timed out after ${FIND_RUN_TIMEOUT}s waiting for a $workflow run"
  return 1
}

# wait_for_run <run-id> -- sets RUN_CONCLUSION and RUN_URL.
# Fails only on timeout; a completed-but-failed run returns 0.
RUN_CONCLUSION=""
RUN_URL=""
wait_for_run() {
  local run_id=$1 deadline=$((SECONDS + RUN_TIMEOUT)) info status
  while ((SECONDS < deadline)); do
    info=$(gh run view "$run_id" -R "$REPO" --json status,conclusion,url)
    status=$(jq -r '.status' <<<"$info")
    RUN_URL=$(jq -r '.url' <<<"$info")
    if [[ $status == "completed" ]]; then
      RUN_CONCLUSION=$(jq -r '.conclusion' <<<"$info")
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done
  log "timed out after ${RUN_TIMEOUT}s waiting for run $run_id: $RUN_URL"
  return 1
}

run_log_matches() {
  local run_id=$1 pattern=$2
  gh run view "$run_id" -R "$REPO" --log-failed 2>/dev/null |
    grep -Eqi "$pattern"
}

# --- 1. Dispatch prepare-release.yml -----------------------------------
since=$(utc_ago 5)
log "dispatching prepare-release.yml on $BRANCH (bump=$BUMP)"
gh workflow run prepare-release.yml -R "$REPO" --ref "$BRANCH" \
  -f "bump=$BUMP"

# --- 2. Wait for the prepare run ---------------------------------------
prepare_id=$(wait_for_new_run prepare-release.yml "$since" \
  workflow_dispatch "$BRANCH")
log "prepare-release run: $prepare_id"
wait_for_run "$prepare_id"
if [[ $RUN_CONCLUSION != "success" ]]; then
  if run_log_matches "$prepare_id" 'already exists'; then
    log "release already prepared (branch/tag exists): $RUN_URL"
    emit already-prepared
    exit 0
  fi
  log "prepare-release failed ($RUN_CONCLUSION): $RUN_URL"
  exit 1
fi

# --- 3. Locate the release PR and draft release ------------------------
pr_info=$(gh pr list -R "$REPO" --base "$BRANCH" --state open \
  --json number,title,headRefName,createdAt,url --limit 20 |
  jq -r --arg since "$since" '
    map(select(.headRefName | startswith("release/v"))
        | select(.createdAt >= $since))
    | sort_by(.createdAt) | last // empty')
if [[ -z $pr_info ]]; then
  log "no open release/v* PR found against $BRANCH"
  exit 1
fi
pr_number=$(jq -r '.number' <<<"$pr_info")
PR_URL=$(jq -r '.url' <<<"$pr_info")
pr_title=$(jq -r '.title' <<<"$pr_info")
VERSION=${pr_title#chore(release): prepare }
if [[ ! $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
  log "cannot parse version from PR title: '$pr_title'"
  VERSION=""
  exit 1
fi
log "release PR #$pr_number -> version $VERSION"

release_info=$(gh release view "v$VERSION" -R "$REPO" --json isDraft,url)
RELEASE_URL=$(jq -r '.url' <<<"$release_info")
if [[ $(jq -r '.isDraft' <<<"$release_info") != "true" ]]; then
  log "release v$VERSION exists but is not a draft: $RELEASE_URL"
  exit 1
fi

# --- 4. Merge the release PR (before publishing, so the tag lands on ---
# --- the bumped branch head). Bot PRs get no CI runs; merge directly. --
log "squash-merging PR #$pr_number"
gh pr merge "$pr_number" -R "$REPO" --squash --delete-branch

# --- 5. Publish the draft release --------------------------------------
publish_since=$(utc_ago 5)
log "publishing release v$VERSION"
gh release edit "v$VERSION" -R "$REPO" --draft=false

# --- 6. Wait for release.yml (PyPI publish) ----------------------------
release_run=$(wait_for_new_run release.yml "$publish_since" release "")
log "release.yml run: $release_run"
wait_for_run "$release_run"
if [[ $RUN_CONCLUSION == "success" ]]; then
  log "PyPI publish succeeded: $RUN_URL"
  emit published
  exit 0
fi
if run_log_matches "$release_run" 'file already exists'; then
  log "PyPI already has this file (duplicate upload): $RUN_URL"
  emit already-on-pypi
  exit 0
fi
log "PyPI publish failed ($RUN_CONCLUSION): $RUN_URL"
exit 1
