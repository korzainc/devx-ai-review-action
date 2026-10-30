#!/usr/bin/env bash
# Completes the check run opened by prepare.sh. Runs even when earlier steps
# fail or are cancelled, so a required check never hangs in "In progress".
set -euo pipefail

result="${RESULT_FILE:-}"
summary_file="${SUMMARY_FILE:-}"

case "${JOB_CANCELLED:-false}:$REVIEW_OUTCOME" in
  true:* | *:cancelled) conclusion=cancelled ;;
  *)
    if [ "${REVIEW_COMPLETED:-false}" = "true" ] \
      && { [ "${PUBLISH_OUTCOME:-}" != "success" ] || [ "${DELIVERY:-}" != "success" ]; }; then
      conclusion=failure
    elif [ "$REVIEW_OUTCOME:$VERDICT" = "success:pass" ] \
      && [ "${REVIEW_COMPLETED:-false}" = "true" ]; then
      conclusion=success
    elif [ "$VERDICT" = "fail" ] && [ "${REVIEW_COMPLETED:-false}" = "true" ]; then
      conclusion=failure
    elif [ "${BLOCKING_PROCESS_FAILURE:-false}" = "true" ] || [ "$FAIL_ON_ERROR" = "true" ]; then
      conclusion=failure
    else
      conclusion=neutral
    fi
    ;;
esac

# Completed only if the step outcome agrees with the verdict it reported, so a
# step that failed after writing pass never shows a passing title or summary.
completed=false
if [ "${REVIEW_COMPLETED:-false}" = "true" ]; then
  case "$REVIEW_OUTCOME:$VERDICT" in
    success:pass | success:fail | failure:fail) completed=true ;;
  esac
fi

if [ "$conclusion" = "cancelled" ]; then
  title="Review cancelled"
elif [ "$completed" = "true" ] && [ "${DELIVERY:-}" != "success" ]; then
  title="Review delivery could not be verified"
elif [ "$completed" = "true" ]; then
  if [ -n "$result" ] && [ -f "$result" ] && [ ! -L "$result" ] && [ -s "$result" ]; then
    title="$(jq -r '.verdict.reason' "$result")"
  elif [ "$VERDICT" = "pass" ]; then
    title="Passed"
  else
    title="Blocking findings"
  fi
else
  title="Review could not complete"
fi

# On cancel, run.sh may still be writing a summary as the engine is torn down,
# and it would describe the teardown, not the cancellation.
if [ "$conclusion" = "cancelled" ]; then
  summary="The review was cancelled. See the workflow run logs."
elif [ "$completed" = "false" ] && { [ "$VERDICT" = "pass" ] || [ "$VERDICT" = "fail" ]; }; then
  summary="The review could not complete. See the workflow run logs."
elif [ -n "$summary_file" ] && [ -f "$summary_file" ] && [ ! -L "$summary_file" ] && [ -s "$summary_file" ]; then
  summary="$(cat "$summary_file")"
else
  summary="The review did not produce a result. See the workflow run logs."
fi
summary="${summary:0:60000}"
if [ "$completed" = "true" ] && [ "${DELIVERY:-}" != "success" ]; then
  summary="$(printf 'Review delivery could not be verified; comments may have been posted. Do not retry automatically.\n\n%s' "$summary")"
fi

jq -n --arg c "$conclusion" --arg t "$title" --arg s "$summary" \
  --arg u "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
  '{status:"completed",conclusion:$c,details_url:$u,output:{title:$t,summary:$s}}' \
  | gh api -X PATCH "repos/$GITHUB_REPOSITORY/check-runs/$CHECK_RUN_ID" --input - > /dev/null

echo "Check run completed: $conclusion"
