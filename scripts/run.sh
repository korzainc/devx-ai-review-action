#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$RUNNER_TEMP/korza-review"
mkdir -p "$out_dir"

# The engine contract this action implements (mounts, env, result.json shape).
# The engine states the major version it implements in result.json, so a
# mismatched action/image pair fails loudly instead of misbehaving.
contract_version=1

# The review could not complete. Write the error everywhere it is read from,
# then let fail-on-error decide whether that fails the step.
engine_error() {
  out verdict error
  printf '%s\n' "$1" > "$out_dir/summary.md"
  if [ "$FAIL_ON_ERROR" = "true" ]; then
    echo "::error::$1"
    exit 1
  fi
  echo "::warning::$1 The check reports neutral (fail-on-error is false)."
  exit 0
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
  engine_error "Could not pull engine image $REVIEW_IMAGE within ${pull_timeout}s."
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
if [ ! -s "$result" ]; then
  # 124: timeout sent TERM and the container exited. 137: the KILL fallback.
  # 137 also means a SIGKILL from elsewhere (the OOM killer, or the cleanup
  # step on a cancelled job), so only call it a timeout if the budget ran out.
  if { [ "$engine_exit" -eq 124 ] || [ "$engine_exit" -eq 137 ]; } && [ "$elapsed" -ge "$hard_timeout" ]; then
    engine_error "Engine timed out: no result after ${elapsed}s (timeout-seconds $REVIEW_TIMEOUT plus 30s grace)."
  fi
  if [ "$engine_exit" -eq 137 ]; then
    engine_error "Engine was killed after ${elapsed}s and produced no result (exit 137, often out of memory)."
  fi
  engine_error "Engine exited with $engine_exit after ${elapsed}s and produced no result."
fi

# A TERM can land mid-write. Report an unreadable result like any engine error
# rather than letting jq fail the step under set -e.
if ! jq -e 'type == "object"' "$result" > /dev/null 2>&1; then
  engine_error "Engine exited with $engine_exit and wrote an unreadable result.json."
fi

engine_contract="$(jq -r '.contract_version // empty' "$result")"
if [ -z "$engine_contract" ]; then
  engine_error "Engine image does not declare a contract_version in result.json; this action implements contract $contract_version. Use the action release that ships this image, or an image built for contract $contract_version."
fi
if [ "${engine_contract%%.*}" != "$contract_version" ]; then
  engine_error "Engine image implements contract version $engine_contract; this action implements $contract_version. Use the action release that ships this image, or an image built for contract $contract_version."
fi

# Checked after the contract version, since another major may change the shape.
# A wrong shape would otherwise fail jq below, possibly after verdict is set.
if ! jq -e '(.verdict | type) == "object" and (.verdict.status | type) == "string"
  and (.findings | type) == "array"' "$result" > /dev/null 2>&1; then
  engine_error "Engine wrote a result.json without a verdict.status string and a findings array."
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
