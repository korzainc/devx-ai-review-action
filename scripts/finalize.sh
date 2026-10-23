#!/usr/bin/env bash
# Completes the check run opened by prepare.sh. Runs even when earlier steps
# fail or are cancelled, so a required check never hangs in "In progress".
set -euo pipefail

out_dir="${OUTPUT_DIR:-}"
result="$out_dir/result.json"
summary_file="$out_dir/summary.md"

case "$REVIEW_OUTCOME:$VERDICT" in
  cancelled:*) conclusion=cancelled ;;
  success:pass) conclusion=success ;;
  success:fail | failure:fail) conclusion=failure ;;
  *)
    if [ "${BLOCKING_PROCESS_FAILURE:-false}" = "true" ] || [ "$FAIL_ON_ERROR" = "true" ]; then
      conclusion=failure
    else
      conclusion=neutral
    fi
    ;;
esac

completed=false
case "$REVIEW_OUTCOME:$VERDICT" in
  success:pass | success:fail | failure:fail) completed=true ;;
esac

if [ "$conclusion" = "cancelled" ]; then
  title="Review cancelled"
elif [ "$completed" = "true" ]; then
  if [ -n "$out_dir" ] && [ -f "$result" ] && [ ! -L "$result" ] && [ -s "$result" ]; then
    title="$(jq -r '.verdict.reason' "$result")"
  elif [ "$VERDICT" = "pass" ]; then
    title="Passed"
  else
    title="Blocking findings"
  fi
else
  title="Review could not complete"
fi

if [ "$conclusion" = "cancelled" ]; then
  summary="The review was cancelled. See the workflow run logs."
elif [ "$completed" = "false" ] && { [ "$VERDICT" = "pass" ] || [ "$VERDICT" = "fail" ]; }; then
  summary="The review could not complete. See the workflow run logs."
elif [ -n "$out_dir" ] && [ -f "$summary_file" ] && [ ! -L "$summary_file" ] && [ -s "$summary_file" ]; then
  summary="$(cat "$summary_file")"
else
  summary="The review did not produce a result. See the workflow run logs."
fi
summary="${summary:0:60000}"

jq -n --arg c "$conclusion" --arg t "$title" --arg s "$summary" \
  --arg u "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
  '{status:"completed",conclusion:$c,details_url:$u,output:{title:$t,summary:$s}}' \
  | gh api -X PATCH "repos/$GITHUB_REPOSITORY/check-runs/$CHECK_RUN_ID" --input - > /dev/null

echo "Check run completed: $conclusion"
