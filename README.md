# Korza AI Code Review

This is a local wiring candidate for source review. It has not been merged,
published, installed in a consumer, or exercised against the reported alpha
image or a real GitHub/model account. No accepted integrated action SHA exists yet.

On-demand AI code review for pull requests. Comment `/review` on a PR and the
review runs inside your own CI. Findings appear as review comments on the exact
lines, and the result shows up as a pass/fail check you can require on protected
branches.

This action repository and the review engine image are private. A consumer needs
separate action-sharing and package pull access before use.

## Usage

Add [`examples/ai-review.yml`](examples/ai-review.yml) to the consuming repo as
`.github/workflows/ai-review.yml` and **merge it to the default branch**. GitHub
only runs `issue_comment` workflows from there, so `/review` does nothing until
it lands.

Command matching ignores letter case. The supplied caller requires the whole
comment to be `/review`; the action retains its first-line and optional argument
handling for custom callers.

```yaml
on:
  issue_comment:
    types: [created]
  pull_request:          # only for the one-time /review hint
    types: [opened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  checks: write
  packages: read

jobs:
  hint:
    if: >-
      github.event_name == 'pull_request' &&
      ((github.event.action == 'opened' && !github.event.pull_request.draft) ||
       github.event.action == 'ready_for_review') &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - env:
          GH_TOKEN: ${{ github.token }}
          PR: ${{ github.event.pull_request.number }}
        run: |
          gh pr comment "$PR" --repo "$GITHUB_REPOSITORY" --body \
            "Comment \`/review\` on this PR for a DevX AI Review. It runs on demand only."

  review:
    if: >-
      github.event_name == 'issue_comment' &&
      github.event.issue.pull_request &&
      github.event.comment.body == '/review' &&
      contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'),
               github.event.comment.author_association)
    runs-on: ubuntu-latest
    concurrency:
      group: ai-review-${{ github.event.issue.number }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: korzainc/devx-ai-review-action@REPLACE_WITH_REVIEWED_ACTION_SHA
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The action fetches the PR's base and head itself, so the default shallow checkout
is fine.

## Events

| Event | When to use it | Notes |
| --- | --- | --- |
| `issue_comment` | Always. On-demand reviews via `/review`. | Only `allowed-associations` can trigger. Works on fork PRs. |
| `pull_request` (`opened`, `ready_for_review`) | Always. Posts a one-time comment telling the author how to request a review. | Never runs a review: every review costs model tokens. |
| `pull_request` (every push) | Once a skill blocks merges. | Add `synchronize` and `reopened`, and let the review job run on `pull_request`, so the gate cannot be skipped. Fork PRs are skipped because their token is read-only; a maintainer can comment `/review` instead. |

## The check

After fetching the PR, the action opens a check run named **AI Code Review** on
the exact fetched head. A final step completes it even if the engine crashes or
the run is cancelled:

| Outcome | Check conclusion |
| --- | --- |
| No blocking findings | `success` |
| Blocking findings | `failure` |
| Review could not complete | `neutral` (or `failure` with `fail-on-error: true`) |
| Completed review but publisher delivery unverified | `failure` |
| Run cancelled | `cancelled` |

Require **AI Code Review** in branch protection or a ruleset. On `pull_request`
the workflow job also appears as `AI Code Review / review (pull_request)`; don't
require that one. Comments and the check are posted as `github-actions[bot]`.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `anthropic-api-key` | required | Model API key. Pass it from a secret. |
| `github-token` | `github.token` | Reads the PR, posts the check run and review comments. |
| `registry-token` | `github.token` | Pulls the engine image from GHCR. |
| `registry-username` | `github.actor` | GHCR login user. Set to the token owner when using a PAT. |
| `config` | `.korza/review.yml` | Review config in the consuming repo. |
| `skills` | all enabled | Comma-separated skills to run. |
| `trigger-phrase` | `/review` | Comment that starts a review. |
| `allowed-associations` | `OWNER,MEMBER,COLLABORATOR` | Who may trigger by comment. |
| `check-name` | `AI Code Review` | Name of the check run to require. |
| `fail-on-error` | `false` | Report `failure` instead of `neutral` when the review cannot complete. |
| `timeout-seconds` | `600` | Engine time budget. |
| `publish-timeout-seconds` | `120` | Publisher time budget in seconds (1 through 3600), followed by bounded termination and cleanup. |
| `upload-sarif` | `false` | Also upload to code scanning. Needs `security-events: write`. |
| `image` | pinned per release | Engine image digest. Override only for testing. |

## Outputs

| Output | Description |
| --- | --- |
| `verdict` | `pass`, `fail`, `error`, or `skipped` |
| `findings` | Number of findings |
| `sarif-file` | Path to the SARIF file on the runner |
| `check-run-id` | ID of the check run on the PR head commit |
| `delivery` | `success` only after a verified summary-comment receipt; otherwise `skipped` or `error` |
| `summary-url` | Verified summary-comment URL on successful delivery |

## Access to the engine image

| Where the consuming repo lives | How it pulls |
| --- | --- |
| `korzainc` org | Default `GITHUB_TOKEN`. Grant the repo read access on the package (Package settings, Manage Actions access) and set `packages: read`. |
| Any other org | A classic PAT with `read:packages` from a Korza machine account, stored as a secret and passed as `registry-token`, with `registry-username` set to that account. |

GHCR does not currently accept GitHub App installation tokens or fine-grained PATs
for pulls, which is why other orgs need a classic PAT. Give the machine account
read access to this one package only, so the token can pull nothing else.

## Security

- The engine mounts the repo read-only and never builds or executes PR code. This
  matters because `issue_comment` runs on fork PRs have access to secrets.
- PR titles, descriptions, and comments are treated as untrusted data, never as instructions.
- Only the associations in `allowed-associations` can trigger a review by comment.
- Inputs reach scripts through environment variables, never interpolated into shell.
- The pilot requires the pinned checkout v7.0.1 shown above, which stores its
  persisted credential outside the mounted workspace. Keep credentials outside
  `/workspace`; do not mount the runner's home or whole `RUNNER_TEMP` into the
  engine. A custom checkout must preserve that boundary before use.
- Publisher attestation covers comment delivery. The pilot leaves `upload-sarif`
  disabled. Optional SARIF uses the existing, separate artifact upload path and
  is not validated by this attestation; enabling it requires a separate review
  of artifact integrity and attribution to the reviewed head.

## Engine contract

`scripts/run.sh` runs the pinned image's `action-review` command as the runner's
UID with model authority only:

| Mount or variable | Contents |
| --- | --- |
| `/workspace` (read-only) | The repo, with `refs/korza/base` and `refs/korza/head` fetched |
| `/github/event.json` | The event payload |
| `/github/out` (writable) | Where the engine writes its results |
| `PR_NUMBER`, `BASE_SHA`, `HEAD_SHA` | The PR and the merge-base..head range to review |
| `CHECK_RUN_ID` | Identity of the open check run, for logging and correlation only. The review container has no GitHub token and cannot modify it; `finalize.sh` completes it. |
| `REVIEW_CONFIG`, `REVIEW_SKILLS`, `REVIEW_TIMEOUT` | From the action inputs |

The engine writes versioned `result.json`, typed `publish/result.json` and
`publish/snapshot.json`, and optionally `results.sarif`. After a zero process
exit and a completed pass/fail result, the host validates the handoff and records
its hashes and frozen identity outside the engine mount. Only then does
`scripts/publish.sh` stage the two typed files and invoke `action-publish` with
GitHub authority. A completed blocking fail is still publishable; a crash,
invalid handoff, cancelled run or unverified publisher receipt cannot produce a
successful check. Publication can be partial, so the wrapper does not retry it.
The engine must not assume a home directory or writable `/workspace`.

## Versioning

The candidate uses the reported alpha image digest as a source configuration
input. Its runtime contents and grants are unverified. A consumer must pin a
reviewed, accepted action commit after integration; no `v1` release exists.
