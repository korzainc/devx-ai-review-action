# Stub engine

A canned stand-in for the real review engine. It follows the engine contract,
so the action's trigger, auth, refs, check run, and verdict handling can be
tested end to end before the engine exists.

What it does:

- Reads the contract env (PR_NUMBER, BASE_SHA, HEAD_SHA, REVIEW_*, GITHUB_*).
- Finds added lines in the PR diff and attaches two findings to them:
  one medium (`stub/medium-check`) and one low advisory (`stub/low-note`).
- Writes `result.json` to `REVIEW_OUTPUT_DIR`.
- Posts one review comment with a suggestion block, best effort.
- Never completes the check run. That is the action's job.

`REVIEW_SKILLS` picks the scenario, which maps to the `skills` input:

| Value | Result |
| --- | --- |
| (empty) or anything else | advisory: 2 findings, verdict pass |
| `blocking` | medium finding enforcing, verdict fail |
| `clean` | no findings, verdict pass |
| `error` | verdict error |
| `crash` | exits 2 with no result.json |
| `sleep` | hangs well past REVIEW_TIMEOUT, to test cancellation and the timeout backstop |

Build and try locally:

```sh
docker build -t ai-review-stub stub-engine/
```
