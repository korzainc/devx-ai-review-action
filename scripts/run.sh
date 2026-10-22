#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$RUNNER_TEMP/korza-review"
mkdir -p "$out_dir"

set +e
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$GITHUB_WORKSPACE:/workspace:ro" \
  -v "$GITHUB_EVENT_PATH:/github/event.json:ro" \
  -v "$out_dir:/github/out" \
  -w /workspace \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/workspace \
  -e ANTHROPIC_API_KEY -e GITHUB_TOKEN \
  -e GITHUB_REPOSITORY -e GITHUB_EVENT_NAME -e GITHUB_SERVER_URL -e GITHUB_API_URL \
  -e GITHUB_RUN_ID -e GITHUB_RUN_ATTEMPT \
  -e GITHUB_EVENT_PATH=/github/event.json \
  -e PR_NUMBER -e BASE_SHA -e HEAD_SHA -e CHECK_RUN_ID \
  -e REVIEW_CONFIG -e REVIEW_SKILLS -e REVIEW_TIMEOUT \
  -e REVIEW_OUTPUT_DIR=/github/out \
  "$REVIEW_IMAGE"
engine_exit=$?
set -e

result="$out_dir/result.json"
if [ ! -s "$result" ]; then
  out verdict error
  msg="Engine exited with $engine_exit and produced no result."
  printf '%s\n' "$msg" > "$out_dir/summary.md"
  if [ "$FAIL_ON_ERROR" = "true" ]; then echo "::error::$msg"; exit 1; fi
  echo "::warning::$msg The check reports neutral (fail-on-error is false)."
  exit 0
fi

verdict="$(jq -r '.verdict.status' "$result")"
findings="$(jq -r '.findings | length' "$result")"
out verdict "$verdict"
out findings "$findings"
if [ -s "$out_dir/results.sarif" ]; then out sarif-file "$out_dir/results.sarif"; fi

{
  echo "### AI Code Review: $verdict"
  echo
  jq -r '.verdict.reason' "$result"
  echo
  if [ "$findings" -gt 0 ]; then
    echo "| Severity | Rule | Location | Blocking |"
    echo "| --- | --- | --- | --- |"
    # shellcheck disable=SC2016
    jq -r '.findings[] | "| \(.severity) | `\(.rule_id)` | `\(.location.path):\(.location.start_line)` | \(if .enforcing then "yes" else "no" end) |"' "$result"
  fi
} > "$out_dir/summary.md"
cat "$out_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"

case "$verdict" in
  pass) exit 0 ;;
  fail) echo "::error::AI Code Review failed: $(jq -r '.verdict.reason' "$result")"; exit 1 ;;
  *)
    if [ "$FAIL_ON_ERROR" = "true" ]; then echo "::error::AI Code Review ended with verdict: $verdict"; exit 1; fi
    echo "::warning::AI Code Review ended with verdict: $verdict. The check reports neutral."
    exit 0
    ;;
esac
