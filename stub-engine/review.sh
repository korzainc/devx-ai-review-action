#!/usr/bin/env bash
# Stub engine. Proves the action's adapter wiring before the real engine exists.
# Follows the engine contract: reads the contract env, writes result.json to
# REVIEW_OUTPUT_DIR, posts one review comment, and never completes the check run.
#
# REVIEW_SKILLS picks the scenario (comma-separated, first match wins):
#   advisory  default: medium + low findings, both advisory, verdict pass
#   blocking  medium finding is enforcing, verdict fail
#   clean     no findings, verdict pass
#   error     verdict error, as if the review could not complete
#   crash     exit nonzero without writing result.json
#   sleep     sleep REVIEW_TIMEOUT seconds first, to test cancellation
set -euo pipefail

OUT_DIR="${REVIEW_OUTPUT_DIR:-/github/out}"

mode=advisory
for m in blocking clean error crash sleep; do
  case ",${REVIEW_SKILLS:-}," in
    *,"$m",*) mode="$m"; break ;;
  esac
done
echo "Stub engine: mode=$mode PR=#${PR_NUMBER:-?} ${BASE_SHA:0:7}..${HEAD_SHA:0:7}"

if [ "$mode" = "crash" ]; then
  echo "Simulating an engine crash: exiting without a result." >&2
  exit 2
fi
if [ "$mode" = "sleep" ]; then
  echo "Sleeping ${REVIEW_TIMEOUT:-600}s so the run can be cancelled."
  sleep "${REVIEW_TIMEOUT:-600}"
  mode=advisory
fi

provenance() {
  jq -n '{engine_version: "stub-0.1.0", image_digest: "", model: "none"}'
}

if [ "$mode" = "error" ]; then
  jq -n --argjson prov "$(provenance)" \
    '{verdict: {status: "error", reason: "Stub simulated an engine error.", blocking_findings: []},
      findings: [], skills_run: ["stub"], provenance: $prov}' > "$OUT_DIR/result.json"
  exit 0
fi

# Pick lines the findings can attach to: the first added line of each hunk in
# the PR diff. Review comments only land on lines that are part of the diff.
targets=()
while IFS= read -r t; do targets+=("$t"); done < <(
  git diff --unified=0 "$BASE_SHA" "$HEAD_SHA" | awk '
    /^\+\+\+ / { f = ""; if (substr($0, 5, 2) == "b/") f = substr($0, 7) }
    /^@@/ {
      if (f == "") next
      for (i = 1; i <= NF; i++) if ($i ~ /^\+[0-9]+/) {
        split(substr($i, 2), a, ",")
        if (a[2] == "" || a[2] + 0 > 0) print f ":" a[1]
        break
      }
    }'
)

if [ "${#targets[@]}" -eq 0 ] && [ "$mode" != "clean" ]; then
  echo "No added lines in the diff. Falling back to a clean result."
  mode=clean
fi

if [ "$mode" = "clean" ]; then
  jq -n --argjson prov "$(provenance)" \
    '{verdict: {status: "pass", reason: "No findings.", blocking_findings: []},
      findings: [], skills_run: ["stub"], provenance: $prov}' > "$OUT_DIR/result.json"
  echo "Result written: pass, 0 findings."
  exit 0
fi

path1="${targets[0]%:*}"; line1="${targets[0]#"${path1}":}"
t2="${targets[1]:-${targets[0]}}"
path2="${t2%:*}"; line2="${t2#"${path2}":}"

line1_text="$(git show "$HEAD_SHA:$path1" | sed -n "${line1}p")"

enforcing=false; status=pass; blocking='[]'
reason="No blocking findings. 2 advisory findings."
if [ "$mode" = "blocking" ]; then
  enforcing=true; status=fail; blocking='["stub-0001"]'
  reason="1 blocking finding from the stub skill."
fi

jq -n \
  --arg status "$status" --arg reason "$reason" --argjson blocking "$blocking" \
  --argjson enforcing "$enforcing" \
  --arg path1 "$path1" --argjson line1 "$line1" --arg sugg "$line1_text" \
  --arg path2 "$path2" --argjson line2 "$line2" \
  --argjson prov "$(provenance)" \
  '{verdict: {status: $status, reason: $reason, blocking_findings: $blocking},
    findings: [
      {id: "stub-0001", rule_id: "stub/medium-check", skill: "stub",
       severity: "medium", confidence: 0.95, enforcing: $enforcing,
       location: {path: $path1, start_line: $line1, end_line: $line1},
       message: "Stub medium finding on an added line, to prove the adapter wiring.",
       suggestion: $sugg},
      {id: "stub-0002", rule_id: "stub/low-note", skill: "stub",
       severity: "low", confidence: 0.6, enforcing: false,
       location: {path: $path2, start_line: $line2, end_line: $line2},
       message: "Stub low advisory finding."}
    ],
    skills_run: ["stub"], provenance: $prov}' > "$OUT_DIR/result.json"
echo "Result written: $status, 2 findings."

# Post one review comment with a suggestion block. Best effort: the result
# stands even if commenting fails (for example, a read-only token).
if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${PR_NUMBER:-}" ]; then
  body="**Korza AI Review (stub)**: example finding on an added line.

\`\`\`suggestion
$line1_text
\`\`\`"
  if jq -n --arg body "$body" --arg commit_id "$HEAD_SHA" \
       --arg path "$path1" --argjson line "$line1" \
       '{body: $body, commit_id: $commit_id, path: $path, line: $line, side: "RIGHT"}' \
     | curl -sS --fail-with-body -o /dev/null -X POST \
         -H "Authorization: Bearer $GITHUB_TOKEN" \
         -H "Accept: application/vnd.github+json" \
         "${GITHUB_API_URL:-https://api.github.com}/repos/$GITHUB_REPOSITORY/pulls/$PR_NUMBER/comments" \
         --data-binary @-; then
    echo "Posted a review comment on $path1:$line1."
  else
    echo "::warning::Could not post the review comment. Continuing."
  fi
else
  echo "No GITHUB_TOKEN or PR_NUMBER. Skipping the review comment."
fi

exit 0
