"""Real Bash/Python boundary tests; Docker alone is a local executable fixture."""
import copy
import hashlib
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHA, BASE = "a" * 40, "c" * 40
IMAGE = "ghcr.io/example/engine@sha256:" + "b" * 64
URL = "https://github.com/example/repo/pull/7#issuecomment-123"


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.kill_fixture_container)
        self.out = self.root / "review"
        (self.out / "publish").mkdir(parents=True)
        self.context = self.root / "completion.json"
        self.result = {"schema_version": 1, "run_id": "review-123", "head_sha": SHA,
                       "findings": [], "dropped": [], "skipped": [], "not_reviewed": [],
                       "stopped_at_budget": False,
                       "provenance": {"package_version": "0.1.0", "provider": "anthropic"}}
        self.snapshot = {"schema_version": 1, "provider": "github", "repo": "example/repo",
                         "pr": {"number": 7, "url": "https://github.com/example/repo/pull/7",
                                "title": "Fixture"},
                         "refs": {"base_ref": "main", "base_sha": BASE, "head_ref": "branch",
                                  "head_sha": SHA, "merge_base_sha": BASE},
                         "diff_path": "diff.patch", "workspace": "/workspace"}
        self.action = {"contract_version": 1,
                       "verdict": {"status": "pass", "reason": "No blocking findings.",
                                   "blocking_findings": []}, "findings": [],
                       "skills_run": [], "provenance": {}}
        self.receipt = {"publisher": "github", "posted_comments": 0, "couldnt_place": 0,
                        "head_moved": False, "summary_only": False, "url": URL, "notes": []}
        self.env = {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                    "RUNNER_TEMP": str(self.root), "OUTPUT_DIR": str(self.out),
                    "REVIEW_COMPLETION_FILE": str(self.context), "REVIEW_IMAGE": IMAGE,
                    "GITHUB_REPOSITORY": "example/repo", "PR_NUMBER": "7", "HEAD_SHA": SHA,
                    "BASE_SHA": BASE, "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "2",
                    "GITHUB_SERVER_URL": "https://github.com", "GITHUB_API_URL": "https://api.github.com",
                    "GITHUB_TOKEN": "fixture-write",
                    "ANTHROPIC_API_KEY": "fixture-model", "GEMINI_API_KEY": "fixture-gemini",
                    "OPENROUTER_API_KEY": "fixture-openrouter", "REGISTRY_TOKEN": "fixture-registry",
                    "GH_TOKEN": "fixture-gh", "GITHUB_WORKSPACE": str(self.root / "checkout"),
                    "HOME": str(self.root / "secret-home"),
                    "GITHUB_OUTPUT": str(self.root / "outputs")}
        docker = self.root / "docker"
        docker.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, signal, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
if args[:2] == ["rm", "-f"]:
    with (root / "cleanup-calls").open("a") as stream:
        stream.write(json.dumps({"argv": args, "env": dict(os.environ)}) + "\\n")
    pid = root / "container-pid"
    if pid.exists():
        try: os.kill(int(pid.read_text()), signal.SIGKILL)
        except ProcessLookupError: pass
    sys.exit(0)
with (root / "calls").open("a") as stream:
    stream.write(json.dumps({"argv": args, "env": dict(os.environ)}) + "\\n")
mounts = [args[i+1] for i, arg in enumerate(args) if arg == "-v"]
publish = pathlib.Path(next(m.split(":")[0] for m in mounts if m.endswith(":/github/out/publish:ro")))
run = pathlib.Path(next(m.split(":")[0] for m in mounts if m.endswith(":/github/out/run:rw")))
(root / "received.json").write_text((publish / "result.json").read_text())
(root / "staged-names.json").write_text(json.dumps(sorted(p.name for p in publish.iterdir())))
mode = (root / "mode").read_text()
target = run / "publish"
target.mkdir()
if mode != "missing":
    if mode == "symlink":
        (target / "publish.json").symlink_to(root / "receipt.json")
    elif mode == "fifo":
        os.mkfifo(target / "publish.json")
    elif mode == "directory":
        (target / "publish.json").mkdir()
    elif mode == "oversized":
        (target / "publish.json").write_bytes(b" " * (256 * 1024 + 1))
    else:
        (target / "publish.json").write_bytes((root / "receipt.json").read_bytes())
if mode == "hang":
    (root / "container-pid").write_text(str(os.getpid()))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(0.1)
sys.exit(17 if mode == "crash" else 0)
''')
        docker.chmod(0o755)
        # This Mac has no GNU timeout. Model its command boundary and expiration;
        # native GNU signal/process-group semantics remain a Linux acceptance gap.
        timeout = self.root / "timeout"
        timeout.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, signal, subprocess, sys
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
with (root / "timeout-calls").open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
assert args[:2] == ["--signal=TERM", "--kill-after=5"], args
child = subprocess.Popen(args[3:], start_new_session=True)
try:
    code = child.wait(timeout=float(args[2]))
except subprocess.TimeoutExpired:
    os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    code = 124
sys.exit(code)
''')
        timeout.chmod(0o755)
        self.write_handoff()

    def kill_fixture_container(self):
        pid = self.root / "container-pid"
        if pid.exists():
            try:
                os.kill(int(pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def write_handoff(self):
        files = {"result": (self.out / "publish/result.json", self.result),
                 "snapshot": (self.out / "publish/snapshot.json", self.snapshot),
                 "action_result": (self.out / "result.json", self.action)}
        context = {"contract_version": 1, "repository": "example/repo", "pr_number": 7,
                   "head_sha": SHA, "merge_base_sha": BASE, "github_run_id": "99",
                   "github_run_attempt": "2", "engine_run_id": "review-123", "image": IMAGE,
                   "engine_exit": 0, "review_verdict": self.action["verdict"]["status"]}
        for name, (path, value) in files.items():
            path.write_text(json.dumps(value))
            context[name + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.context.write_text(json.dumps(context))

    def add_finding(self):
        self.result["findings"] = [{"id": "dar-fixture", "reviewer": "bug-hunter",
            "unit_id": "u1", "file": "a.py", "start_line": 1, "end_line": 1,
            "severity": "low", "category": "bug", "title": "Fixture", "body": "Detail",
            "confidence": 0.9}]
        self.action["findings"] = [{"id": "dar-fixture", "rule_id": "bug-hunter/bug",
            "skill": "bug-hunter", "severity": "low", "confidence": 0.9, "enforcing": False,
            "location": {"path": "a.py", "start_line": 1, "end_line": 1},
            "message": "Fixture\n\nDetail"}]
        self.write_handoff()

    def run_publish(self, mode="ok"):
        (self.root / "receipt.json").write_text(json.dumps(self.receipt))
        (self.root / "mode").write_text(mode)
        (self.root / "outputs").write_text("")
        return subprocess.run(["bash", str(SCRIPTS / "publish.sh")], env=self.env,
                              capture_output=True, text=True, timeout=8)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_rejected(self):
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

    def test_clean_review_posts_summary_with_no_inline_comments(self):
        (self.out / "publish/untrusted-extra").write_text("do not copy")
        result = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 1)
        outputs = (self.root / "outputs").read_text()
        self.assertIn("delivery=success", outputs)
        self.assertIn("summary-url=" + URL, outputs)
        receipt_paths = [line.split("=", 1)[1] for line in outputs.splitlines()
                         if line.startswith("receipt-file=")]
        self.assertEqual(len(receipt_paths), 1, "missing persisted delivery receipt")
        receipt_file = Path(receipt_paths[0])
        saved = json.loads(receipt_file.read_text())
        self.assertEqual(saved["url"], URL)
        self.assertEqual(saved["posted_comments"], 0)
        self.assertNotIn("notes", saved)
        self.assertNotIn("fixture-write", receipt_file.read_text())
        self.assertFalse((receipt_file.parent / "publish").exists())
        self.assertFalse((receipt_file.parent / "run").exists())
        self.assertEqual(json.loads((self.root / "staged-names.json").read_text()),
                         ["result.json", "snapshot.json"])

    def test_findings_publish_only_with_github_authority_and_two_isolated_mounts(self):
        self.add_finding()
        self.receipt["posted_comments"] = 1
        result = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args, env = self.calls()[0]["argv"], self.calls()[0]["env"]
        self.assertEqual(args[-2:], [IMAGE, "action-publish"])
        forwarded = [args[i + 1] for i, value in enumerate(args) if value == "-e"]
        self.assertEqual(set(forwarded), {"GITHUB_TOKEN", "GITHUB_REPOSITORY", "PR_NUMBER",
                                         "REVIEW_OUTPUT_DIR=/github/out", "KORZA_CONTRACT_VERSION=1"})
        self.assertEqual(args.count("-v"), 2)
        mounts = [args[i+1] for i, arg in enumerate(args) if arg == "-v"]
        self.assertTrue(mounts[0].endswith(":/github/out/publish:ro"))
        self.assertTrue(mounts[1].endswith(":/github/out/run:rw"))
        self.assertIn("--pull=never", args)
        self.assertNotIn(str(self.out) + ":", " ".join(mounts))
        self.assertNotIn("fixture-write", " ".join(args))
        for forbidden in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY",
                          "REGISTRY_TOKEN", "GH_TOKEN", "HOME", "GITHUB_WORKSPACE"):
            self.assertNotIn(forbidden, env)
        self.assertEqual(json.loads((self.root / "received.json").read_text()), self.result)

    def test_validator_never_receives_token_model_or_registry_credentials(self):
        wrapper = self.root / "python3"
        wrapper.write_text("#!" + sys.executable + "\n" + '''import json, os, pathlib, sys
root = pathlib.Path(__file__).parent
with (root / "validator-env").open("a") as stream:
    stream.write(json.dumps(dict(os.environ)) + "\\n")
os.execve(sys.executable, [sys.executable, *sys.argv[1:]], dict(os.environ))
''')
        wrapper.chmod(0o755)
        result = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (self.root / "validator-env").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertNotIn("fixture-", call)
            self.assertNotIn("GITHUB_TOKEN", call)

    def test_stage_cannot_reuse_existing_data(self):
        stage = self.root / "stage"
        stage.mkdir()
        (stage / "previous").write_text("old data")
        command = [sys.executable, str(SCRIPTS / "contract.py"), "stage-handoff",
                   "--output-dir", str(self.out), "--completion-file", str(self.context),
                   "--stage-dir", str(stage), "--repository", "example/repo", "--pr-number", "7",
                   "--head-sha", SHA, "--merge-base-sha", BASE, "--image", IMAGE,
                   "--github-run-id", "99", "--github-run-attempt", "2"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["previous"])
        self.assertEqual(self.calls(), [])

    def test_completed_blocking_review_can_publish(self):
        self.add_finding()
        self.action["verdict"] = {"status": "fail", "reason": "Blocking fixture.",
                                  "blocking_findings": ["dar-fixture"]}
        self.action["findings"][0]["enforcing"] = True
        self.write_handoff()
        self.receipt["posted_comments"] = 1
        self.assertEqual(self.run_publish().returncode, 0)

    def test_default_engine_role_metadata_can_stage_a_completed_handoff(self):
        # Current contract_result(None, Settings()) metadata; the producer-to-host
        # compatibility probe also binds this fixture to the real engine output.
        self.action["skills_run"] = ["code-reviewer", "bug-hunter", "performance-review",
                                     "owasp-reviewer"]
        self.write_handoff()
        published = self.run_publish()
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_security_reviewer_findings_can_stage_and_publish(self):
        for role in ("owasp-reviewer", "stride-modeler"):
            with self.subTest(role=role):
                self.add_finding()
                self.result["findings"][0].update(reviewer=role, category="security")
                self.action["findings"][0].update(skill=role, rule_id=role + "/security")
                # Isolate finding classification from the metadata-only test.
                self.action["skills_run"] = []
                self.receipt["posted_comments"] = 1
                self.write_handoff()
                published = self.run_publish()
                self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
                received = json.loads((self.root / "received.json").read_text())
                self.assertEqual(received["findings"][0]["reviewer"], role)

    def test_unknown_roles_cannot_stage_metadata_or_findings(self):
        self.add_finding()
        for role in ("scanner-orchestrator", "security-reviewer", "unknown"):
            for target in (self.action, self.action["findings"][0], self.result["findings"][0]):
                key = "skills_run" if target is self.action else (
                    "skill" if target is self.action["findings"][0] else "reviewer")
                with self.subTest(role=role, field=key):
                    previous = target[key]
                    target[key] = [role] if key == "skills_run" else role
                    self.write_handoff()
                    self.assert_rejected()
                    target[key] = previous

    def test_stale_and_unplaceable_summaries_are_successful_delivery(self):
        self.add_finding()
        for updates in ({"head_moved": True, "summary_only": True}, {"couldnt_place": 1},
                        {"head_moved": True, "posted_comments": 1}):
            with self.subTest(updates=updates):
                original = copy.deepcopy(self.receipt)
                self.receipt.update(updates)
                result = self.run_publish()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.receipt = original

    def test_duplicate_skips_accounted_without_inflating_new_posts(self):
        self.add_finding()
        for collection in (self.result["findings"], self.action["findings"]):
            second = copy.deepcopy(collection[0])
            second["id"] = "dar-second"
            collection.append(second)
        self.write_handoff()
        gh = self.root / "gh"
        gh.write_text("#!" + sys.executable + "\nimport pathlib,sys\n"
                      "pathlib.Path(__file__).with_name('final.json').write_text(sys.stdin.read())\n")
        gh.chmod(0o755)
        for skipped in (1, 2):
            with self.subTest(skipped=skipped):
                self.receipt.update(posted_comments=2 - skipped, already_posted=skipped)
                published = self.run_publish()
                self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
                outputs = dict(line.split("=", 1) for line in
                               (self.root / "outputs").read_text().splitlines())
                self.assertEqual(outputs["delivery"], "success")
                saved = json.loads(Path(outputs["receipt-file"]).read_text())
                self.assertEqual(saved["posted_comments"], 2 - skipped)
                self.assertEqual(saved["already_posted"], skipped)
                finalized = subprocess.run(["bash", str(SCRIPTS / "finalize.sh")],
                    env=dict(self.env, REVIEW_OUTCOME="success", VERDICT="pass",
                             REVIEW_COMPLETED="true", PUBLISH_OUTCOME="success",
                             DELIVERY=outputs["delivery"], FAIL_ON_ERROR="false",
                             CHECK_RUN_ID="7", RESULT_FILE="", SUMMARY_FILE=""),
                    text=True, capture_output=True, timeout=5)
                self.assertEqual(finalized.returncode, 0, finalized.stderr)
                self.assertEqual(json.loads((self.root / "final.json").read_text())["conclusion"],
                                 "success")

    def test_already_posted_must_be_strict_and_account_for_expected_findings(self):
        self.add_finding()
        for bad in (True, False, -1, "1", 1.0, None, [], {}, 0, 2):
            with self.subTest(already_posted=bad):
                self.receipt["already_posted"] = bad
                published = self.run_publish()
                self.assertNotEqual(published.returncode, 0, published.stdout + published.stderr)
                self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
        for updates in ({"posted_comments": 1, "already_posted": 1},
                        {"couldnt_place": 1, "already_posted": 1},
                        {"head_moved": True, "summary_only": True, "already_posted": 1}):
            with self.subTest(updates=updates):
                self.receipt.update(posted_comments=0, couldnt_place=0,
                                    head_moved=False, summary_only=False)
                self.receipt.update(updates)
                self.assertNotEqual(self.run_publish().returncode, 0)

    def test_completion_identity_process_status_and_hashes_are_required(self):
        bad = {"contract_version": True, "repository": "other/repo", "pr_number": 8,
               "head_sha": "d" * 40, "merge_base_sha": "d" * 40, "github_run_id": "98",
               "github_run_attempt": "1", "engine_run_id": "previous", "image": "other",
               "engine_exit": 1, "review_verdict": "error", "result_sha256": "0" * 64,
               "snapshot_sha256": "0" * 64, "action_result_sha256": "0" * 64}
        for key, value in bad.items():
            with self.subTest(key=key):
                self.write_handoff()
                context = json.loads(self.context.read_text())
                context[key] = value
                self.context.write_text(json.dumps(context))
                self.assert_rejected()
        self.write_handoff()
        context = json.loads(self.context.read_text())
        del context["engine_exit"]
        self.context.write_text(json.dumps(context))
        self.assert_rejected()

    def test_changed_bytes_after_completion_cannot_be_published(self):
        (self.out / "publish/result.json").write_text(json.dumps({**self.result, "findings": [{}]}))
        self.assert_rejected()

    def test_handoff_identity_versions_and_findings_checked_with_matching_hashes(self):
        cases = [(self.result, "schema_version", "1"), (self.result, "run_id", ""),
                 (self.result, "head_sha", "d" * 40), (self.snapshot, "schema_version", True),
                 (self.snapshot, "provider", "local"), (self.snapshot, "repo", "other/repo"),
                 (self.snapshot["pr"], "number", 8), (self.snapshot["refs"], "head_sha", "d" * 40),
                 (self.snapshot["refs"], "merge_base_sha", "d" * 40),
                 (self.action, "contract_version", None), (self.action, "contract_version", "1"),
                 (self.action, "contract_version", True), (self.action, "contract_version", 2),
                 (self.action["verdict"], "status", "error"),
                 (self.action, "findings", [{"id": "unrelated"}]),
                 (self.result, "findings", [{"id": "bad", "start_line": True}])]
        for obj, key, value in cases:
            with self.subTest(key=key, value=value):
                original = obj[key]
                obj[key] = value
                self.write_handoff()
                self.assert_rejected()
                obj[key] = original

    def test_missing_malformed_duplicate_nonregular_and_oversized_files_never_publish(self):
        paths = [self.context, self.out / "result.json", self.out / "publish/result.json",
                 self.out / "publish/snapshot.json"]
        for path in paths:
            for mode in ("missing", "malformed", "duplicate", "symlink", "fifo", "directory", "oversized"):
                with self.subTest(file=path.name, mode=mode):
                    self.write_handoff()
                    data = path.read_bytes()
                    path.unlink()
                    if mode == "malformed":
                        path.write_text("{")
                    elif mode == "duplicate":
                        path.write_text('{"x": 1, "x": 2}')
                    elif mode == "symlink":
                        target = self.root / "target.json"
                        target.write_bytes(data)
                        path.symlink_to(target)
                    elif mode == "fifo":
                        os.mkfifo(path)
                    elif mode == "directory":
                        path.mkdir()
                    elif mode == "oversized":
                        path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
                    self.assert_rejected()
                    if path.is_dir():
                        path.rmdir()
                    else:
                        path.unlink(missing_ok=True)

    def test_handoff_parent_symlink_is_rejected(self):
        (self.out / "publish").rename(self.out / "moved")
        (self.out / "publish").symlink_to(self.out / "moved", target_is_directory=True)
        self.assert_rejected()

    def test_completion_must_be_outside_engine_output(self):
        target = self.out / "completion.json"
        target.write_bytes(self.context.read_bytes())
        self.env["REVIEW_COMPLETION_FILE"] = str(target)
        self.assert_rejected()

    def test_missing_token_or_unpinned_image_does_not_start_publisher(self):
        del self.env["GITHUB_TOKEN"]
        self.assert_rejected()
        self.env["GITHUB_TOKEN"] = "fixture-write"
        self.env["REVIEW_IMAGE"] = "ghcr.io/example/engine:latest"
        self.assert_rejected()

    def test_failed_noop_and_nonregular_receipts_are_never_retried(self):
        for mode in ("missing", "crash", "symlink", "fifo", "directory", "oversized"):
            with self.subTest(mode=mode):
                (self.root / "calls").unlink(missing_ok=True)
                result = self.run_publish(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.calls()), 1)
                self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
                self.assertIn("delivery=error", (self.root / "outputs").read_text())

    def test_invalid_counts_flags_and_destinations_cannot_report_success(self):
        cases = [("publisher", "html"), ("posted_comments", True), ("posted_comments", -1),
                 ("posted_comments", 1), ("couldnt_place", 1), ("summary_only", True),
                 ("head_moved", "false"), ("notes", [1]), ("url", None),
                 ("url", "https://evil.example/example/repo/pull/7#issuecomment-1"),
                 ("url", "https://github.com/example/repo/pull/8#issuecomment-1"),
                 ("url", "https://github.com/example/other/pull/7#issuecomment-1"),
                 ("url", "https://github.com/example/repo/pull/7"),
                 ("url", URL + "\nstatus=success"), ("url", URL + "?redirect=elsewhere")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                (self.root / "calls").unlink(missing_ok=True)
                original = self.receipt[key]
                self.receipt[key] = value
                result = self.run_publish()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.calls()), 1)
                self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
                self.receipt[key] = original

    def test_deadline_rejects_receipt_and_removes_named_container_without_retry(self):
        self.env["PUBLISH_TIMEOUT"] = "1"
        result = self.run_publish("hang")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
        self.assertEqual(len(self.calls()), 1)
        self.assert_container_cleanup()
        args = [json.loads(line) for line in (self.root / "timeout-calls").read_text().splitlines()]
        self.assertEqual(args[0][:3], ["--signal=TERM", "--kill-after=5", "1"])
        self.assertEqual(args[-1][:3], ["--signal=TERM", "--kill-after=5", "10"])

    def assert_container_cleanup(self):
        outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
        name = outputs["publisher-container"]
        args = self.calls()[0]["argv"]
        self.assertEqual(args[args.index("--name") + 1], name)
        cleanups = [json.loads(line) for line in (self.root / "cleanup-calls").read_text().splitlines()]
        self.assertEqual(cleanups[-1]["argv"], ["rm", "-f", name])
        self.assertNotIn("GITHUB_TOKEN", cleanups[-1]["env"])

    def test_term_and_int_cancel_promptly_and_cleanup_container(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with self.subTest(signal=sig):
                (self.root / "receipt.json").write_text(json.dumps(self.receipt))
                (self.root / "mode").write_text("hang")
                (self.root / "outputs").write_text("")
                (self.root / "calls").unlink(missing_ok=True)
                (self.root / "container-pid").unlink(missing_ok=True)
                process = subprocess.Popen(["bash", str(SCRIPTS / "publish.sh")], env=self.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           start_new_session=True)
                try:
                    deadline = time.monotonic() + 4
                    while not (self.root / "container-pid").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue((self.root / "container-pid").exists())
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=4)
                    self.assertEqual(process.returncode, code, stdout + stderr)
                    self.assert_container_cleanup()
                    self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                    if (self.root / "container-pid").exists():
                        try: os.kill(int((self.root / "container-pid").read_text()), signal.SIGKILL)
                        except ProcessLookupError: pass
                    process.communicate()

    def test_publisher_names_are_unique_and_success_still_cleans_up(self):
        names = []
        for _ in range(2):
            (self.root / "calls").unlink(missing_ok=True)
            result = self.run_publish()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_container_cleanup()
            outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
            names.append(outputs["publisher-container"])
        self.assertNotEqual(names[0], names[1])

    def test_invalid_publish_deadline_never_invokes_docker(self):
        for value in ("0", "-1", "3601", "1.5", "garbage"):
            with self.subTest(value=value):
                self.env["PUBLISH_TIMEOUT"] = value
                self.assert_rejected()

    def test_action_fallback_removes_container_after_shell_is_killed(self):
        (self.root / "receipt.json").write_text(json.dumps(self.receipt))
        (self.root / "mode").write_text("hang")
        (self.root / "outputs").write_text("")
        process = subprocess.Popen(["bash", str(SCRIPTS / "publish.sh")], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 4
            while not (self.root / "container-pid").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "container-pid").exists())
            process.kill()
            process.wait(timeout=2)
            outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
            action = (SCRIPTS.parent / "action.yml").read_text()
            step = action.split("    - name: Clean up publisher container\n", 1)[1].split("    - name:", 1)[0]
            command = next(line.split("run: ", 1)[1] for line in step.splitlines() if "run: " in line)
            result = subprocess.run(["bash", "-c", command],
                                    env=dict(self.env, PUBLISHER_CONTAINER=outputs["publisher-container"]),
                                    capture_output=True, text=True, timeout=4)
            self.assertEqual(result.returncode, 0, result.stderr)
            process.communicate(timeout=3)

            args = self.calls()[0]["argv"]
            self.assertEqual(args[args.index("--name") + 1], outputs["publisher-container"])
            cleanups = [json.loads(line) for line in (self.root / "cleanup-calls").read_text().splitlines()]
            self.assertEqual(cleanups[-1]["argv"], ["rm", "-f", outputs["publisher-container"]])
            self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
        finally:
            self.kill_fixture_container()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def test_unsupported_github_destinations_are_rejected_before_docker(self):
        cases = [("GITHUB_SERVER_URL", value) for value in
                 ("", "http://github.com", "https://github.com/", "https://github.com/path",
                  "https://example.ghe.com", "https://secret-fixture.invalid")]
        cases += [("GITHUB_API_URL", value) for value in
                  ("", "https://api.github.com/", "https://github.com/api/v3",
                   "https://example.ghe.com/api/v3", "https://secret-fixture.invalid")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                (self.root / "calls").unlink(missing_ok=True)
                self.env.update(GITHUB_SERVER_URL="https://github.com", GITHUB_API_URL="https://api.github.com")
                self.env[key] = value
                result = self.run_publish()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertNotIn("delivery=success", (self.root / "outputs").read_text())
                self.assertNotIn("secret-fixture", result.stdout + result.stderr)

    def test_contract_reasons_are_static_and_dynamic_failures_remain_type_only(self):
        marker = "SENSITIVE_FIXTURE_DO_NOT_ECHO"
        context = json.loads(self.context.read_text())
        context["repository"] = marker
        self.context.write_text(json.dumps(context))
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Publisher contract rejected: completion identity mismatch", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

        self.write_handoff()
        self.context.write_text('{"' + marker + '": invalid}')
        result = self.run_publish()
        self.assertIn("Publisher contract rejected: JSONDecodeError", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

        self.env["REVIEW_COMPLETION_FILE"] = str(self.root / marker)
        result = self.run_publish()
        self.assertIn("Publisher contract rejected: FileNotFoundError", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

        self.env["REVIEW_COMPLETION_FILE"] = str(self.context)
        self.context.write_text('{"' + marker + '": NaN}')
        result = self.run_publish()
        self.assertIn("Publisher contract rejected: ValueError", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

        self.action["skills_run"] = [{marker: "unhashable"}]
        self.write_handoff()
        result = self.run_publish()
        self.assertIn("Publisher contract rejected: TypeError", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertEqual(self.calls(), [])

        self.action["skills_run"] = []
        self.write_handoff()
        self.receipt.update(posted_comments=1, notes=[marker])
        result = self.run_publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Publisher contract rejected: receipt does not account for findings", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertNotIn("fixture-write", result.stdout + result.stderr)
        self.assertEqual(len(self.calls()), 1)


if __name__ == "__main__":
    unittest.main()
