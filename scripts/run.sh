#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$(mktemp -d "$RUNNER_TEMP/korza-review.XXXXXX")"
# Private step output: finalization must read only this invocation's files.
# Keep the directory until later steps have uploaded any completed SARIF.
out output-dir "$out_dir"
out verdict error

incomplete() {
  local msg="$1"
  local detail="${2:-$msg}"
  printf '### AI Code Review: error\n\n%s\n' "$detail" > "$out_dir/summary.md"
  cat "$out_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"
  if [ "$FAIL_ON_ERROR" = "true" ] || [ "${blocking_process_failure:-false}" = "true" ]; then
    echo "::error::$msg"
    exit 1
  fi
  echo "::warning::$msg The check reports neutral (fail-on-error is false)."
  exit 0
}

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
if [ "$engine_exit" -ne 0 ]; then
  # A crash is never a completed review. Preserve the existing blocking gate
  # if this invocation left a fail result, without publishing its artifacts.
  # Bound this exceptional read and do not follow engine-created symlinks.
  # Oversized output is refused conservatively, without parsing it on the host.
  if [ -f "$result" ] && [ ! -L "$result" ] && [ -s "$result" ]; then
    if [ "$(wc -c < "$result")" -gt 1048576 ]; then
      blocking_process_failure=true
      out blocking-process-failure true
      incomplete "Engine exited with $engine_exit and produced an oversized result. The review could not complete."
    fi
    if jq -e -s 'length == 1 and .[0].verdict.status == "fail"' "$result" > /dev/null 2>&1; then
      blocking_process_failure=true
      out blocking-process-failure true
    fi
  fi
  incomplete "Engine exited with $engine_exit. The review could not complete."
fi

if [ ! -f "$result" ] || [ -L "$result" ] || [ ! -s "$result" ]; then
  incomplete "Engine exited with $engine_exit and produced no usable result."
fi
if ! jq -e -s 'length == 1 and (.[0].verdict.status | . == "pass" or . == "fail" or . == "error")' "$result" > /dev/null 2>&1; then
  incomplete "Engine produced an invalid result. The review could not complete."
fi
verdict="$(jq -r '.verdict.status' "$result")"
if [ "$verdict" = "fail" ]; then
  # Retain the blocking gate even if later validation or rendering fails.
  blocking_process_failure=true
  out blocking-process-failure true
fi
if [ "$verdict" = "error" ]; then
  incomplete "Engine reported an error. The review could not complete." \
    "$(jq -r '.verdict.reason // "The review could not complete."' "$result")"
fi
if ! jq -e '.findings | type == "array"' "$result" > /dev/null 2>&1; then
  incomplete "Engine produced invalid findings. The review could not complete."
fi
findings="$(jq -r '.findings | length' "$result")"
finding_rows=""
if [ "$findings" -gt 0 ]; then
  # Render before writing a completed summary or publishing any artifacts.
  # shellcheck disable=SC2016
  if ! finding_rows="$(jq -r '.findings[] | "| \(.severity) | `\(.rule_id)` | `\(.location.path):\(.location.start_line)` | \(if .enforcing then "yes" else "no" end) |"' "$result")"; then
    incomplete "Engine produced invalid findings. The review could not complete."
  fi
fi

{
  echo "### AI Code Review: $verdict"
  echo
  jq -r '.verdict.reason' "$result"
  echo
  if [ "$findings" -gt 0 ]; then
    echo "| Severity | Rule | Location | Blocking |"
    echo "| --- | --- | --- | --- |"
    printf '%s\n' "$finding_rows"
  fi
} > "$out_dir/summary.md"
cat "$out_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"
out verdict "$verdict"
out findings "$findings"
if [ -s "$out_dir/results.sarif" ]; then out sarif-file "$out_dir/results.sarif"; fi

case "$verdict" in
  pass) exit 0 ;;
  fail)
    echo "::error::AI Code Review failed: $(jq -r '.verdict.reason' "$result")"
    exit 1
    ;;
  *)
    if [ "$FAIL_ON_ERROR" = "true" ]; then
      echo "::error::AI Code Review ended with verdict: $verdict"
      exit 1
    fi
    echo "::warning::AI Code Review ended with verdict: $verdict. The check reports neutral."
    exit 0
    ;;
esac
