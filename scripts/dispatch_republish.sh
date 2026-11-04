#!/usr/bin/env bash
# Dispatch this same workflow to post what GitHub's rate limit left unposted, so the author
# does not have to ask for it. ATTEMPT counts the follow-ups already made and travels in the
# dispatch, so a pull request that keeps hitting the limit stops at MAX_ATTEMPTS instead of
# dispatching forever. Never fails the job: a missed follow-up is recoverable by the next
# review, a failed step is not.
set -euo pipefail

give_up() {
  echo "$1"
  exit 0
}

: "${GITHUB_OUTPUT:?}"
for name in GITHUB_REPOSITORY GITHUB_WORKFLOW_REF GITHUB_RUN_ID GITHUB_RUN_ATTEMPT \
  PR_NUMBER DEFERRED MAX_ATTEMPTS GH_TOKEN; do
  if [ -z "${!name:-}" ]; then
    echo "::warning::Cannot dispatch a follow-up without prepared host context."
    exit 0
  fi
done

case "$DEFERRED" in
  '' | *[!0-9]*) give_up "No deferred count to act on." ;;
esac
[ "$DEFERRED" -gt 0 ] || give_up "Nothing was deferred. No follow-up needed."

case "$MAX_ATTEMPTS" in
  '' | *[!0-9]*)
    echo "::warning::max-republish-attempts must be a whole number."
    exit 0
    ;;
esac
attempt=$((${ATTEMPT:-0} + 1))
if [ "$attempt" -gt "$MAX_ATTEMPTS" ]; then
  echo "::warning::$DEFERRED finding(s) still unposted after $MAX_ATTEMPTS follow-up attempts. Comment the trigger phrase to try again."
  exit 0
fi

# owner/repo/.github/workflows/<file>@refs/heads/<branch>: dispatch the same workflow file,
# on the ref it is running from, so a republish runs the version that reviewed. Under a
# reusable workflow this names the caller, which is the file that can be dispatched.
case "$GITHUB_WORKFLOW_REF" in
  "$GITHUB_REPOSITORY"/*) ;;
  *) give_up "This workflow belongs to another repository; skipping the follow-up." ;;
esac
workflow_path="${GITHUB_WORKFLOW_REF%@*}"
workflow_file="${workflow_path##*/}"
# The dispatch API takes a branch or tag name, not a full ref.
workflow_ref="${GITHUB_WORKFLOW_REF#*@}"
workflow_ref="${workflow_ref#refs/heads/}"
workflow_ref="${workflow_ref#refs/tags/}"
case "$workflow_file" in
  '' | *[!A-Za-z0-9._-]*) give_up "Cannot name this workflow file; skipping the follow-up." ;;
esac
case "$workflow_ref" in
  '' | refs/*) give_up "Cannot name this workflow's branch; skipping the follow-up." ;;
esac

if gh workflow run "$workflow_file" --ref "$workflow_ref" \
  -f pr="$PR_NUMBER" \
  -f source-run-id="$GITHUB_RUN_ID" \
  -f source-run-attempt="$GITHUB_RUN_ATTEMPT" \
  -f attempt="$attempt"; then
  echo "dispatched-attempt=$attempt" >> "$GITHUB_OUTPUT"
  echo "Dispatched follow-up $attempt of $MAX_ATTEMPTS to post $DEFERRED deferred finding(s)."
else
  echo "::warning::Could not dispatch a follow-up for $DEFERRED deferred finding(s). The next review will post them."
fi
