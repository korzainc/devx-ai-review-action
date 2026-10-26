#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$(mktemp -d "$RUNNER_TEMP/korza-review.XXXXXX")"
control_dir="$(mktemp -d "$RUNNER_TEMP/korza-control.XXXXXX")"
engine_container="korza-${out_dir##*/}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
summary_file="$control_dir/summary.md"
# Private step output: finalization must read only this invocation's files.
# Keep the directory until later steps have uploaded any completed SARIF.
out output-dir "$out_dir"
out summary-file "$summary_file"
out result-file "$control_dir/result.json"
out engine-container "$engine_container"
out verdict error

incomplete() {
  local msg="$1"
  local detail="${2:-$msg}"
  printf '### AI Code Review: error\n\n%s\n' "$detail" > "$summary_file"
  cat "$summary_file" >> "$GITHUB_STEP_SUMMARY"
  if [ "$FAIL_ON_ERROR" = "true" ] || [ "${blocking_process_failure:-false}" = "true" ]; then
    echo "::error::$msg"
    exit 1
  fi
  echo "::warning::$msg The check reports neutral (fail-on-error is false)."
  exit 0
}

if [[ ! "$REVIEW_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  incomplete "Engine image must be pinned by SHA-256."
fi
if [[ ! "$REVIEW_TIMEOUT" =~ ^[1-9][0-9]{0,3}$ ]] || [ "$REVIEW_TIMEOUT" -gt 3600 ]; then
  incomplete "timeout-seconds must be 1 through 3600."
fi
REVIEW_IMAGE_DIGEST="sha256:${REVIEW_IMAGE##*@sha256:}"
export REVIEW_IMAGE_DIGEST

set +e
timeout --signal=TERM --kill-after=20 "$((REVIEW_TIMEOUT + 30))" \
env -u GITHUB_TOKEN -u GH_TOKEN -u REGISTRY_TOKEN docker run --rm --init --name "$engine_container" \
  --user "$(id -u):$(id -g)" \
  -v "$GITHUB_WORKSPACE:/workspace:ro" \
  -v "$GITHUB_EVENT_PATH:/github/event.json:ro" \
  -v "$out_dir:/github/out" \
  -w /workspace \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/workspace \
  -e ANTHROPIC_API_KEY \
  -e GITHUB_REPOSITORY -e GITHUB_EVENT_NAME -e GITHUB_SERVER_URL -e GITHUB_API_URL \
  -e GITHUB_RUN_ID -e GITHUB_RUN_ATTEMPT \
  -e GITHUB_EVENT_PATH=/github/event.json \
  -e PR_NUMBER -e BASE_SHA -e HEAD_SHA -e CHECK_RUN_ID \
  -e REVIEW_CONFIG -e REVIEW_SKILLS -e REVIEW_TIMEOUT \
  -e KORZA_CONTRACT_VERSION=1 -e REVIEW_IMAGE_DIGEST \
  -e REVIEW_OUTPUT_DIR=/github/out \
  "$REVIEW_IMAGE" action-review
engine_exit=$?
set -e
timeout --signal=TERM --kill-after=5 10 docker rm -f "$engine_container" > /dev/null 2>&1 || true

engine_result="$out_dir/result.json"
top_status="$(env -i PATH="$PATH" python3 "$script_dir/inspect.py" "$engine_result")"
if [ "$engine_exit" -ne 0 ]; then
  # A crash is never a completed review. Preserve the existing blocking gate
  # if this invocation left a fail result, without publishing its artifacts.
  # A valid fail or an oversized unknown result remains blocking on a crash.
  if [ "$top_status" = "fail" ] || [ "$top_status" = "oversized" ]; then
    blocking_process_failure=true
    out blocking-process-failure true
  fi
  incomplete "Engine exited with $engine_exit. The review could not complete."
fi

case "$top_status" in
  pass | fail) ;;
  error) incomplete "Engine reported an error. The review could not complete." ;;
  *) incomplete "Engine produced an invalid result. The review could not complete." ;;
esac
if [ "$top_status" = "fail" ]; then
  # Retain the blocking gate even if later validation or rendering fails.
  blocking_process_failure=true
  out blocking-process-failure true
fi
if ! completion_file="$(env -i PATH="$PATH" python3 "$script_dir/complete.py" \
  --output-dir "$out_dir" --control-dir "$control_dir" \
  --repository "$GITHUB_REPOSITORY" --pr-number "$PR_NUMBER" \
  --head-sha "$HEAD_SHA" --merge-base-sha "$BASE_SHA" --image "$REVIEW_IMAGE" \
  --github-run-id "$GITHUB_RUN_ID" --github-run-attempt "$GITHUB_RUN_ATTEMPT")"; then
  incomplete "Engine produced an invalid completed handoff. The review could not complete."
fi
result="$control_dir/result.json"
verdict="$top_status"
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
} > "$summary_file"
cat "$summary_file" >> "$GITHUB_STEP_SUMMARY"
out completion-file "$completion_file"
out review-completed true
out verdict "$verdict"
out findings "$findings"
if [ -f "$out_dir/results.sarif" ] && [ ! -L "$out_dir/results.sarif" ] &&
   [ -s "$out_dir/results.sarif" ]; then out sarif-file "$out_dir/results.sarif"; fi

case "$verdict" in
  pass) exit 0 ;;
  fail)
    echo "::error::AI Code Review reported blocking findings."
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
