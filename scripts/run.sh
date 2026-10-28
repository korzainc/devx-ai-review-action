#!/usr/bin/env bash
# Runs the engine image against the checked-out repo and maps its result to
# step outputs, a summary, and an exit code. The check run is completed by
# finalize.sh, so it closes even if this step crashes. The engine never gets a
# GitHub token: publish.sh posts the review only after this step completes it.
set -euo pipefail

out() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }
out_dir="$(mktemp -d "$RUNNER_TEMP/korza-review.XXXXXX")"
# Host-private files live outside anything the engine can write.
control_dir="$(mktemp -d "$RUNNER_TEMP/korza-control.XXXXXX")"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
summary_file="$control_dir/summary.md"
# Private step output: finalization must read only this invocation's files.
# Keep the directory until later steps have uploaded any completed SARIF.
out output-dir "$out_dir"
out summary-file "$summary_file"
out result-file "$control_dir/result.json"
out verdict error

# The engine contract this action implements (mounts, env, result.json shape).
# The engine states the version it implements in result.json, so a
# mismatched action/image pair fails loudly instead of misbehaving.
contract_version=1

# The review could not complete. Write the error everywhere it is read from,
# then let fail-on-error (or a known blocking result) decide the step.
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

# A result that says fail keeps the merge gate closed, whatever happens next.
mark_blocking() {
  blocking_process_failure=true
  out blocking-process-failure true
}

contract_mismatch="Engine image does not implement contract version $contract_version in result.json. Use the action release that ships this image, or an image built for contract $contract_version."

if [[ ! "$REVIEW_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
  incomplete "Engine image must be pinned by SHA-256."
fi
REVIEW_IMAGE_DIGEST="sha256:${REVIEW_IMAGE##*@sha256:}"
export REVIEW_IMAGE_DIGEST

case "$REVIEW_TIMEOUT" in
  '' | *[!0-9]*)
    echo "::warning::timeout-seconds '$REVIEW_TIMEOUT' is not a number. Using 600."
    REVIEW_TIMEOUT=600
    ;;
esac
# Bound significant digits before Bash arithmetic can wrap an oversized value.
# Strip leading zeros first to preserve base-10 inputs such as "0600" and "09".
timeout_digits="${REVIEW_TIMEOUT#"${REVIEW_TIMEOUT%%[!0]*}"}"
if [ -z "$timeout_digits" ] || [ "${#timeout_digits}" -gt 4 ]; then
  incomplete "timeout-seconds must be 1 through 3600."
fi
REVIEW_TIMEOUT=$((10#$timeout_digits))
if [ "$REVIEW_TIMEOUT" -gt 3600 ]; then
  incomplete "timeout-seconds must be 1 through 3600."
fi

# Pull before the clock starts, so a slow pull does not eat the engine's
# budget. An image already on the runner (built earlier in the job) is used as is.
# The pull gets its own limit, or a stalled registry would hold the check open.
# The publisher later runs the same image with --pull=never.
pull_timeout=300
if ! docker image inspect "$REVIEW_IMAGE" > /dev/null 2>&1 \
  && ! timeout "$pull_timeout" docker pull --quiet "$REVIEW_IMAGE" > /dev/null; then
  incomplete "Could not pull engine image $REVIEW_IMAGE. Check that this repo has read access to the package (or pass registry-token); a pull also gives up after ${pull_timeout}s."
fi

# What earlier reviews left on the PR, so a re-run skips unchanged files and doesn't repeat
# findings (engine contract: REVIEW_PRIOR). Fetched here with the job token, read-only; the
# engine container gets the file, never the token. Any failure leaves it out: the engine then
# reviews every file, and the publisher still skips exact repeats.
changed_files() {
  # Files whose content differs between the last reviewed commit ($1) and the head, as
  # snapshots: GitHub's compare when the head builds on it; after a force push or rebase, a
  # second compare from the merge base, matched by blob hash. Writes nothing when GitHub
  # can't list every file (300+); fails when a commit is gone. Either way: review every file.
  local last="$1" out="$2" base
  gh api "repos/$GITHUB_REPOSITORY/compare/$last...$HEAD_SHA" > "$out.head" || return 1
  base="$(jq -r '.merge_base_commit.sha // ""' "$out.head")"
  if [ "$base" = "$last" ]; then
    echo '{"files": []}' > "$out.last"
  else
    gh api "repos/$GITHUB_REPOSITORY/compare/$base...$last" > "$out.last" || return 1
  fi
  jq -n -c --arg last "$last" --slurpfile h "$out.head" --slurpfile l "$out.last" '
    def blobs: [.files[]?
      | (if .previous_filename then {key: .previous_filename, value: null} else empty end),
        {key: .filename, value: (if .status == "removed" then null else .sha end)}]
      | from_entries;
    if ([$h[0].files[]?] | length) >= 300 or ([$l[0].files[]?] | length) >= 300 then empty
    else ($h[0] | blobs) as $head | ($l[0] | blobs) as $was
      | {base: $last, status: $h[0].status, snapshot_diff: true,
         files: [($head + $was) | keys[] as $f
           | select((($head | has($f)) and ($was | has($f)) and $head[$f] == $was[$f]) | not)
           | $f]}
    end' > "$out"
}
fetch_prior() {
  # Via files, not arguments: a busy PR's comments exceed the OS limit on one argument.
  local dir review issue changed last
  dir="$(dirname -- "$1")"
  review="$dir/prior-review.json" issue="$dir/prior-issue.json" changed="$dir/prior-changed.json"
  echo '{}' > "$changed"
  gh api "repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER/comments" --paginate --slurp > "$review" || return 1
  gh api "repos/$GITHUB_REPOSITORY/issues/$PR_NUMBER/comments" --paginate --slurp > "$issue" || return 1
  # The last reviewed commit, from the hidden state line in the bot's summary comment.
  last="$(jq -r '[.[][] | select(.user.type == "Bot") | .body // ""
      | select(contains("<!-- devx-ai-review summary -->"))] | last // ""
      | (capture("devx-ai-review state (?<s>\\{[^}]*\\})").s // "{}") | fromjson | .head // ""' \
    "$issue")" || return 1
  if [ -n "$last" ] && [ "$last" != "$HEAD_SHA" ] \
    && changed_files "$last" "$changed.tmp" && [ -s "$changed.tmp" ]; then
    mv "$changed.tmp" "$changed"  # else changed_since stays out: review every file
  fi
  jq -n --slurpfile r "$review" --slurpfile i "$issue" --slurpfile c "$changed" \
    '{review_comments: $r[0], conversation_comments: $i[0]}
     + (if $c[0] == {} then {} else {changed_since: $c[0]} end)' > "$1"
}
prior_file="$control_dir/prior.json"
prior_args=()
if fetch_prior "$prior_file" 2> "$control_dir/prior.err"; then
  prior_args=(-v "$prior_file:/github/prior.json:ro" -e REVIEW_PRIOR=/github/prior.json)
else
  echo "::warning::Could not read earlier reviews of this PR; the engine reviews every file."
fi

# The engine gets REVIEW_TIMEOUT to finish (or time out gracefully and still
# write a result) on its own; this wrapper is the backstop that keeps a hung
# engine from holding the job, and the check, until the 6 hour job limit.
# --init gives the container a PID 1 that actually dies on SIGTERM.
hard_timeout=$((REVIEW_TIMEOUT + 30))

start=$SECONDS
set +e
timeout --signal=TERM --kill-after=20 "$hard_timeout" \
  env -u GITHUB_TOKEN -u GH_TOKEN -u REGISTRY_TOKEN docker run --rm --init --name "$ENGINE_CONTAINER" \
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
  -e KORZA_CONTRACT_VERSION="$contract_version" -e REVIEW_IMAGE_DIGEST \
  -e REVIEW_OUTPUT_DIR=/github/out \
  ${prior_args[@]+"${prior_args[@]}"} \
  "$REVIEW_IMAGE" action-review
engine_exit=$?
set -e
elapsed=$((SECONDS - start))
# When the kill-after path fires, timeout kills the docker client and the
# container outlives it. Remove it before reading what it wrote. (A cancelled
# job never gets here; the always() cleanup step in action.yml covers that.)
timeout --signal=TERM --kill-after=5 10 docker rm -f "$ENGINE_CONTAINER" > /dev/null 2>&1 || true

engine_result="$out_dir/result.json"
top_status="$(env -i PATH="$PATH" python3 "$script_dir/inspect_result.py" "$engine_result")"
engine_reason() { env -i PATH="$PATH" python3 "$script_dir/inspect_result.py" --reason "$engine_result"; }

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
  # A crash is never a completed review, so nothing it wrote is published.
  # Still keep the blocking gate if it left a fail result (or an oversized one,
  # refused without parsing), and show the engine's own reason (a graceful
  # timeout writes one on TERM).
  case "$top_status" in
    fail | contract-fail) mark_blocking ;;
    oversized)
      mark_blocking
      incomplete "$msg It left an oversized result, which is treated as blocking."
      ;;
  esac
  detail="$msg"
  reason="$(engine_reason)"
  if [ -n "$reason" ]; then detail="$(printf '%s\n\nThe engine reported: %s' "$msg" "$reason")"; fi
  incomplete "$msg" "$detail"
fi

case "$top_status" in
  pass | fail) ;;
  contract-fail)
    mark_blocking
    incomplete "$contract_mismatch"
    ;;
  contract) incomplete "$contract_mismatch" ;;
  error) incomplete "Engine reported an error. The review could not complete." "$(engine_reason)" ;;
  *) incomplete "Engine exited with 0 after ${elapsed}s and produced no usable result. The review could not complete." ;;
esac
if [ "$top_status" = "fail" ]; then
  # Retain the blocking gate even if later validation or rendering fails.
  mark_blocking
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
if [ -f "$out_dir/results.sarif" ] && [ ! -L "$out_dir/results.sarif" ] \
  && [ -s "$out_dir/results.sarif" ]; then out sarif-file "$out_dir/results.sarif"; fi

# The engine's reason is in the check and the step summary. It never goes
# into a workflow command, where it could forge one.
if [ "$verdict" = "fail" ]; then
  echo "::error::AI Code Review reported blocking findings."
  exit 1
fi
