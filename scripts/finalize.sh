#!/usr/bin/env bash
# Completes the check run opened by prepare.sh. Runs even when earlier steps
# fail or are cancelled, so a required check never hangs in "In progress".
set -euo pipefail

out_dir="$RUNNER_TEMP/korza-review"
result="$out_dir/result.json"
summary_file="$out_dir/summary.md"

case "$REVIEW_OUTCOME:$VERDICT" in
  *:pass) conclusion=success ;;
  *:fail) conclusion=failure ;;
  cancelled:*) conclusion=cancelled ;;
  *) if [ "$FAIL_ON_ERROR" = "true" ]; then conclusion=failure; else conclusion=neutral; fi ;;
esac

if [ -s "$result" ] && { [ "$VERDICT" = "pass" ] || [ "$VERDICT" = "fail" ]; }; then
  title="$(jq -r '.verdict.reason' "$result")"
elif [ "$VERDICT" = "pass" ]; then
  title="Passed"
elif [ "$VERDICT" = "fail" ]; then
  title="Blocking findings"
elif [ "$conclusion" = "cancelled" ]; then
  title="Review cancelled"
else
  title="Review could not complete"
fi

if [ -s "$summary_file" ]; then
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
