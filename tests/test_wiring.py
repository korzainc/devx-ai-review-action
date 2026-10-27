"""Synthetic controller seam checks; no real image, GitHub call, or model."""
import json
import ast
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import test_publish

ROOT = Path(__file__).resolve().parents[1]


def github_equal(left, right):
    # Actions uses OrdinalIgnoreCase for strings; these command fixtures are ASCII.
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    return left == right


def github_expression(expression, values, *, allowed_functions):
    # Expression string literals have no escape sequences: '\n' stays a backslash and
    # an n (fromJSON then decodes it). Parse them as Python raw strings to match.
    expression = re.sub(r"'([^']*)'", r"r'\1'", expression)
    expression = re.sub(r"!(?!=)", " not ", expression)
    tree = ast.parse(expression.replace("&&", " and ").replace("||", " or "), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
                                ast.Compare, ast.Eq, ast.Call, ast.Name, ast.Load,
                                ast.Attribute, ast.Constant)):
            raise AssertionError("unsupported expression node: " + type(node).__name__)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_functions:
                raise AssertionError("function is unavailable in this expression context")

    class CaseInsensitiveEquality(ast.NodeTransformer):
        def visit_Compare(self, node):
            if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
                raise AssertionError("unsupported comparison")
            return ast.copy_location(ast.Call(func=ast.Name(id="github_equal", ctx=ast.Load()),
                args=[node.left, node.comparators[0]], keywords=[]), node)

    tree = ast.fix_missing_locations(CaseInsensitiveEquality().visit(tree))
    return eval(compile(tree, "actions-boundary", "eval"), {"__builtins__": {}},
                dict(values, github_equal=github_equal, **allowed_functions))


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_publish.PublisherTests("test_clean_review_posts_summary_with_no_inline_comments")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.event = self.root / "event.json"
        self.event.write_text("{}")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        docker = self.bin / "docker"
        docker.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, shutil, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
with (root / "analysis-calls").open("a") as stream:
    stream.write(json.dumps({"args": args, "env": dict(os.environ)}) + "\\n")
if args[0:2] == ["image", "inspect"] or args[0:1] == ["pull"] or args[0:2] == ["rm", "-f"]:
    sys.exit(0)
mounts = [args[i + 1] for i, item in enumerate(args) if item == "-v"]
target = pathlib.Path(next(m.split(":")[0] for m in mounts if m.endswith(":/github/out")))
source = root / "review"
for item in source.iterdir():
    if item.is_dir():
        shutil.copytree(item, target / item.name)
    else:
        shutil.copy2(item, target / item.name)
if os.environ.get("FIXTURE_SUMMARY_SYMLINK") == "1":
    (target / "summary.md").symlink_to(root / "host-target")
sys.exit(int(os.environ.get("FIXTURE_EXIT", "0")))
''')
        docker.chmod(0o755)
        timeout = self.bin / "timeout"
        timeout.write_text("#!/bin/sh\n[ \"${FIXTURE_TIMEOUT_EXIT:-0}\" = 0 ] || exit \"$FIXTURE_TIMEOUT_EXIT\"\nshift 3\nexec \"$@\"\n")
        timeout.chmod(0o755)
        self.env = dict(self.fixture.env, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        RUNNER_TEMP=str(self.runner), GITHUB_EVENT_PATH=str(self.event),
                        GITHUB_WORKSPACE=str(self.workspace), GITHUB_STEP_SUMMARY=str(self.root / "summary"),
                        FIXTURE_ROOT=str(self.root), FAIL_ON_ERROR="false", CHECK_RUN_ID="13",
                        REVIEW_TIMEOUT="600", REVIEW_CONFIG=".korza/review.yml", REVIEW_SKILLS="",
                        GITHUB_EVENT_NAME="issue_comment",
                        ENGINE_CONTAINER="korza-review-fixture")

    def review(self, code=0):
        (self.root / "outputs").write_text("")
        (self.root / "summary").write_text("")
        return subprocess.run(["bash", str(ROOT / "scripts/run.sh")],
                              env=dict(self.env, FIXTURE_EXIT=str(code)),
                              text=True, capture_output=True, timeout=12)

    def outputs(self):
        return dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())

    def test_contract_cli_can_construct_its_argument_parser(self):
        # Python 3.14's argparse imports inspect through its color formatter.
        # Helpers beside this script must not shadow the standard library.
        run = subprocess.run([sys.executable, str(ROOT / "scripts/contract.py"), "--help"],
                             env=self.env, text=True, capture_output=True, timeout=8)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("usage:", run.stdout)

    def test_oversized_timeout_cannot_wrap_into_an_allowed_budget(self):
        # On 64-bit Bash the oversized value wraps to 600 before validation.
        for value in ("0", "0000", "3601", "18446744073709552216"):
            with self.subTest(value=value):
                self.env["REVIEW_TIMEOUT"] = value
                run = self.review()
                self.assertIn("timeout-seconds must be 1 through 3600", run.stdout)
                self.assertEqual(self.outputs()["verdict"], "error")
                self.assertNotIn("review-completed", self.outputs())
                self.assertFalse((self.root / "analysis-calls").exists())

    def test_timeout_keeps_base_ten_leading_zero_normalization(self):
        for value, expected in [("1", "1"), ("3600", "3600"), ("09", "9"), ("0000000600", "600")]:
            with self.subTest(value=value):
                self.env["REVIEW_TIMEOUT"] = value
                (self.root / "analysis-calls").unlink(missing_ok=True)
                run = self.review()
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(self.outputs()["review-completed"], "true")
                calls = [json.loads(x) for x in (self.root / "analysis-calls").read_text().splitlines()]
                engine = next(c for c in calls if "run" in c["args"])
                self.assertEqual(engine["env"]["REVIEW_TIMEOUT"], expected)

    def test_completed_pass_has_host_manifest_and_private_analysis_authority(self):
        run = self.review()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        outputs = self.outputs()
        self.assertEqual(outputs["review-completed"], "true")
        manifest = json.loads(Path(outputs["completion-file"]).read_text())
        self.assertEqual(manifest["head_sha"], test_publish.SHA)
        self.assertEqual(manifest["review_verdict"], "pass")
        self.assertFalse(Path(outputs["completion-file"]).is_relative_to(Path(outputs["output-dir"])))
        calls = [json.loads(x) for x in (self.root / "analysis-calls").read_text().splitlines()]
        run_call = next(c for c in calls if "run" in c["args"])
        self.assertEqual(run_call["args"][-1], "action-review")
        self.assertNotIn("GITHUB_TOKEN", [run_call["args"][i + 1]
                           for i, item in enumerate(run_call["args"][:-1]) if item == "-e"])

    def test_completed_fail_remains_eligible_to_publish_and_blocking(self):
        self.fixture.action["verdict"] = {"status": "fail", "reason": "Blocking fixture",
                                            "blocking_findings": []}
        self.fixture.write_handoff()
        run = self.review()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.outputs()["review-completed"], "true")
        self.assertEqual(self.outputs()["verdict"], "fail")

    def test_host_marker_passes_unchanged_publisher_boundary(self):
        run = self.review()
        self.assertEqual(run.returncode, 0, run.stderr)
        outputs = self.outputs()
        self.fixture.env["OUTPUT_DIR"] = outputs["output-dir"]
        self.fixture.env["REVIEW_COMPLETION_FILE"] = outputs["completion-file"]
        published = self.fixture.run_publish()
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertEqual(len(self.fixture.calls()), 1)
        self.assertIn("delivery=success", (self.root / "outputs").read_text())

    def test_nonzero_engine_or_bad_handoff_never_completes(self):
        for mode in ("process", "handoff"):
            with self.subTest(mode=mode):
                if mode == "handoff":
                    (self.fixture.out / "publish/result.json").unlink()
                run = self.review(17 if mode == "process" else 0)
                outputs = self.outputs()
                self.assertNotEqual(outputs.get("review-completed"), "true")
                self.assertNotIn("completion-file", outputs)
                self.assertEqual(outputs["verdict"], "error")

    def test_nonzero_blocking_result_and_timeout_cannot_publish(self):
        self.fixture.action["verdict"] = {"status": "fail", "reason": "Blocking fixture",
                                            "blocking_findings": []}
        self.fixture.write_handoff()
        run = self.review(17)
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.outputs()["blocking-process-failure"], "true")
        self.assertNotEqual(self.outputs().get("review-completed"), "true")
        (self.root / "outputs").write_text("")
        run = subprocess.run(["bash", str(ROOT / "scripts/run.sh")],
                             env=dict(self.env, FIXTURE_TIMEOUT_EXIT="124"),
                             text=True, capture_output=True, timeout=8)
        self.assertIn("124", run.stdout)
        self.assertNotEqual(self.outputs().get("review-completed"), "true")
        self.assertEqual(self.outputs()["verdict"], "error")

    def test_contract_version_mismatch_prevents_completion(self):
        self.fixture.action["contract_version"] = 2
        self.fixture.write_handoff()
        self.review()
        self.assertEqual(self.outputs()["verdict"], "error")
        self.assertNotIn("completion-file", self.outputs())

    def test_blocking_reason_cannot_create_workflow_log_command(self):
        self.fixture.action["verdict"] = {"status": "fail",
            "reason": "Fixture\n::warning::forged workflow command",
            "blocking_findings": []}
        self.fixture.write_handoff()
        run = self.review()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.outputs()["review-completed"], "true")
        self.assertNotIn("::warning::forged", run.stdout + run.stderr)

    def test_engine_summary_symlink_cannot_redirect_host_write(self):
        host_target = self.root / "host-target"
        host_target.write_text("preserve host file")
        run = subprocess.run(["bash", str(ROOT / "scripts/run.sh")],
                             env=dict(self.env, FIXTURE_SUMMARY_SYMLINK="1"),
                             text=True, capture_output=True, timeout=8)
        self.assertEqual(host_target.read_text(), "preserve host file")

    def test_action_wires_publish_only_after_completed_review(self):
        action = (ROOT / "action.yml").read_text()
        step = action.split("    - name: Publish review\n", 1)[1].split("    - name:", 1)[0]
        self.assertIn("always() && !cancelled() && steps.review.outputs.review-completed == 'true'", step)
        self.assertIn("GITHUB_TOKEN: ${{ inputs.github-token }}", step)
        review = action.split("    - name: Review\n", 1)[1].split("    - name:", 1)[0]
        self.assertNotIn("GITHUB_TOKEN: ${{ inputs.github-token }}", review)
        final = action.split("    - name: Complete check\n", 1)[1].split("    - name:", 1)[0]
        self.assertIn("PUBLISH_OUTCOME: ${{ steps.publish.outcome }}", final)
        self.assertIn("DELIVERY: ${{ steps.publish.outputs.delivery }}", final)

    def test_example_filters_authorized_exact_command_before_concurrency(self):
        example = (ROOT / "examples/ai-review.yml").read_text()
        review = example.split("  review:\n", 1)[1]
        gate = review.split("    concurrency:\n", 1)[0]
        self.assertIn("github.event.comment.body", gate)
        self.assertIn("github.event.comment.author_association", gate)
        self.assertIn("OWNER", gate)
        self.assertIn("MEMBER", gate)
        self.assertIn("COLLABORATOR", gate)
        self.assertNotIn("startsWith", gate)

    def test_prepare_creates_check_only_on_fetched_head(self):
        (self.event).write_text(json.dumps({"pull_request": {
            "number": 7, "head": {"repo": {"full_name": "example/repo"}}}}))
        (self.bin / "git").write_text("#!" + sys.executable + "\n" + '''import os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["rev-parse", "--is-inside-work-tree"]: print("true")
elif args[:2] == ["rev-parse", "--is-shallow-repository"]: print("false")
elif args[:2] == ["rev-parse", "refs/korza/head"]: print("b" * 40)
elif args[0] == "merge-base": print("c" * 40)
elif args[0] == "fetch" and os.getenv("FIXTURE_FETCH_FAIL") == "1": sys.exit(1)
''')
        (self.bin / "git").chmod(0o755)
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ["api", "repos/example/repo/pulls/7"]:
    print(json.dumps({"state":"open", "base":{"ref":"main"},
                      "head":{"sha":"a" * 40}}))
elif any(a.endswith("/check-runs") for a in args):
    pathlib.Path(os.environ["FIXTURE_ROOT"], "check-args.json").write_text(json.dumps(args))
    print("13")
''')
        (self.bin / "gh").chmod(0o755)
        env = dict(self.env, TRIGGER_PHRASE="/review", ALLOWED_ASSOCIATIONS="OWNER",
                   CHECK_NAME="AI Code Review", GITHUB_OUTPUT=str(self.root / "prep.outputs"),
                   GITHUB_EVENT_NAME="pull_request")
        for fail in ("1", "0"):
            with self.subTest(fetch_fail=fail):
                (self.root / "prep.outputs").write_text("")
                check = self.root / "check-args.json"
                check.unlink(missing_ok=True)
                result = subprocess.run(["bash", str(ROOT / "scripts/prepare.sh")],
                                        env=dict(env, FIXTURE_FETCH_FAIL=fail),
                                        capture_output=True, text=True, timeout=8)
                if fail == "1":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(check.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("head_sha=" + "b" * 40, json.dumps(json.loads(check.read_text())))
                    self.assertIn("head-sha=" + "b" * 40, (self.root / "prep.outputs").read_text())

    def test_finalizer_requires_verified_delivery_and_preserves_blocking(self):
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
pathlib.Path(os.environ["FIXTURE_ROOT"], "final-patch.json").write_text(sys.stdin.read())
''')
        (self.bin / "gh").chmod(0o755)
        out = self.root / "final-review"
        out.mkdir()
        (out / "result.json").write_text(json.dumps({"verdict": {"reason": "Fixture verdict"}}))
        (out / "summary.md").write_text("Fixture summary")
        for verdict, review_outcome, delivery, publish_outcome, expected in (
            ("pass", "success", "success", "success", "success"),
            ("pass", "success", "error", "failure", "failure"),
            ("fail", "failure", "success", "success", "failure"),
            ("fail", "cancelled", "error", "cancelled", "cancelled"),
            ("pass", "success", "error", "cancelled", "cancelled"),
        ):
            with self.subTest(verdict=verdict, delivery=delivery, review_outcome=review_outcome):
                env = dict(self.env, OUTPUT_DIR=str(out), REVIEW_COMPLETED="true",
                           REVIEW_OUTCOME=review_outcome, VERDICT=verdict,
                           DELIVERY=delivery, PUBLISH_OUTCOME=publish_outcome,
                           JOB_CANCELLED="true" if publish_outcome == "cancelled" else "false")
                result = subprocess.run(["bash", str(ROOT / "scripts/finalize.sh")],
                                        env=env, capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads((self.root / "final-patch.json").read_text())
                self.assertEqual(payload["conclusion"], expected)
                if delivery != "success" and expected != "cancelled":
                    self.assertIn("delivery could not be verified", payload["output"]["summary"])

    def test_review_publish_finalize_actual_scripts_preserve_delivery_and_blocking(self):
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import os, pathlib, sys
pathlib.Path(os.environ["FIXTURE_ROOT"], "final-patch.json").write_text(sys.stdin.read())
''')
        (self.bin / "gh").chmod(0o755)
        for verdict, mode, expected in (("pass", "ok", "success"),
                                         ("fail", "ok", "failure"),
                                         ("pass", "missing", "failure"),
                                         ("pass", "hang", "failure")):
            with self.subTest(verdict=verdict, publisher=mode):
                self.fixture.action["verdict"]["status"] = verdict
                self.fixture.write_handoff()
                review = self.review()
                reviewed = self.outputs()
                self.assertEqual(reviewed["review-completed"], "true")
                self.fixture.env.update(OUTPUT_DIR=reviewed["output-dir"],
                                        REVIEW_COMPLETION_FILE=reviewed["completion-file"],
                                        PUBLISH_TIMEOUT="1")
                published = self.fixture.run_publish(mode)
                delivered = self.outputs()
                env = dict(self.env, REVIEW_COMPLETED=reviewed["review-completed"],
                           VERDICT=reviewed["verdict"], RESULT_FILE=reviewed["result-file"],
                           SUMMARY_FILE=reviewed["summary-file"],
                           REVIEW_OUTCOME="success" if review.returncode == 0 else "failure",
                           PUBLISH_OUTCOME="success" if published.returncode == 0 else "failure",
                           DELIVERY=delivered["delivery"], JOB_CANCELLED="false")
                finalized = subprocess.run(["bash", str(ROOT / "scripts/finalize.sh")],
                                           env=env, text=True, capture_output=True, timeout=8)
                self.assertEqual(finalized.returncode, 0, finalized.stderr)
                payload = json.loads((self.root / "final-patch.json").read_text())
                self.assertEqual(payload["conclusion"], expected)
                if mode != "ok":
                    self.assertEqual(delivered["delivery"], "error")
                    self.assertIn("Do not retry automatically", payload["output"]["summary"])

    def test_hanging_review_cleanup_reaches_completion_and_finalization(self):
        docker = self.bin / "docker"
        docker.write_text(docker.read_text().replace(
            'if args[0:2] == ["image", "inspect"]',
            'if args[:2] == ["rm", "-f"]:\n'
            '    import time\n'
            '    (root / "cleanup-entered").write_text("true")\n'
            '    time.sleep(30)\n'
            'if args[0:2] == ["image", "inspect"]'))
        # Exercise a real stalled executable, with a shortened synthetic timeout.
        # The asserted production command still has the full 10+5 second bound.
        (self.bin / "timeout").write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, signal, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FIXTURE_ROOT"])
if args[3:6] != ["docker", "rm", "-f"]:
    os.execvp(args[3], args[3:])
with (root / "cleanup-timeouts").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
assert args[:3] == ["--signal=TERM", "--kill-after=5", "10"], args
child = subprocess.Popen(args[3:], start_new_session=True)
try:
    sys.exit(child.wait(timeout=0.2))
except subprocess.TimeoutExpired:
    os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    sys.exit(124)
''')
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import os, pathlib, sys
pathlib.Path(os.environ["FIXTURE_ROOT"], "final-patch.json").write_text(sys.stdin.read())
''')
        (self.bin / "gh").chmod(0o755)
        for engine_code, verdict, expected in ((0, "pass", "success"), (17, "fail", "failure")):
            with self.subTest(engine_code=engine_code):
                self.fixture.action["verdict"]["status"] = verdict
                self.fixture.write_handoff()
                (self.root / "outputs").write_text("")
                (self.root / "summary").write_text("")
                process = subprocess.Popen(["bash", str(ROOT / "scripts/run.sh")],
                    env=dict(self.env, FIXTURE_EXIT=str(engine_code)), start_new_session=True,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    stdout, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                    self.fail("Stalled review cleanup prevented controller completion")
                self.assertTrue((self.root / "cleanup-entered").exists())
                reviewed = self.outputs()
                self.assertEqual(process.returncode, 0 if engine_code == 0 else 1, stdout + stderr)
                action = (ROOT / "action.yml").read_text()
                step = action.split("    - name: Clean up review container\n", 1)[1].split("    - name:", 1)[0]
                command = next(line.split("run: ", 1)[1] for line in step.splitlines() if "run: " in line)
                fallback = subprocess.Popen(["bash", "-c", command],
                    env=self.env,
                    start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    fallback.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    import signal
                    os.killpg(fallback.pid, signal.SIGKILL)
                    fallback.communicate()
                    self.fail("Stalled always cleanup prevented finalization")
                self.assertEqual(fallback.returncode, 0)
                delivered = {}
                if engine_code == 0:
                    self.assertEqual(reviewed["review-completed"], "true")
                    self.fixture.env.update(OUTPUT_DIR=reviewed["output-dir"],
                                            REVIEW_COMPLETION_FILE=reviewed["completion-file"])
                    published = self.fixture.run_publish()
                    self.assertEqual(published.returncode, 0, published.stderr)
                    delivered = self.outputs()
                else:
                    self.assertNotIn("review-completed", reviewed)
                    self.assertEqual(reviewed["blocking-process-failure"], "true")
                finalized = subprocess.run(["bash", str(ROOT / "scripts/finalize.sh")],
                    env=dict(self.env, REVIEW_COMPLETED=reviewed.get("review-completed", "false"),
                        VERDICT=reviewed["verdict"], RESULT_FILE=reviewed["result-file"],
                        SUMMARY_FILE=reviewed["summary-file"],
                        BLOCKING_PROCESS_FAILURE=reviewed.get("blocking-process-failure", "false"),
                        REVIEW_OUTCOME="success" if engine_code == 0 else "failure",
                        PUBLISH_OUTCOME="success" if delivered else "skipped",
                        DELIVERY=delivered.get("delivery", "")),
                    text=True, capture_output=True, timeout=3)
                self.assertEqual(finalized.returncode, 0, finalized.stderr)
                payload = json.loads((self.root / "final-patch.json").read_text())
                self.assertEqual(payload["conclusion"], expected)
        self.assertEqual(len((self.root / "cleanup-timeouts").read_text().splitlines()), 4)

    def test_composite_and_reusable_workflows_gate_cancellation_on_exact_authorized_command(self):
        reusable = (ROOT / ".github/workflows/ai-review.yml").read_text()
        example = (ROOT / "examples/ai-review.yml").read_text()
        cases = [("issue_comment", "/review", role, True) for role in ("OWNER", "MEMBER", "COLLABORATOR")]
        # The comment box submits a trailing newline when the author presses Enter.
        cases += [("issue_comment", command, "MEMBER", True)
                  for command in ("/Review", "/REVIEW", "/review\n", "/review\r\n")]
        cases += [("issue_comment", command, role, False) for command, role in
                  (("/review", "CONTRIBUTOR"), ("/review invalid", "MEMBER"),
                   ("/reviewdog", "MEMBER"), ("ordinary comment", "MEMBER"))]
        cases.append(("pull_request", "/review", "MEMBER", False))
        for source, text in (("reusable", reusable), ("example", example)):
            review = text.split("  review:\n", 1)[1]
            lines = review.splitlines()
            index = next(i for i, line in enumerate(lines) if line.startswith("    if:"))
            expression = lines[index].split("if:", 1)[1].strip()
            if expression == ">-":
                expression = " ".join(line.strip() for line in lines[index + 1:]
                    [:next(i for i, line in enumerate(lines[index + 1:]) if not line.startswith("      "))])
            for event, command, association, expected in cases:
                with self.subTest(source=source, command=command, association=association, event=event):
                    github = SimpleNamespace(event_name=event, event=SimpleNamespace(
                        issue=SimpleNamespace(pull_request=True),
                        comment=SimpleNamespace(body=command, author_association=association)))
                    eligible = github_expression(expression, {"github": github}, allowed_functions={
                        "contains": lambda values, item: any(github_equal(value, item) for value in values),
                        "fromJSON": json.loads,
                        "startsWith": lambda value, prefix: value.lower().startswith(prefix.lower())})
                    can_cancel = "\nconcurrency:" in text or (eligible and "\n    concurrency:" in review)
                    self.assertEqual(bool(eligible), expected)
                    self.assertEqual(bool(can_cancel), expected)

    def test_every_admitted_command_case_reaches_prepare_check_creation(self):
        # Catch the real boundary break: /Review enters job concurrency, then
        # prepare skips it and leaves the cancelled review without a replacement.
        (self.bin / "git").write_text("#!" + sys.executable + "\n" + '''import sys
args = sys.argv[1:]
if args[:2] == ["rev-parse", "--is-inside-work-tree"]: print("true")
elif args[:2] == ["rev-parse", "--is-shallow-repository"]: print("false")
elif args[:2] == ["rev-parse", "refs/korza/head"]: print("a" * 40)
elif args[0] == "merge-base": print("c" * 40)
''')
        (self.bin / "git").chmod(0o755)
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ["api", "repos/example/repo/pulls/7"]:
    print(json.dumps({"state":"open", "base":{"ref":"main"}, "head":{"sha":"a" * 40}}))
elif any(a.endswith("/check-runs") for a in args):
    pathlib.Path(os.environ["FIXTURE_ROOT"], "comment-check.json").write_text(json.dumps(args))
    print("13")
''')
        (self.bin / "gh").chmod(0o755)
        for command, allowed in (("/review", True), ("/Review", True), ("/REVIEW", True),
                                 ("/reviewdog", False)):
            with self.subTest(command=command):
                self.event.write_text(json.dumps({"issue": {"number": 7, "pull_request": {}},
                    "comment": {"id": 9, "body": command, "author_association": "MEMBER",
                                "user": {"login": "fixture"}}}))
                (self.root / "outputs").write_text("")
                check = self.root / "comment-check.json"
                check.unlink(missing_ok=True)
                prepared = subprocess.run(["bash", str(ROOT / "scripts/prepare.sh")],
                    env=dict(self.env, TRIGGER_PHRASE="/review", ALLOWED_ASSOCIATIONS="MEMBER",
                             CHECK_NAME="AI Code Review"), text=True, capture_output=True, timeout=8)
                self.assertEqual(prepared.returncode, 0, prepared.stderr)
                self.assertEqual(check.exists(), allowed, prepared.stdout)
                self.assertEqual(self.outputs().get("run") == "true", allowed)

    def test_manifest_cancellation_env_uses_permitted_context_and_drives_finalizer(self):
        # action_yaml.json step-env permits job but no status functions. Exercise
        # this action's actual expression and the finalizer, not a YAML grep.
        final_step = (ROOT / "action.yml").read_text().split("    - name: Complete check\n", 1)[1]
        value = next(line.split("JOB_CANCELLED:", 1)[1].strip() for line in final_step.splitlines()
                     if "JOB_CANCELLED:" in line)
        expression = value.removeprefix("${{").removesuffix("}}").strip()
        (self.bin / "gh").write_text("#!" + sys.executable + "\n" + '''import os, pathlib, sys
pathlib.Path(os.environ["FIXTURE_ROOT"], "cancel-final-patch.json").write_text(sys.stdin.read())
''')
        (self.bin / "gh").chmod(0o755)
        for status, expected in (("success", "success"), ("failure", "success"),
                                 ("cancelled", "cancelled")):
            with self.subTest(job_status=status):
                cancelled = github_expression(expression, {"job": SimpleNamespace(status=status)},
                                              allowed_functions={})
                finalized = subprocess.run(["bash", str(ROOT / "scripts/finalize.sh")],
                    env=dict(self.env, JOB_CANCELLED=str(cancelled).lower(), REVIEW_COMPLETED="true",
                             REVIEW_OUTCOME="success", VERDICT="pass", DELIVERY="success",
                             PUBLISH_OUTCOME="success"), text=True, capture_output=True, timeout=8)
                self.assertEqual(finalized.returncode, 0, finalized.stderr)
                payload = json.loads((self.root / "cancel-final-patch.json").read_text())
                self.assertEqual(payload["conclusion"], expected)


if __name__ == "__main__":
    unittest.main()
