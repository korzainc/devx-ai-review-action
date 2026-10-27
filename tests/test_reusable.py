"""Offline checks of the reusable workflow's authorization and hint boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

from test_wiring import github_equal, github_expression

ROOT = Path(__file__).resolve().parents[1]


class ReusableTests(unittest.TestCase):
    def workflow(self):
        return (ROOT / ".github/workflows/ai-review.yml").read_text()

    def test_only_authorized_pr_commands_can_enter_cancelling_job(self):
        source = self.workflow()
        review = source.split("  review:\n", 1)[1]
        gate = review.split("    runs-on:", 1)[0].split("    if: >-\n", 1)[1]
        expression = " ".join(line.strip() for line in gate.splitlines())
        cases = [(True, command, role, True)
                 for command in ("/review", "/Review", "/review\n", "/review\r\n")
                 for role in ("OWNER", "MEMBER", "COLLABORATOR")]
        cases += [(True, command, role, False) for command, role in
                  (("/review", "CONTRIBUTOR"), ("ordinary", "MEMBER"),
                   ("/reviewdog", "MEMBER"), ("/review extra", "MEMBER"))]
        cases.append((False, "/review", "MEMBER", False))
        for is_pr, command, role, expected in cases:
            with self.subTest(is_pr=is_pr, command=command, role=role):
                github = SimpleNamespace(event_name="issue_comment", event=SimpleNamespace(
                    issue=SimpleNamespace(pull_request=is_pr),
                    comment=SimpleNamespace(body=command, author_association=role)))
                admitted = github_expression(expression, {"github": github}, allowed_functions={
                    "contains": lambda values, item: any(github_equal(v, item) for v in values),
                    "fromJSON": json.loads, "cancelled": lambda: False})
                self.assertEqual(bool(admitted), expected)
        # A replacement review must stop this job on cancellation re-evaluation.
        github = SimpleNamespace(event_name="issue_comment", event=SimpleNamespace(
            issue=SimpleNamespace(pull_request=True),
            comment=SimpleNamespace(body="/review", author_association="MEMBER")))
        admitted = github_expression(expression, {"github": github}, allowed_functions={
            "contains": lambda values, item: any(github_equal(v, item) for v in values),
            "fromJSON": json.loads, "cancelled": lambda: True})
        self.assertFalse(admitted)
        self.assertNotIn("\nconcurrency:", source)
        self.assertIn("    concurrency:\n", review)

    def test_hint_is_posted_once_and_without_model_credentials(self):
        hint = self.workflow().split("  hint:\n", 1)[1].split("  review:\n", 1)[0]
        script = textwrap.dedent(hint.split("        run: |\n", 1)[1])
        self.assertNotIn("ANTHROPIC_API_KEY", hint)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            gh = base / "gh"
            gh.write_text("#!" + sys.executable + "\n" + '''import os, pathlib, sys
record = pathlib.Path(os.environ["FIXTURE_COMMENT"])
if sys.argv[1] == "api":
    if os.environ.get("FIXTURE_LIST_ERROR"): raise SystemExit(1)
    print(record.read_text() if record.exists() else "")
elif sys.argv[1:3] == ["pr", "comment"]:
    record.write_text(sys.stdin.read())
    with record.with_suffix(".count").open("a") as stream: stream.write("posted\\n")
else: raise SystemExit(1)
''')
            gh.chmod(0o755)
            env = dict(os.environ, PATH=str(base) + os.pathsep + os.environ["PATH"],
                       GITHUB_REPOSITORY="fixture/repo", PR="7",
                       GH_TOKEN="synthetic", FIXTURE_COMMENT=str(base / "comment"))
            for _ in range(2):
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                        env=env, text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((base / "comment.count").read_text(), "posted\n")
            self.assertIn("<!-- devx-ai-review-hint -->", (base / "comment").read_text())
            # A marker near the start of a large response must not trip pipefail.
            (base / "comment").write_text("<!-- devx-ai-review-hint -->\n" + "x" * 262144)
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((base / "comment.count").read_text(), "posted\n")
            (base / "comment").unlink()
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script],
                                    env=dict(env, FIXTURE_LIST_ERROR="1"),
                                    text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base / "comment").exists())

    def test_composite_is_bound_to_called_workflow_revision(self):
        source = self.workflow()
        review = source.split("  review:\n", 1)[1]
        self.assertIn("      - uses: $/\n", review)
        self.assertNotIn("@pre-release", source)
        self.assertNotIn("      - uses: ./", review)
        self.assertIn("anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}", review)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", review)
        self.assertNotIn("secrets: inherit", source)


    def test_caller_overrides_keep_check_identity_and_job_deadline(self):
        source = self.workflow()
        self.assertIn("      check-name:\n", source)
        self.assertIn("      job-timeout-minutes:\n", source)
        self.assertIn("    timeout-minutes: ${{ inputs.job-timeout-minutes }}", source)
        self.assertIn("          check-name: ${{ inputs.check-name }}", source)
        self.assertIn("        default: 360", source)
        self.assertIn("      concurrency-prefix:\n", source)
        self.assertIn("      group: ${{ inputs.concurrency-prefix }}-${{ github.event.issue.number }}", source)

    def test_hint_only_runs_for_ready_same_repository_prs(self):
        hint = self.workflow().split("  hint:\n", 1)[1].split("  review:\n", 1)[0]
        gate = hint.split("    runs-on:", 1)[0].split("    if: >-\n", 1)[1]
        expression = " ".join(line.strip() for line in gate.splitlines())
        for action, draft, repo, expected in (("opened", False, "fixture/repo", True),
                ("opened", True, "fixture/repo", False),
                ("ready_for_review", False, "fixture/repo", True),
                ("opened", False, "fork/repo", False),
                ("synchronize", False, "fixture/repo", False)):
            with self.subTest(action=action, draft=draft, repo=repo):
                github = SimpleNamespace(event_name="pull_request", repository="fixture/repo",
                    event=SimpleNamespace(action=action, pull_request=SimpleNamespace(
                        draft=draft, head=SimpleNamespace(repo=SimpleNamespace(full_name=repo)))))
                self.assertEqual(bool(github_expression(expression, {"github": github}, allowed_functions={})), expected)
