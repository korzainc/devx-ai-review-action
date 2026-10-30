# Korza AI Code Review

> **Pre-release.** The `pre-release` branch is for trying the action on a few
> repos before v1. It pins the alpha engine image
> (`ghcr.io/korzainc/devx-ai-review-pre-release`, engine `95b842a`), and the inputs, outputs and
> engine contract may still change.

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

```yaml
on:
  issue_comment:
    types: [created]
  pull_request:
    types: [opened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  checks: write
  packages: read

jobs:
  review:
    uses: korzainc/devx-ai-review-action/.github/workflows/ai-review.yml@pre-release
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

`pre-release` is a moving trial branch. Pin the reusable workflow to a full
reviewed commit SHA for a stable rollout; its composite action automatically
uses that same revision. This requires GitHub.com runners 2.336.0 or newer and
uses hosted `ubuntu-latest`.

The `ANTHROPIC_API_KEY` secret belongs to the consuming repository (or an org
secret available to it). No key is needed to post the hint; a review needs it.
The shared workflow accepts optional `config`, `skills`, `timeout-seconds`,
`fail-on-error`, `check-name` and `upload-debug` inputs under `with:`. Their
defaults match the action below. `job-timeout-minutes` bounds the whole job,
including preparation and publication; it defaults to GitHub’s standard 360 minutes. Keep it above
the engine budget plus image-pull and publishing time. Existing consumers can
preserve their check name and shorter job deadline with these inputs. Set
`upload-debug: false` (an unquoted YAML boolean) to disable debug artifact uploads.
`concurrency-prefix` defaults to `ai-review`; retain an existing prefix when
migrating so a new review can still cancel an older run on the same PR.

For a repository whose job token cannot pull the engine image, add an optional
`REGISTRY_TOKEN` secret and `registry-username` input to the same short caller:

```yaml
    with:
      registry-username: korza-registry-bot
    secrets:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
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
| `timeout-seconds` | `600` | Engine time budget, 1 through 3600. 30s past it the action sends TERM, then KILL 20s later. |
| `publish-timeout-seconds` | `360` | Hard outer publisher timeout in seconds (1 through 3600), including container startup, followed by bounded termination and cleanup. |
| `upload-sarif` | `false` | Also upload to code scanning. Needs `security-events: write`. |
| `upload-debug` | `true` | Keep review-stage logs, model calls, findings and inputs for up to 14 days. Set `false` to skip uploads. See the data note below. |
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
credential files. Anyone with read access to the repository can download them.
Publisher run files are not included in these artifacts. Normal console output
remains in workflow logs.

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

No `v1` release exists yet. To try the action, use `@pre-release`. It is a
moving branch, so pin a commit SHA from it if your org requires immutable refs.
Each release pins the engine image by digest in `action.yml`.
