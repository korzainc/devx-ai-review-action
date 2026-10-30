"""Completion attestation rejection and cleanup with real handoff validation."""
import contextlib
import io
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest import mock

import test_publish

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import complete
finally:
    sys.path.pop(0)


class CompletionTests(unittest.TestCase):
    def setUp(self):
        fixture = test_publish.PublisherTests("test_clean_review_posts_summary_with_no_inline_comments")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.control = fixture.root / "control"
        self.control.mkdir()
        self.argv = ["complete.py", "--output-dir", str(fixture.out),
                     "--control-dir", str(self.control), "--repository", "example/repo",
                     "--pr-number", "7", "--head-sha", test_publish.SHA,
                     "--merge-base-sha", test_publish.BASE, "--image", test_publish.IMAGE,
                     "--github-run-id", "99", "--github-run-attempt", "2"]

    def complete(self):
        with mock.patch.object(sys, "argv", self.argv), contextlib.redirect_stdout(io.StringIO()):
            complete.main()

    def assert_no_attestation(self):
        for name in ("completion.json", "pending.json", "preflight"):
            self.assertFalse((self.control / name).exists(), name)

    def test_existing_markers_are_rejected_without_replacing_them(self):
        for name in ("completion.json", "pending.json"):
            with self.subTest(marker=name):
                marker = self.control / name
                marker.write_text("preserve existing marker")
                run = subprocess.run([sys.executable, str(SCRIPTS / "complete.py"), *self.argv[1:]],
                                     text=True, capture_output=True, timeout=8)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertEqual(run.stdout, "")
                self.assertIn("Review completion rejected: ContractError", run.stderr)
                self.assertEqual(marker.read_text(), "preserve existing marker")
                self.assertEqual(list(self.control.iterdir()), [marker])
                marker.unlink()

    def test_preflight_copy_failure_removes_pending_and_partial_stage(self):
        original = Path.open
        def fail_stage_copy(path, *args, **kwargs):
            if path == self.control / "preflight/publish/snapshot.json":
                raise OSError("fixture staging write failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "open", fail_stage_copy):
            with self.assertRaisesRegex(OSError, "fixture staging write failure"):
                self.complete()
        self.assert_no_attestation()

    def test_host_copy_write_failure_cannot_attest_completion(self):
        original = os.open
        def fail_host_copy(path, *args, **kwargs):
            if path == self.control / "result.json":
                raise OSError("fixture host copy write failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(os, "open", fail_host_copy):
            with self.assertRaisesRegex(OSError, "fixture host copy write failure"):
                self.complete()
        self.assert_no_attestation()

    def test_changed_host_copy_cannot_attest_completion(self):
        original = complete.contract.read_json
        def corrupt_host_copy(path, *args, **kwargs):
            if path == self.control / "result.json":
                path.write_text('{"contract_version": 1}')
            return original(path, *args, **kwargs)

        with mock.patch.object(complete.contract, "read_json", corrupt_host_copy):
            with self.assertRaisesRegex(ValueError, "host result copy changed"):
                self.complete()
        self.assert_no_attestation()
