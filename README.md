# Korza AI Code Review

> **Engine 0.1.1.** Pin the action to commit `ab02c64` (full SHA below), which pins the
> released engine image `ghcr.io/korzainc/devx-ai-review:0.1.1`, a private package: a repo needs
> read access on it ("Manage Actions access") to run the action. The `pre-release` branch stays
> on the earlier release candidate until the repos that follow it move.

On-demand AI code review for pull requests. Comment `/review` on a PR and the
review runs inside your own CI. Findings appear as review comments on the exact
lines, and the result shows up as a pass/fail check you can require on protected
branches.

This repo is public. The review engine is a private image on GHCR, so using the
action needs pull access to that image (see [Access](#access-to-the-engine-image)).

## Usage

Add [`examples/ai-review-reusable.yml`](examples/ai-review-reusable.yml) as
`.github/workflows/ai-review.yml` and **merge it to the default branch**. The shared
workflow keeps the one-time PR hint, authorization, checkout and review steps in
this repository. Only `/review` comments run the engine.

The introductory hint is skipped if this PR already has a completed review
summary from `github-actions[bot]`. This does not certify that later commits
have been reviewed; the review check and summary show the reviewed revision.

```yaml
on:
  issue_comment:
    types: [created]
  pull_request:
    types: [opened, ready_for_review]
  # Dispatched by the action itself to post findings GitHub's rate limit deferred.
  # Do not run it by hand. See "Findings the rate limit defers".
  workflow_dispatch:
    inputs:
      pr: {description: Pull request to post to., required: true}
      source-run-id: {description: Run whose artifact holds the findings., required: true}
      source-run-attempt: {description: That run's attempt., required: true}
      attempt: {description: Which follow-up this is., required: true}

permissions:
  contents: read
  pull-requests: write
  checks: write
  packages: read
  actions: write   # dispatch the follow-up, and read the earlier run's handoff artifact

jobs:
  review:
    # The commit that pins engine 0.1.1.
    uses: korzainc/devx-ai-review-action/.github/workflows/ai-review.yml@ab02c64333b5afd4792784630de5170defb0e3c0
    with:
      republish-pr-number: ${{ inputs.pr }}
      republish-source-run-id: ${{ inputs.source-run-id }}
      republish-source-run-attempt: ${{ inputs.source-run-attempt }}
      republish-attempt: ${{ inputs.attempt }}
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      KORZA_ORGWIDE_DEVX_AI_REVIEW_OPENAI_API_KEY: ${{ secrets.KORZA_ORGWIDE_DEVX_AI_REVIEW_OPENAI_API_KEY }}
```

Pin the reusable workflow to a full commit SHA; its composite action automatically uses
that same revision. It runs on hosted `ubuntu-latest` and needs support for
repository-root `$/` references (supported by runner 2.338.2).

A review needs one model key; posting the hint needs none. The key picks the model, in this order:

1. The repository's own `OPENAI_API_KEY`: GPT-6 Luna.
2. The repository's own `ANTHROPIC_API_KEY`: Haiku 5.5.
3. The org-level `KORZA_ORGWIDE_DEVX_AI_REVIEW_OPENAI_API_KEY`: GPT-6 Luna.

The org key has its own name because a workflow can't tell a repository secret from an org
secret of the same name; don't create org secrets named `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.
The `provider` input (`anthropic` or `openai`) forces one. Only the chosen key reaches the engine.

The shared workflow accepts optional `config`, `skills`, `timeout-seconds`, `provider`,
`fail-on-error`, `check-name` and `upload-debug` inputs under `with:`. Their
defaults match the action below. `job-timeout-minutes` bounds the whole job,
including preparation and publication; it defaults to GitHub’s standard 360 minutes. Keep it above
the engine budget plus image-pull and publishing time. Existing consumers can
preserve their check name and shorter job deadline with these inputs. Debug
artifacts upload only from private repositories by default (`upload-debug: auto`); set
`upload-debug: "true"` or `"false"` to force them on or off.
When updating an existing reusable-workflow pin, quote any existing `upload-debug` boolean value: use
`upload-debug: "true"` or `"false"` instead of bare `true` or `false`. The input
now accepts strings. Private repositories that previously omitted it will upload
debug artifacts; set `"false"` to keep uploads disabled.
`concurrency-prefix` defaults to `ai-review`; retain an existing prefix when
migrating so a new review can still cancel an older run on the same PR.

For a repository whose job token cannot pull the engine image, add an optional
`REGISTRY_TOKEN` secret and `registry-username` input to the same short caller:

```yaml
    with:
      registry-username: korza-registry-bot
    secrets:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      REGISTRY_TOKEN: ${{ secrets.KORZA_REGISTRY_TOKEN }}
```

Use the token owner's username and a pull-only credential with access to this
package. If omitted or empty, these settings independently fall back to
`github.token` and `github.actor`, preserving the default caller. The registry
credential is used for GHCR login; it does not replace the token that reads the
PR or posts reviews. Pass secrets by name rather than inheriting unrelated ones.

The action repository is public, so cross-org callers can reference the shared
workflow. Their Actions policy must allow it and the actions it uses. Image
access remains separate; see [Access](#access-to-the-engine-image).

The short workflow deliberately keeps the image pin, command grammar, allowed
roles and publisher defaults in the action repository. For custom triggers or
roles, a custom GitHub API token, publisher budgets, an image override for
testing, SARIF or action outputs, use the [full composite example](examples/ai-review.yml)
and its inputs. Those capabilities remain available; they are not forwarded by
the short workflow. SARIF also needs an explicit permission grant and correct
reviewed-commit attribution. Custom runners and extra same-job steps likewise
use the full action. The action fetches the PR's base and head itself, so the
default shallow checkout is fine.

Command matching ignores letter case. Both supplied workflows accept `/review`
with an optional trailing LF or CRLF. Custom composite callers can use its
first-line and optional argument handling.

## Events

| Event | When to use it | Notes |
| --- | --- | --- |
| `issue_comment` | Always. On-demand reviews via `/review`. | Only `allowed-associations` can trigger. Works on fork PRs. |
| `pull_request` (`opened`, `ready_for_review`) | Always. Posts a one-time comment telling the author how to request a review. | Never runs a review: every review costs model tokens. |
| `pull_request` (every push) | Once a skill blocks merges. | Add `synchronize` and `reopened`, and let the review job run on `pull_request`, so the gate cannot be skipped. Fork PRs are skipped because their token is read-only; a maintainer can comment `/review` instead. |
| `workflow_dispatch` | Always, unless `auto-republish: "false"`. | Dispatched by the action, not by people. Posts findings a previous run could not. |

## Findings the rate limit defers

GitHub meters how fast a token may create content. The publisher paces itself inside that
limit and waits when it is refused, but its waiting is bounded (`publish-timeout-seconds`,
and the engine's own ceiling). Findings it could not post in time are *deferred*: still
owed, never lost. They are named in the publish receipt and their files are marked
unfinished in the review state.

With `auto-republish: true` (the default) the run then dispatches your own workflow again,
passing the run whose artifact holds the findings. That follow-up downloads the handoff,
re-attests it to itself, and posts only what is missing — no model call, so no extra cost.
It repeats until nothing is owed or `max-republish-attempts` is reached, after which the
summary asks for a `/review` instead.

This needs three things in the consuming workflow: the `workflow_dispatch` block with those
four inputs, `actions: write`, and the inputs forwarded to the action. Without them, set
`auto-republish: "false"`; deferred findings are then posted by the next review of the
pull request, which re-reviews only the unfinished files.

The handoff artifact holds the review's own findings, the PR snapshot and the host
attestation. Most of those findings are already on the pull request as comments, but the
deferred ones are not yet, which is the point of keeping them. The diff and the raw
model answers stay in the debug artifact, behind `upload-debug`. Both expire after 14 days,
which bounds how late a follow-up can post.

## The check

After fetching the PR, the action opens a check run named **AI Code Review** on
the exact fetched head. A final step completes it even if the engine crashes or
the run is cancelled:

| Outcome | Check conclusion |
| --- | --- |
| No blocking findings | `success` |
| Blocking findings | `failure` |
| Review could not complete | `neutral` (or `failure` with `fail-on-error: true`) |
| Review could not complete, but the engine had reported blocking findings | `failure` |
| Completed review but publisher delivery unverified | `failure` |
| Run cancelled | `cancelled` |

A review only completes when the engine exits 0 and writes a valid `result.json`.
Anything else (a crash, a timeout, an unreadable or mismatched result) is
"could not complete": its findings and SARIF are not published. A result that
says `fail` keeps the check failing even then, so a crash can never open the
gate; an oversized result from a crashed engine is treated the same way.

Require **AI Code Review** in branch protection or a ruleset. On `pull_request`
the workflow job also appears as `AI Code Review / review (pull_request)`; don't
require that one. Comments and the check are posted as `github-actions[bot]`.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `openai-api-key` | empty | The repository's own OpenAI key, from a secret. Used first (GPT-6 Luna). |
| `anthropic-api-key` | empty | The repository's own Anthropic key, from a secret. Used when no `openai-api-key` is set (Haiku 5.5). |
| `org-openai-api-key` | empty | The org-level OpenAI key (`KORZA_ORGWIDE_DEVX_AI_REVIEW_OPENAI_API_KEY`). Used only when neither key above is set. One of the three is needed. |
| `provider` | empty | `anthropic` or `openai` to force a provider; empty picks by key in the order above. |
| `github-token` | `github.token` | Reads the PR, posts the check run and review comments. |
| `registry-token` | `github.token` | Pulls the engine image from GHCR. |
| `registry-username` | `github.actor` | GHCR login user. Set to the token owner when using a PAT. |
| `config` | `.korza/review.yml` | Review config in the consuming repo. |
| `skills` | all enabled | Comma-separated skills to run. |
| `trigger-phrase` | `/review` | Comment that starts a review. |
| `allowed-associations` | `OWNER,MEMBER,COLLABORATOR` | Who may trigger by comment. |
| `check-name` | `AI Code Review` | Name of the check run to require. |
| `fail-on-error` | `false` | Report `failure` instead of `neutral` when the review cannot complete. |
| `timeout-seconds` | `600` | Engine time budget, 1 through 3600. 30s past it the action sends TERM, then KILL 20s later. |
| `publish-timeout-seconds` | `360` | Hard outer publisher timeout in seconds (1 through 3600), including container startup, followed by bounded termination and cleanup. |
| `upload-sarif` | `false` | Also upload to code scanning. Needs `security-events: write`. |
| `upload-debug` | `auto` | Keep review-stage logs, model calls, findings and inputs for up to 14 days. `auto` uploads only when the repository is private; `true` or `false` forces it. See the data note below. |
| `auto-republish` | `true` | Post findings the rate limit deferred by dispatching this workflow again, instead of waiting for the author. Needs `actions: write` and the `workflow_dispatch` block. |
| `max-republish-attempts` | `3` | How many follow-ups in a row may be dispatched for one pull request. |
| `republish-pr-number`, `republish-source-run-id`, `republish-source-run-attempt`, `republish-attempt` | empty | Set by the action's own dispatch. Never set these by hand. |
| `image` | pinned per release | Engine image digest. Override only for testing. |

The publish timeout is forwarded unchanged to the engine. A compatible engine reserves
its own shutdown margin within that limit; the action does not subtract another one.
The paired rate-limit engine uses 350s as its internal ceiling when given the default
360s limit; its clock starts after container startup. This handoff requires an image
with that timeout support; updating the action alone does not update its pinned engine.
Container startup, GitHub delays or failures can still prevent a summary or receipt.
The timeout bounds the attempt; it does not guarantee delivery.

The paired rate-limit engine paces reviews to about 15 comments a minute per run and
may wait up to 240s in total. Large reviews can take a few minutes; keep the outer
timeout above the configured waiting budget.

Debug artifacts contain unredacted source and model text, which may include sensitive
content. The action does not deliberately attach the checkout, environment files or
credential files. Anyone with read access to the repository can download them, which
is why the default (`auto`) never uploads from a public repository.
Publisher run files are not included in these artifacts. Normal console output
remains in workflow logs.

Debug staging accepts at most 256 MiB of regular-file contents and 10,000 tree
entries, including directories and skipped entries. If either limit is exceeded,
staging is abandoned with a warning and the debug artifact is skipped. The review
and publishing results are unchanged.

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
- The pilot requires `actions/checkout` v7.0.1, pinned in the
  [shared workflow](.github/workflows/ai-review.yml) and
  [full workflow example](examples/ai-review.yml). This version stores its
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
| `REVIEW_IMAGE_DIGEST` | SHA-256 content digest of the pinned engine image, passed for provenance; it does not select a different image |
| `REVIEW_CONFIG`, `REVIEW_SKILLS`, `REVIEW_TIMEOUT` | From the action inputs |
| `PUBLISH_TIMEOUT` (publish step) | From `publish-timeout-seconds`; the engine sizes its rate-limit waiting to finish inside it |
| `REVIEW_PRIOR` → `/github/prior.json` (read-only) | What earlier reviews left on the PR, fetched by the action with the job token: `review_comments` (the bot's earlier findings), `conversation_comments` (its summary, with the last reviewed commit) and `changed_since`: the files whose content differs between that commit and the head (`snapshot_diff: true`), also after a force push or rebase (two compares from the merge base, matched by blob hash). `changed_since` is left out when the last commit is gone or GitHub can't list every file; the engine then reviews every file. |
| `KORZA_CONTRACT_VERSION` | The contract version this action implements, currently `1` |

The engine writes versioned `result.json`, typed `publish/result.json` and
`publish/snapshot.json`, and optionally `results.sarif`. After a zero process
exit and a completed pass/fail result, the host validates the handoff and records
its hashes and frozen identity outside the engine mount. Only then does
`scripts/publish.sh` stage the two typed files and invoke `action-publish` with
GitHub authority. A completed blocking fail is still publishable; a crash,
invalid handoff, cancelled run or unverified publisher receipt cannot produce a
successful check. Publication can be partial, so the wrapper does not retry it.
The engine must not assume a home directory or writable `/workspace`.

`result.json` must carry a top-level integer `contract_version` equal to the
version this action implements (`1`). The action refuses any other value and
reports it like any engine error, so a mismatched action/image pair fails with a
clear message instead of misbehaving. A result that says `fail` still keeps the
check failing. Changing the contract means a new action release shipping the
matching image.

### Timeouts

`REVIEW_TIMEOUT` is the engine's whole budget, and the engine should enforce it
itself: finish within it, or time out gracefully and still write a `result.json`
with `verdict.status: error` and a reason, which gives the PR a precise message.
The action is the backstop, not the mechanism: 30 seconds past the budget it
TERMs the container (KILL 20 seconds later). If the engine writes `result.json`
on TERM, the check shows its reason next to the timeout; otherwise the action
reports a generic timeout error. Either way the check completes, `neutral` by default or `failure` with
`fail-on-error: true`. A required check is never left hanging in progress.

If the job is cancelled, a cleanup step removes the engine container, so a
hung engine does not keep running on a self-hosted runner.

## Versioning

No release tag exists yet. Pin a commit SHA: `ab02c64333b5afd4792784630de5170defb0e3c0` pins engine 0.1.1. Each
pinned commit pins the engine image by digest in `action.yml`. The `pre-release` branch is the
earlier trial branch and will be retired.
