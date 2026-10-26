#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$(mktemp -d "$RUNNER_TEMP/korza-review.XXXXXX")"
control_dir="$(mktemp -d "$RUNNER_TEMP/korza-control.XXXXXX")"
engine_container="${ENGINE_CONTAINER:-korza-${out_dir##*/}}"
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
# Preserve upstream fallback and base-10 leading-zero normalization. Bound the
# significant digits before arithmetic so an oversized integer cannot wrap.
case "$REVIEW_TIMEOUT" in
  '' | *[!0-9]*)
    echo "::warning::timeout-seconds is not a number. Using 600."
    REVIEW_TIMEOUT=600
    ;;
esac
timeout_digits="${REVIEW_TIMEOUT#"${REVIEW_TIMEOUT%%[!0]*}"}"
if [ -z "$timeout_digits" ] || [ "${#timeout_digits}" -gt 4 ]; then
  incomplete "timeout-seconds must be 1 through 3600."
fi
REVIEW_TIMEOUT=$((10#$timeout_digits))
if [ "$REVIEW_TIMEOUT" -gt 3600 ]; then
  incomplete "timeout-seconds must be 1 through 3600."
fi
export REVIEW_TIMEOUT
REVIEW_IMAGE_DIGEST="sha256:${REVIEW_IMAGE##*@sha256:}"
export REVIEW_IMAGE_DIGEST

# Pull has an independent bound; do not spend the engine's budget on registry I/O.
if ! docker image inspect "$REVIEW_IMAGE" > /dev/null 2>&1 \
  && ! timeout 300 docker pull --quiet "$REVIEW_IMAGE" > /dev/null; then
  incomplete "Could not pull engine image within 300s."
fi
hard_timeout=$((REVIEW_TIMEOUT + 30))
start=$SECONDS
set +e
timeout --signal=TERM --kill-after=20 "$hard_timeout" \
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
elapsed=$((SECONDS - start))
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
  if { [ "$engine_exit" -eq 124 ] || [ "$engine_exit" -eq 137 ]; } && [ "$elapsed" -ge "$hard_timeout" ]; then
    msg="Engine timed out after ${elapsed}s (timeout-seconds $REVIEW_TIMEOUT plus 30s grace)."
  elif [ "$engine_exit" -eq 137 ]; then
    msg="Engine was killed after ${elapsed}s (exit 137, often out of memory)."
  else
    msg="Engine exited with $engine_exit after ${elapsed}s."
  fi
  msg="$msg The review could not complete."
  detail="$msg"
  reason="$(env -i PATH="$PATH" python3 "$script_dir/inspect.py" "$engine_result" --reason)"
  if [ -n "$reason" ]; then detail="$(printf '%s\n\nThe engine reported: %s' "$msg" "$reason")"; fi
  incomplete "$msg" "$detail"
fi

case "$top_status" in
  pass | fail) ;;
  error)
    reason="$(env -i PATH="$PATH" python3 "$script_dir/inspect.py" "$engine_result" --reason)"
    incomplete "Engine reported an error. The review could not complete." "${reason:-The review could not complete.}"
    ;;
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
  incomplete "Engine produced an invalid completed handoff or unsupported action contract. The review could not complete."
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
    # Workflow commands are line based; preserve the reason as one escaped value.
    reason="$(jq -r '.verdict.reason' "$result")"
    reason="${reason//'%'/%25}"
    reason="${reason//$'\r'/%0D}"
    reason="${reason//$'\n'/%0A}"
    echo "::error::AI Code Review failed: $reason"
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
