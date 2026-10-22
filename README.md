# Korza AI Code Review

On-demand AI code review for pull requests. Comment `/review` on a PR and the
review runs inside your own CI. Findings appear as review comments on the exact
lines, and the result shows up as a pass/fail check you can require on protected
branches.

This repo is public. The review engine is a private image on GHCR, so using the
action needs pull access to that image (see [Access](#access-to-the-engine-image)).

## Usage

Add [`examples/ai-review.yml`](examples/ai-review.yml) to the consuming repo as
`.github/workflows/ai-review.yml` and **merge it to the default branch**. GitHub
only runs `issue_comment` workflows from there, so `/review` does nothing until
it lands.

```yaml
on:
  issue_comment:
    types: [created]
  pull_request:          # add once a skill blocks merges

permissions:
  contents: read
  pull-requests: write
  checks: write
  packages: read

concurrency:
  group: ai-review-${{ github.event.issue.number || github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    if: >-
      github.event_name == 'pull_request' ||
      (github.event.issue.pull_request && startsWith(github.event.comment.body, '/review'))
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: korzainc/devx-ai-review-action@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

The action fetches the PR's base and head itself, so the default shallow checkout
is fine.

## Events

| Event | When to use it | Notes |
| --- | --- | --- |
| `issue_comment` | Always. On-demand reviews via `/review`. | Only `allowed-associations` can trigger. Works on fork PRs. |
| `pull_request` | Once a skill blocks merges. | Runs on every push so the gate cannot be skipped. Fork PRs are skipped because their token is read-only; a maintainer can comment `/review` instead. |

## The check

The action opens a check run named **AI Code Review** on the PR's head commit as
soon as the run starts, so the PR shows "In progress" right away. A final step
always completes it, even if the engine crashes or the run is cancelled:

| Outcome | Check conclusion |
| --- | --- |
| No blocking findings | `success` |
| Blocking findings | `failure` |
| Review could not complete | `neutral` (or `failure` with `fail-on-error: true`) |
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
| `upload-sarif` | `false` | Also upload to code scanning. Needs `security-events: write`. |
| `image` | pinned per release | Engine image digest. Override only for testing. |

## Outputs

| Output | Description |
| --- | --- |
| `verdict` | `pass`, `fail`, `error`, or `skipped` |
| `findings` | Number of findings |
| `sarif-file` | Path to the SARIF file on the runner |
| `check-run-id` | ID of the check run on the PR head commit |

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

## Engine contract

`scripts/run.sh` runs the image as the runner's UID with:

| Mount or variable | Contents |
| --- | --- |
| `/workspace` (read-only) | The repo, with `refs/korza/base` and `refs/korza/head` fetched |
| `/github/event.json` | The event payload |
| `/github/out` (writable) | Where the engine writes its results |
| `PR_NUMBER`, `BASE_SHA`, `HEAD_SHA` | The PR and the merge-base..head range to review |
| `CHECK_RUN_ID` | The open check run. The engine may update its output text to show progress, but must not complete it. |
| `REVIEW_CONFIG`, `REVIEW_SKILLS`, `REVIEW_TIMEOUT` | From the action inputs |

The engine writes `result.json` (a `ReviewResult`) and optionally `results.sarif`
to `/github/out`, and posts review comments itself. It must not assume a home
directory or a writable `/workspace`. Anything beyond this is a contract change.

## Versioning

Each release pins the engine image by digest in `action.yml` and is tagged
`vX.Y.Z`, with a moving `v1` tag. A skill moving from advisory to blocking ships as
a new image, so it always arrives as a new release.
