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

# The engine contract this action implements (mounts, env, result.json shape).
# The engine states the major version it implements in result.json, so a
# mismatched action/image pair fails loudly instead of misbehaving.
contract_version=1

# The engine can leave anything in out_dir, including a symlink to a runner
# file. Remove whatever is at the path before the host writes there.
write_summary() {
  rm -rf "$out_dir/summary.md"
  cat > "$out_dir/summary.md"
  cat "$out_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"
}

# Workflow commands are line based, so engine text must not start a new one.
# Escape the way the runner unescapes.
annotation() {
  local s="$1"
  s="${s//'%'/%25}"
  s="${s//$'\r'/%0D}"
  printf '%s' "${s//$'\n'/%0A}"
}

# The review could not complete. Write the error everywhere it is read from,
# then let fail-on-error (or a known blocking result) decide the step.
incomplete() {
  local msg="$1"
  local detail="${2:-$1}"
  printf '### AI Code Review: error\n\n%s\n' "$detail" | write_summary
  if [ "$FAIL_ON_ERROR" = "true" ] || [ "${blocking_process_failure:-false}" = "true" ]; then
    echo "::error::$msg"
    exit 1
  fi
  echo "::warning::$msg The check reports neutral (fail-on-error is false)."
  exit 0
}

# A result that says fail keeps the merge gate closed, whatever happens next.
mark_blocking() {
  blocking_process_failure=true
  out blocking-process-failure true
}

case "$REVIEW_TIMEOUT" in
  '' | *[!0-9]*)
    echo "::warning::timeout-seconds '$REVIEW_TIMEOUT' is not a number. Using 600."
    REVIEW_TIMEOUT=600
    ;;
esac
# Force base 10, or a leading zero reads as octal ("0600" is 384, "09" aborts).
REVIEW_TIMEOUT=$((10#$REVIEW_TIMEOUT))

# Pull before the clock starts, so a slow pull does not eat the engine's
# budget. An image already on the runner (built earlier in the job) is used as is.
# The pull gets its own limit, or a stalled registry would hold the check open.
pull_timeout=300
if ! docker image inspect "$REVIEW_IMAGE" > /dev/null 2>&1 \
  && ! timeout "$pull_timeout" docker pull --quiet "$REVIEW_IMAGE" > /dev/null; then
  incomplete "Could not pull engine image $REVIEW_IMAGE within ${pull_timeout}s."
fi

# The engine gets REVIEW_TIMEOUT to finish (or time out gracefully and still
# write a result) on its own; this wrapper is the backstop that keeps a hung
# engine from holding the job, and the check, until the 6 hour job limit.
# --init gives the container a PID 1 that actually dies on SIGTERM.
hard_timeout=$((REVIEW_TIMEOUT + 30))

start=$SECONDS
set +e
timeout --signal=TERM --kill-after=20 "$hard_timeout" \
  docker run --rm --init --name "$ENGINE_CONTAINER" \
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
  -e KORZA_CONTRACT_VERSION="$contract_version" \
  "$REVIEW_IMAGE"
engine_exit=$?
set -e
elapsed=$((SECONDS - start))
# When the kill-after path fires, timeout kills the docker client and the
# container outlives it. Remove it before reading what it wrote. (A cancelled
# job never gets here; the always() cleanup step in action.yml covers that.)
docker rm -f "$ENGINE_CONTAINER" > /dev/null 2>&1 || true

result="$out_dir/result.json"
# Only a regular file is read: never follow an engine-created symlink.
usable_result() { [ -f "$result" ] && [ ! -L "$result" ] && [ -s "$result" ]; }
# Exactly one JSON object, so a concatenated stream is never half trusted.
single_object() { jq -e -s 'length == 1 and (.[0] | type) == "object"' "$result" > /dev/null 2>&1; }
says_fail() { jq -e '.verdict.status == "fail"' "$result" > /dev/null 2>&1; }

if [ "$engine_exit" -ne 0 ]; then
  # 124: timeout sent TERM and the container exited. 137: the KILL fallback.
  # 137 also means a SIGKILL from elsewhere (the OOM killer, or the cleanup
  # step on a cancelled job), so only call it a timeout if the budget ran out.
  if { [ "$engine_exit" -eq 124 ] || [ "$engine_exit" -eq 137 ]; } && [ "$elapsed" -ge "$hard_timeout" ]; then
    msg="Engine timed out after ${elapsed}s (timeout-seconds $REVIEW_TIMEOUT plus 30s grace)."
  elif [ "$engine_exit" -eq 137 ]; then
    msg="Engine was killed after ${elapsed}s (exit 137, often out of memory)."
  else
    msg="Engine exited with $engine_exit after ${elapsed}s."
  fi
  msg="$msg The review could not complete."
  detail="$msg"
  # A crash is never a completed review, so nothing it wrote is published.
  # Still keep the blocking gate if it left a fail result, and show the
  # engine's own reason (a graceful timeout writes one on TERM). Oversized
  # output is refused conservatively, without parsing it on the host.
  if usable_result; then
    if [ "$(wc -c < "$result")" -gt 1048576 ]; then
      mark_blocking
      incomplete "$msg It left an oversized result, which is treated as blocking."
    fi
    if single_object; then
      if says_fail; then mark_blocking; fi
      reason="$(jq -r '.verdict.reason | strings' "$result" 2> /dev/null || true)"
      if [ -n "$reason" ]; then detail="$(printf '%s\n\nThe engine reported: %s' "$msg" "$reason")"; fi
    fi
  fi
  incomplete "$msg" "$detail"
fi

if ! usable_result; then
  incomplete "Engine exited with 0 after ${elapsed}s and produced no usable result."
fi
if ! single_object; then
  incomplete "Engine wrote an unreadable result.json. The review could not complete."
fi
# Retain the blocking gate even if a later check or rendering fails.
if says_fail; then mark_blocking; fi

engine_contract="$(jq -r '.contract_version // empty | tostring' "$result")"
if [ -z "$engine_contract" ]; then
  incomplete "Engine image does not declare a contract_version in result.json; this action implements contract $contract_version. Use the action release that ships this image, or an image built for contract $contract_version."
fi
if [ "${engine_contract%%.*}" != "$contract_version" ]; then
  incomplete "Engine image implements contract version $engine_contract; this action implements $contract_version. Use the action release that ships this image, or an image built for contract $contract_version."
fi

if ! jq -e '.verdict.status | . == "pass" or . == "fail" or . == "error"' "$result" > /dev/null 2>&1; then
  incomplete "Engine produced an invalid result. The review could not complete."
fi
verdict="$(jq -r '.verdict.status' "$result")"
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
reason="$(jq -r '.verdict.reason' "$result")"

{
  echo "### AI Code Review: $verdict"
  echo
  printf '%s\n' "$reason"
  echo
  if [ "$findings" -gt 0 ]; then
    echo "| Severity | Rule | Location | Blocking |"
    echo "| --- | --- | --- | --- |"
    printf '%s\n' "$finding_rows"
  fi
} | write_summary
out verdict "$verdict"
out findings "$findings"
sarif="$out_dir/results.sarif"
if [ -f "$sarif" ] && [ ! -L "$sarif" ] && [ -s "$sarif" ]; then out sarif-file "$sarif"; fi

if [ "$verdict" = "fail" ]; then
  echo "::error::AI Code Review failed: $(annotation "$reason")"
  exit 1
fi
