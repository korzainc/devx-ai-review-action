#!/usr/bin/env bash
# A run id alone says nothing about what produced the handoff. Before its bytes are used,
# check the run is this workflow, in this repository, from a branch of this repository, on an
# event this action actually reviews on.
#
# This is not the whole check: attest-republish separately pins the pull request, the reviewed
# commit, the engine image and the digests of the bytes themselves.
set -euo pipefail

: "${GITHUB_OUTPUT:?}"
for name in GITHUB_REPOSITORY GITHUB_WORKFLOW_REF SOURCE_RUN_ID GH_TOKEN; do
  if [ -z "${!name:-}" ]; then
    echo "::error::Cannot check the handoff's source run without prepared host context."
    exit 1
  fi
done

case "$SOURCE_RUN_ID" in
  '' | *[!0-9]*)
    echo "::error::The source run id must be a number."
    exit 1
    ;;
esac

# owner/repo/.github/workflows/<file>@<ref> -> .github/workflows/<file>
workflow_path="${GITHUB_WORKFLOW_REF%@*}"
workflow_path="${workflow_path#"$GITHUB_REPOSITORY"/}"

if ! run="$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$SOURCE_RUN_ID" \
  --jq '[.repository.full_name, .head_repository.full_name, .path, .event, .head_branch] | @tsv')"; then
  echo "::error::Could not read the handoff's source run."
  exit 1
fi
IFS=$'\t' read -r run_repo head_repo run_path run_event run_branch <<< "$run"

fail() {
  # The values are the repository's own, not a secret, but keep the reason fixed.
  echo "::error::The handoff's source run is not one of this workflow's: $1."
  exit 1
}

[ "$run_repo" = "$GITHUB_REPOSITORY" ] || fail "another repository"
# A fork's pull_request run writes artifacts into this repository. Its branch is not ours.
[ "$head_repo" = "$GITHUB_REPOSITORY" ] || fail "a fork's branch"
[ "$run_path" = "$workflow_path" ] || fail "another workflow"
case "$run_event" in
  issue_comment | pull_request | workflow_dispatch) ;;
  *) fail "an event this action does not review on" ;;
esac

echo "Handoff source run $SOURCE_RUN_ID: $run_path on $run_branch ($run_event)."
echo "source-run-branch=$run_branch" >> "$GITHUB_OUTPUT"
