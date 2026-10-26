#!/usr/bin/env python3
"""Offline integration tests of the real Bash wrappers and action env wiring.

Docker and gh are the external boundaries: no image, account, or network needed.
Run with: python3 -m unittest discover -s tests -v
"""

import concurrent.futures
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SARIF = '{"version":"2.1.0","runs":[]}'


def result(status, findings=None):
    return json.dumps({
        "contract_version": 1,
        "verdict": {"status": status, "reason": "Fixture " + status,
                    "blocking_findings": ["example"] if status == "fail" and findings else []},
        "findings": findings or [], "skills_run": ["bug-hunter"],
        "provenance": {"engine_version": "fixture", "image_digest": "", "model": "none"},
    })


FINDING = {"id": "example", "rule_id": "fixture/check", "skill": "bug-hunter",
           "severity": "high", "confidence": 1, "enforcing": True,
           "location": {"path": "file.py", "start_line": 2, "end_line": 2},
           "message": "Fixture finding", "suggestion": ""}


# Shared start of every fake docker: answer the calls around the engine run
# (image inspect, pull, rm -f), then check the run itself and record out_dir.
DOCKER_PRELUDE = '''import json, os, pathlib, sys
args = sys.argv[1:]
if args[:2] == ["image", "inspect"]:
    sys.exit(1)
if args[:1] == ["pull"]:
    sys.exit(int(os.environ.get("FAKE_PULL_EXIT", "0")))
if args[:2] == ["rm", "-f"]:
    assert args[2] == os.environ["ENGINE_CONTAINER"], args
    sys.exit(0)
assert args[:4] == ["run", "--rm", "--init", "--name"], args
assert args[4] == os.environ["ENGINE_CONTAINER"], args
assert "KORZA_CONTRACT_VERSION=1" in args, args
mounts = [args[i+1] for i, arg in enumerate(args[:-1]) if arg == "-v"]
out = pathlib.Path(next(m[:-len(":/github/out")] for m in mounts if m.endswith(":/github/out")))
pathlib.Path(os.environ["DOCKER_RECORD"]).write_text(str(out))
'''


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # The handoff reader rejects symlinked parent components; macOS /var
        # aliases /private/var, while native hosted-runner paths are canonical.
        self.base = Path(self.temp.name).resolve()
        for name in ("bin", "home", "runner", "workspace"):
            (self.base / name).mkdir()
        (self.base / "event.json").write_text('{"number":1}')
        self.env = {
            "PATH": str(self.base / "bin") + os.pathsep + os.environ["PATH"],
            "HOME": str(self.base / "home"),
            "RUNNER_TEMP": str(self.base / "runner"),
            "GITHUB_WORKSPACE": str(self.base / "workspace"),
            "GITHUB_EVENT_PATH": str(self.base / "event.json"),
            "GITHUB_REPOSITORY": "fixture/repo", "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_API_URL": "https://api.github.com", "PR_NUMBER": "1",
            "BASE_SHA": "c" * 40, "HEAD_SHA": "a" * 40, "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_ID": "42", "CHECK_RUN_ID": "7", "REVIEW_IMAGE": "ghcr.io/fixture/engine@sha256:" + "b" * 64,
            "ENGINE_CONTAINER": "korza-review-fixture", "REVIEW_TIMEOUT": "600",
            "FAKE_RESULT": "", "FAKE_EXIT": "0", "FAKE_SARIF": "", "FAKE_KIND": "file",
            "FAKE_PULL_EXIT": "0", "FAKE_PLANT": "",
        }
        # macOS has no GNU timeout in this environment. This fixture models its
        # command boundary only; native deadline/process-group tests are separate.
        self.executable("timeout", '''import os, sys
args = sys.argv[1:]
while args[0].startswith("--"): args = args[1:]
os.execvp(args[1], args[1:])
''')
        self.executable("docker", DOCKER_PRELUDE + '''
assert os.environ["GITHUB_WORKSPACE"] + ":/workspace:ro" in mounts
assert os.environ["GITHUB_EVENT_PATH"] + ":/github/event.json:ro" in mounts
if os.environ["FAKE_PLANT"]:
    # An engine planting symlinks to a runner file the host would write or upload.
    for name in ("summary.md", "results.sarif"):
        (out / name).symlink_to(os.environ["FAKE_PLANT"])
if os.environ["FAKE_KIND"] == "symlink":
    (out / "result.json").symlink_to(os.environ["FAKE_TARGET"])
elif os.environ["FAKE_KIND"] == "directory":
    (out / "result.json").mkdir()
elif os.environ["FAKE_RESULT"]:
    (out / "result.json").write_text(os.environ["FAKE_RESULT"])
if os.environ["FAKE_SARIF"] and not os.environ["FAKE_PLANT"]:
    (out / "results.sarif").write_text(os.environ["FAKE_SARIF"])
# The real published engine also emits the typed publisher handoff. Keep
# malformed top-result scenarios unchanged; only complete fixtures get this seam.
try: top = json.loads(os.environ["FAKE_RESULT"])
except ValueError: top = None
if isinstance(top, dict):
    handoff = out / "publish"
    handoff.mkdir()
    findings = []
    for finding in top.get("findings", []) if isinstance(top.get("findings"), list) else []:
        if not isinstance(finding, dict): continue
        location = finding.get("location", {})
        findings.append({"id": finding["id"], "reviewer": finding["skill"], "unit_id": "fixture",
            "file": location.get("path"), "start_line": location.get("start_line"),
            "end_line": location.get("end_line"), "severity": finding["severity"],
            "category": "fixture", "title": "Fixture", "body": finding["message"],
            "confidence": finding["confidence"], "suggestion": finding.get("suggestion")})
    (handoff / "result.json").write_text(json.dumps({"schema_version": 1, "run_id": "fixture",
        "head_sha": "a" * 40, "findings": findings, "provenance": {"package_version": "fixture", "provider": "fixture"}}))
    (handoff / "snapshot.json").write_text(json.dumps({"schema_version": 1, "provider": "github", "repo": "fixture/repo",
        "pr": {"number": 1, "url": "https://github.com/fixture/repo/pull/1", "title": "Fixture"},
        "refs": {"base_ref": "main", "base_sha": "c" * 40, "head_ref": "branch", "head_sha": "a" * 40,
                 "merge_base_sha": "c" * 40}, "diff_path": "diff.patch", "workspace": "/workspace"}))
sys.exit(int(os.environ["FAKE_EXIT"]))
''')
        self.executable("gh", '''import json, os, pathlib, sys
assert sys.argv[1:] == ["api", "-X", "PATCH", "repos/fixture/repo/check-runs/7", "--input", "-"], sys.argv
payload = json.load(sys.stdin)
pathlib.Path(os.environ["GH_RECORD"]).write_text(json.dumps(payload))
''')

    def executable(self, name, body):
        script = self.base / "bin" / name
        script.write_text("#!" + sys.executable + "\n" + body)
        script.chmod(0o755)

    def run_review(self, content="", engine_exit=0, policy="false", name="run", **extra):
        output = self.base / (name + ".outputs")
        summary = self.base / (name + ".summary")
        record = self.base / (name + ".docker")
        output.write_text("")
        summary.write_text("")
        env = dict(self.env, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary),
                   DOCKER_RECORD=str(record), FAKE_RESULT=content, FAKE_EXIT=str(engine_exit),
                   FAIL_ON_ERROR=policy)
        env.update(extra)
        proc = subprocess.run(["bash", str(ROOT / "scripts/run.sh")], env=env,
                              capture_output=True, text=True, timeout=15)
        lines = output.read_text().splitlines() if output.exists() else []
        for line in lines:
            self.assertIn("=", line, "Invalid GitHub step output")
        outputs = dict(line.split("=", 1) for line in lines)
        # No record when the engine never ran (a failed pull).
        directory = Path(record.read_text()) if record.exists() else None
        return proc, outputs, directory, env

    def finalize(self, outputs, env, outcome=None):
        # Resolve the actual Complete check env declarations. This catches a
        # missing/misnamed private wire, instead of supplying it only in tests.
        action = (ROOT / "action.yml").read_text()
        step = action.split("    - name: Complete check\n", 1)[1].split("    - name:", 1)[0]
        values = {"inputs.github-token": "", "inputs.fail-on-error": env["FAIL_ON_ERROR"],
                  "steps.prep.outputs.check-run-id": "7", "steps.review.outcome": outcome or "success"}
        values.update({"steps.review.outputs." + key: value for key, value in outputs.items()})
        # These producer-lifecycle scenarios supply a synthetic verified delivery
        # only after host attestation. test_wiring drives real publish.sh as well.
        values["steps.publish.outcome"] = "success" if outputs.get("review-completed") == "true" else "skipped"
        values["steps.publish.outputs.delivery"] = "success" if outputs.get("review-completed") == "true" else ""
        values["job.status == 'cancelled'"] = "true" if outcome == "cancelled" else "false"
        final_env = dict(env, GH_RECORD=str(self.base / (Path(env["GITHUB_OUTPUT"]).stem + ".patch")))
        for key, expression in re.findall(r"^        ([A-Z_]+): \$\{\{ ([^}]+) \}\}$", step, re.M):
            final_env[key] = values.get(expression.strip(), "")
        proc = subprocess.run(["bash", str(ROOT / "scripts/finalize.sh")], env=final_env,
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        patch = json.loads(Path(final_env["GH_RECORD"]).read_text())
        self.assertEqual(patch["status"], "completed")
        self.assertEqual(patch["details_url"], "https://github.com/fixture/repo/actions/runs/42")
        return patch

    def lifecycle(self, content="", engine_exit=0, policy="false", **extra):
        proc, outputs, directory, env = self.run_review(content, engine_exit, policy, **extra)
        patch = self.finalize(outputs, env, "success" if proc.returncode == 0 else "failure")
        return proc, outputs, directory, patch

    def test_completed_results_preserve_outputs_check_and_sarif(self):
        for status, findings, code, conclusion in (("pass", [], 0, "success"),
                                                  ("pass", [FINDING], 0, "success"),
                                                  ("fail", [FINDING], 1, "failure")):
            for policy in ("false", "true"):
                with self.subTest(status=status, findings=len(findings), policy=policy):
                    proc, outputs, directory, patch = self.lifecycle(result(status, findings), policy=policy,
                                                                    FAKE_SARIF=SARIF)
                    self.assertEqual(proc.returncode, code, proc.stderr)
                    self.assertEqual(outputs["verdict"], status)
                    self.assertEqual(outputs["findings"], str(len(findings)))
                    self.assertEqual(Path(outputs["sarif-file"]).read_text(), SARIF)
                    self.assertEqual(Path(outputs["sarif-file"]).parent, directory)
                    self.assertEqual(patch["conclusion"], conclusion)
                    self.assertEqual(patch["output"]["title"], "Fixture " + status)
                    self.assertIn("### AI Code Review: " + status, patch["output"]["summary"])

    def test_incomplete_results_use_error_policy_without_public_artifacts(self):
        for content in ("", "{", "{}", "[]", "null", result("unknown"), result("error"),
                        '{"verdict":{"status":"pass"}}', result("pass") + result("fail")):
            for policy, code, conclusion in (("false", 0, "neutral"), ("true", 1, "failure")):
                with self.subTest(content=content, policy=policy):
                    proc, outputs, _, patch = self.lifecycle(content, policy=policy, FAKE_SARIF=SARIF)
                    self.assertEqual(proc.returncode, code, proc.stderr)
                    self.assertEqual(outputs.get("verdict"), "error")
                    self.assertNotIn("findings", outputs)
                    self.assertNotIn("sarif-file", outputs)
                    self.assertEqual(patch["conclusion"], conclusion)
                    self.assertEqual(patch["output"]["title"], "Review could not complete")

    def test_nonzero_engine_never_completes_review_and_preserves_blocking_failure(self):
        for content, blocking in ((result("pass"), False), (result("fail", [FINDING]), True),
                                  (result("error"), False), ("", False), ("{", False)):
            for policy in ("false", "true"):
                with self.subTest(content=content, policy=policy):
                    proc, outputs, _, patch = self.lifecycle(content, 17, policy, FAKE_SARIF=SARIF)
                    self.assertEqual(outputs.get("verdict"), "error")
                    self.assertNotIn("findings", outputs)
                    self.assertNotIn("sarif-file", outputs)
                    expected = "failure" if blocking or policy == "true" else "neutral"
                    self.assertEqual(patch["conclusion"], expected)
                    self.assertEqual(proc.returncode, 1 if expected == "failure" else 0)
                    self.assertEqual(patch["output"]["title"], "Review could not complete")
                    self.assertIn("17", patch["output"]["summary"])

    def test_error_reason_stays_in_summary_without_becoming_workflow_commands(self):
        payload = json.loads(result("error"))
        payload["verdict"]["reason"] = "Interrupted.\n::error::fixture-untrusted-command"
        for policy in ("false", "true"):
            with self.subTest(policy=policy):
                proc, outputs, _, patch = self.lifecycle(json.dumps(payload), policy=policy)
                self.assertEqual(outputs["verdict"], "error")
                self.assertNotIn("fixture-untrusted-command", proc.stdout)
                self.assertIn(payload["verdict"]["reason"], patch["output"]["summary"])

    def test_fail_verdict_stays_blocking_if_findings_validation_or_rendering_fails(self):
        for findings in ("missing", None, {}, ["bad-member"]):
            payload = json.loads(result("fail"))
            if findings == "missing":
                del payload["findings"]
            else:
                payload["findings"] = findings
            for policy in ("false", "true"):
                with self.subTest(findings=findings, policy=policy):
                    proc, outputs, _, patch = self.lifecycle(json.dumps(payload), policy=policy, FAKE_SARIF=SARIF)
                    self.assertEqual(proc.returncode, 1)
                    self.assertEqual(outputs["verdict"], "error")
                    self.assertNotIn("findings", outputs)
                    self.assertNotIn("sarif-file", outputs)
                    self.assertEqual(patch["conclusion"], "failure")
                    self.assertEqual(patch["output"]["title"], "Review could not complete")
                    self.assertNotIn("### AI Code Review: fail", patch["output"]["summary"])

    def test_cancelled_incomplete_blocking_result_overrides_private_failure_signal(self):
        for policy in ("false", "true"):
            proc, outputs, _, env = self.run_review(result("fail"), 17, policy)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(outputs.get("blocking-process-failure"), "true")
            patch = self.finalize(outputs, env, "cancelled")
            self.assertEqual(patch["conclusion"], "cancelled")
            self.assertEqual(patch["output"]["title"], "Review cancelled")

    def test_later_step_summary_write_failure_retains_known_blocking_result(self):
        unavailable = self.base / "summary-directory"
        unavailable.mkdir()
        for policy in ("false", "true"):
            proc, outputs, _, patch = self.lifecycle(result("fail"), policy=policy,
                                                    GITHUB_STEP_SUMMARY=str(unavailable), FAKE_SARIF=SARIF)
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(outputs["verdict"], "error")
            self.assertEqual(outputs.get("blocking-process-failure"), "true")
            self.assertNotIn("findings", outputs)
            self.assertNotIn("sarif-file", outputs)
            self.assertEqual(patch["conclusion"], "failure")

    def test_nonzero_json_stream_is_not_trusted_as_a_blocking_result(self):
        for policy, conclusion in (("false", "neutral"), ("true", "failure")):
            _, outputs, _, patch = self.lifecycle(result("pass") + result("fail"), 17, policy)
            self.assertEqual(outputs["verdict"], "error")
            self.assertNotIn("blocking-process-failure", outputs)
            self.assertEqual(patch["conclusion"], conclusion)

    def test_cancellation_overrides_pass_and_fail(self):
        for status in ("pass", "fail"):
            for policy in ("false", "true"):
                with self.subTest(status=status, policy=policy):
                    _, outputs, _, env = self.run_review(result(status), policy=policy)
                    patch = self.finalize(outputs, env, "cancelled")
                    self.assertEqual(patch["conclusion"], "cancelled")
                    self.assertEqual(patch["output"]["title"], "Review cancelled")
                    self.assertNotIn("### AI Code Review: " + status, patch["output"]["summary"])

    def test_failed_review_step_cannot_report_success_from_pass_output(self):
        for policy, conclusion in (("false", "neutral"), ("true", "failure")):
            _, outputs, _, env = self.run_review(result("pass"), policy=policy)
            patch = self.finalize(outputs, env, "failure")
            self.assertEqual(patch["conclusion"], conclusion)
            self.assertEqual(patch["output"]["title"], "Review could not complete")
            self.assertNotIn("### AI Code Review: pass", patch["output"]["summary"])

    def test_repeated_invocations_do_not_reuse_a_prior_result_or_summary(self):
        _, first, first_dir, _ = self.lifecycle(result("pass"), FAKE_SARIF=SARIF, name="first")
        _, second, second_dir, patch = self.lifecycle("", 17, name="second")
        self.assertNotEqual(first_dir, second_dir)
        self.assertEqual(second["verdict"], "error")
        self.assertEqual(patch["conclusion"], "neutral")
        self.assertNotIn("Fixture pass", patch["output"]["summary"])
        self.assertEqual(Path(first["sarif-file"]).read_text(), SARIF)

    def test_concurrent_invocations_keep_results_and_sarif_separate(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(self.lifecycle, result(status), name=status, FAKE_SARIF=SARIF)
                       for status in ("pass", "fail")]
            passed, failed = [future.result() for future in pending]
        self.assertNotEqual(passed[2], failed[2])
        self.assertEqual(passed[1]["verdict"], "pass")
        self.assertEqual(failed[1]["verdict"], "fail")
        self.assertEqual(passed[3]["conclusion"], "success")
        self.assertEqual(failed[3]["conclusion"], "failure")
        for run in (passed, failed):
            self.assertEqual(Path(run[1]["sarif-file"]).read_text(), SARIF)

    def test_finalizer_without_review_outputs_does_not_read_old_shared_files(self):
        old = self.base / "runner/korza-review"
        old.mkdir()
        (old / "result.json").write_text(result("pass"))
        (old / "summary.md").write_text("STALE PRIVATE SUMMARY")
        env = dict(self.env, FAIL_ON_ERROR="false", GITHUB_OUTPUT=str(self.base / "skip.outputs"))
        patch = self.finalize({}, env, "skipped")
        self.assertEqual(patch["conclusion"], "neutral")
        self.assertNotIn("STALE", patch["output"]["summary"])

    def test_failed_engine_only_uses_bounded_regular_result_for_blocking_signal(self):
        target = self.base / "elsewhere.json"
        target.write_text(result("fail"))
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind):
                proc, outputs, _, patch = self.lifecycle("", 17, FAKE_KIND=kind, FAKE_TARGET=str(target), name=kind)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(outputs.get("verdict"), "error")
                self.assertEqual(patch["conclusion"], "neutral")
        # Oversized failed-process output is refused without parsing and blocks.
        oversized = result("fail") + " " * (1024 * 1024)
        # Keep large payload out of exec's environment-size limit.
        self.executable("docker", DOCKER_PRELUDE + '''
(out / "result.json").write_text(pathlib.Path(os.environ["FAKE_TARGET"]).read_text())
sys.exit(17)
''')
        target.write_text(oversized)
        for policy in ("false", "true"):
            proc, outputs, _, patch = self.lifecycle("", 17, policy, FAKE_TARGET=str(target), name="large")
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(outputs.get("verdict"), "error")
            self.assertNotIn("findings", outputs)
            self.assertNotIn("sarif-file", outputs)
            self.assertEqual(patch["conclusion"], "failure")

    def test_nonzero_exit_keeps_the_engine_reason_and_names_kills(self):
        payload = json.loads(result("error"))
        payload["verdict"]["reason"] = "Engine budget exhausted after 600s."
        _, outputs, _, patch = self.lifecycle(json.dumps(payload), 143)
        self.assertEqual(outputs["verdict"], "error")
        self.assertEqual(patch["conclusion"], "neutral")
        self.assertIn("Engine exited with 143", patch["output"]["summary"])
        self.assertIn("Engine budget exhausted after 600s.", patch["output"]["summary"])
        # 137 well inside the budget is a kill (OOM, or cleanup on cancel), not a timeout.
        _, _, _, patch = self.lifecycle("", 137, name="killed")
        self.assertIn("killed", patch["output"]["summary"])
        self.assertNotIn("timed out", patch["output"]["summary"])

    def test_contract_version_is_required_but_never_unblocks_a_fail(self):
        for version, status, conclusion in ((None, "pass", "neutral"), (2, "pass", "neutral"),
                                            ("1.4", "pass", "success"), (None, "fail", "failure")):
            payload = json.loads(result(status))
            if version is None:
                del payload["contract_version"]
            else:
                payload["contract_version"] = version
            with self.subTest(version=version, status=status):
                _, outputs, _, patch = self.lifecycle(json.dumps(payload), name="cv")
                self.assertEqual(patch["conclusion"], conclusion)
                if conclusion != "success":
                    self.assertEqual(outputs["verdict"], "error")
                    self.assertIn("contract", patch["output"]["summary"])

    def test_pull_failure_is_an_incomplete_review(self):
        for policy, code, conclusion in (("false", 0, "neutral"), ("true", 1, "failure")):
            proc, outputs, _, env = self.run_review(result("pass"), policy=policy, FAKE_PULL_EXIT="1")
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertEqual(outputs["verdict"], "error")
            patch = self.finalize(outputs, env, "success" if code == 0 else "failure")
            self.assertEqual(patch["conclusion"], conclusion)
            self.assertIn("Could not pull", patch["output"]["summary"])

    def test_engine_planted_symlinks_are_neither_written_nor_published(self):
        victim = self.base / "runner-file"
        victim.write_text("ORIGINAL")
        for content, code in ((result("pass", [FINDING]), 0), ("", 0), (result("pass"), 17)):
            with self.subTest(content=content[:20], code=code):
                _, outputs, directory, _ = self.lifecycle(content, code, FAKE_PLANT=str(victim), name="plant")
                self.assertEqual(victim.read_text(), "ORIGINAL")
                self.assertNotIn("sarif-file", outputs)
                self.assertFalse(Path(outputs["summary-file"]).is_symlink())

    def test_fail_reason_cannot_inject_workflow_commands(self):
        payload = json.loads(result("fail", [FINDING]))
        payload["verdict"]["reason"] = "Blocked.\n::add-mask::fixture-injected\r\n::warning::x"
        proc, outputs, _, patch = self.lifecycle(json.dumps(payload))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(outputs["verdict"], "fail")
        self.assertEqual(patch["conclusion"], "failure")
        command_lines = [line for line in proc.stdout.splitlines() if line.startswith("::")]
        self.assertEqual(len(command_lines), 1, command_lines)
        self.assertIn("%0A::add-mask::", command_lines[0])


if __name__ == "__main__":
    unittest.main()
