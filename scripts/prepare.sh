#!/usr/bin/env bash
# Decides whether to run, resolves the PR, opens the check run, and makes sure
# base and head commits are present locally. The engine assumes valid refs.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
skip() {
  echo "$1"
  out run false
  exit 0
}
event() { jq -r "$1" "$GITHUB_EVENT_PATH"; }

case "$GITHUB_EVENT_NAME" in
  issue_comment)
    [ -n "$(event '.issue.pull_request // empty')" ] || skip "Comment is not on a pull request. Skipping."

    first_line="$(event '.comment.body' | head -n1 | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    # GitHub's caller expression ignores string case. Match the same variants
    # here so an admitted command cannot cancel a review and then be skipped.
    shopt -s nocasematch
    case "$first_line" in
      "$TRIGGER_PHRASE" | "$TRIGGER_PHRASE "*) ;;
      *) skip "Comment does not start with $TRIGGER_PHRASE. Skipping." ;;
    esac
    shopt -u nocasematch

    assoc="$(event '.comment.author_association')"
    if [[ ",$ALLOWED_ASSOCIATIONS," != *",$assoc,"* ]]; then
      echo "::warning::$(event '.comment.user.login') ($assoc) is not allowed to trigger a review."
      out run false
      exit 0
    fi

    pr="$(event '.issue.number')"
    # Acknowledge the request on the comment. Best effort only.
    gh api -X POST "repos/$GITHUB_REPOSITORY/issues/comments/$(event '.comment.id')/reactions" \
      -f content=eyes > /dev/null 2>&1 || true
    ;;
  pull_request)
    pr="$(event '.pull_request.number')"
    # Fork PRs get a read-only token on pull_request, so no check can be posted.
    if [ "$(event '.pull_request.head.repo.full_name')" != "$GITHUB_REPOSITORY" ]; then
      skip "Fork PR: a maintainer can comment $TRIGGER_PHRASE to review it."
    fi
    ;;
  *)
    echo "::error::Unsupported event: $GITHUB_EVENT_NAME. Use issue_comment or pull_request."
    exit 1
    ;;
esac

pr_json="$(gh api "repos/$GITHUB_REPOSITORY/pulls/$pr")"
[ "$(jq -r '.state' <<< "$pr_json")" = "open" ] || skip "PR #$pr is not open. Skipping."
base_ref="$(jq -r '.base.ref' <<< "$pr_json")"
api_head_sha="$(jq -r '.head.sha' <<< "$pr_json")"

git rev-parse --is-inside-work-tree > /dev/null 2>&1 \
  || {
    echo "::error::No git checkout found. Add actions/checkout before this action."
    exit 1
  }

# CI checkouts are shallow by default, which breaks merge-base. Fix it here.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  git fetch --no-tags --quiet --unshallow origin
fi
# issue_comment checks out the default branch, not the PR. Fetch both sides explicitly.
git fetch --no-tags --quiet origin \
  "+refs/heads/$base_ref:refs/korza/base" \
  "+refs/pull/$pr/head:refs/korza/head"

head_sha="$(git rev-parse refs/korza/head)"
base_sha="$(git merge-base refs/korza/base refs/korza/head)"
if [ "$head_sha" != "$api_head_sha" ]; then
  echo "::warning::PR head moved during setup (${api_head_sha:0:7} to ${head_sha:0:7}). Reviewing ${head_sha:0:7}."
fi

# The check must name the exact fetched commit reviewed by the engine.
# A fetch failure creates no check against a different API-observed head.
run_url="$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID"
check_id="$(gh api -X POST "repos/$GITHUB_REPOSITORY/check-runs" \
  -f name="$CHECK_NAME" -f head_sha="$head_sha" -f status=in_progress \
  -f details_url="$run_url" \
  -f 'output[title]=Starting review' \
  -f "output[summary]=Review requested on PR #$pr." \
  --jq '.id')"
out check-run-id "$check_id"

out run true
out pr-number "$pr"
out base-sha "$base_sha"
out head-sha "$head_sha"
echo "Reviewing PR #$pr: ${base_sha:0:7}..${head_sha:0:7}"
