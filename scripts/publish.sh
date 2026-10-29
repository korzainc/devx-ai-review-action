#!/usr/bin/env bash
# A trusted host controller supplies OUTPUT_DIR and REVIEW_COMPLETION_FILE;
# contract.py documents the completion attestation. BASE_SHA is the frozen merge base.
set -euo pipefail

out() { printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"; }
: "${GITHUB_OUTPUT:?}"
out delivery error
for name in OUTPUT_DIR REVIEW_COMPLETION_FILE REVIEW_IMAGE GITHUB_REPOSITORY PR_NUMBER \
  HEAD_SHA BASE_SHA GITHUB_RUN_ID GITHUB_RUN_ATTEMPT RUNNER_TEMP GITHUB_TOKEN; do
  if [ -z "${!name:-}" ]; then
    echo "::error::Publisher requires prepared host context and a GitHub token."
    exit 1
  fi
done
publish_timeout="${PUBLISH_TIMEOUT:-360}"
if [[ ! "$publish_timeout" =~ ^[1-9][0-9]{0,3}$ ]] || [ "$publish_timeout" -gt 3600 ]; then
  echo "::error::publish-timeout-seconds must be 1 through 3600."
  exit 1
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
stage="$(mktemp -d "$RUNNER_TEMP/korza-publish.XXXXXXXX")"
publisher_container="${stage##*/}"
out publisher-container "$publisher_container"
publisher_pid=""
publisher_started=false
# Retain only minimized outputs/receipt. Do not retry the whole stage on any error:
# the publisher may already have posted some comments before failing.
receipt_ready=false
cleanup() {
  trap - EXIT INT TERM
  if [ -n "$publisher_pid" ]; then
    kill -TERM "$publisher_pid" 2> /dev/null || true
  fi
  if [ "$publisher_started" = true ]; then
    # Killing a Docker client does not guarantee the container stopped. Cleanup
    # has its own bound, and action.yml repeats it if the runner kills this shell.
    env -i PATH="$PATH" timeout --signal=TERM --kill-after=5 10 \
      docker rm -f "$publisher_container" > /dev/null 2>&1 || true
  fi
  if [ -n "$publisher_pid" ]; then
    kill -KILL "$publisher_pid" 2> /dev/null || true
    wait "$publisher_pid" 2> /dev/null || true
  fi
  if [ "$receipt_ready" = true ]; then
    rm -rf -- "$stage/publish" "$stage/run"
  else
    rm -rf -- "$stage"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
findings="$(env -i PATH="$PATH" python3 "$script_dir/contract.py" stage-handoff \
  --output-dir "$OUTPUT_DIR" --completion-file "$REVIEW_COMPLETION_FILE" --stage-dir "$stage" \
  --repository "$GITHUB_REPOSITORY" --pr-number "$PR_NUMBER" --head-sha "$HEAD_SHA" \
  --server-url "${GITHUB_SERVER_URL-https://github.com}" --api-url "${GITHUB_API_URL-https://api.github.com}" \
  --merge-base-sha "$BASE_SHA" --image "$REVIEW_IMAGE" --github-run-id "$GITHUB_RUN_ID" \
  --github-run-attempt "$GITHUB_RUN_ATTEMPT")"

set +e
publisher_started=true
env -i PATH="$PATH" GITHUB_TOKEN="$GITHUB_TOKEN" GITHUB_REPOSITORY="$GITHUB_REPOSITORY" \
  PR_NUMBER="$PR_NUMBER" timeout --signal=TERM --kill-after=5 "$publish_timeout" \
  docker run --rm --init --name "$publisher_container" --pull=never --user "$(id -u):$(id -g)" \
  -v "$stage/publish:/github/out/publish:ro" -v "$stage/run:/github/out/run:rw" \
  -e GITHUB_TOKEN -e GITHUB_REPOSITORY -e PR_NUMBER \
  -e REVIEW_OUTPUT_DIR=/github/out -e KORZA_CONTRACT_VERSION=1 \
  "$REVIEW_IMAGE" action-publish &
publisher_pid=$!
wait "$publisher_pid"
publisher_exit=$?
publisher_pid=""
set -e
if ! outputs="$(env -i PATH="$PATH" python3 "$script_dir/contract.py" validate-receipt \
  --stage-dir "$stage" --repository "$GITHUB_REPOSITORY" --pr-number "$PR_NUMBER" \
  --server-url "${GITHUB_SERVER_URL-https://github.com}" --findings-count "$findings" \
  --publisher-exit "$publisher_exit")"; then
  echo "::error::Publisher delivery was not verified; comments may already have been posted. No automatic retry."
  exit 1
fi
receipt_ready=true
printf '%s\n' "$outputs" >> "$GITHUB_OUTPUT"
