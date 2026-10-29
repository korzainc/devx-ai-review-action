"""Host debug exports must not expose paths outside the engine's run directory."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/stage_debug.py"


class DebugTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.output = self.base / "engine"
        self.run = self.output / "run"
        self.run.mkdir(parents=True)
        self.runner = self.base / "runner"
        self.runner.mkdir()
        self.outputs = self.base / "outputs"
        self.outputs.write_text("")
        original_umask = os.umask(0o077)
        self.addCleanup(os.umask, original_umask)
        self.env = {"PATH": os.environ["PATH"], "OUTPUT_DIR": str(self.output),
                    "RUNNER_TEMP": str(self.runner), "GITHUB_OUTPUT": str(self.outputs)}

    def invoke(self):
        proc = subprocess.run([sys.executable, str(SCRIPT)], env=self.env,
                              text=True, capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        values = dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())
        return proc, Path(values["debug-dir"]) if values.get("debug-dir") else None

    def helper(self):
        self.assertTrue(SCRIPT.is_file(), "Missing host-private debug staging helper")
        spec = importlib.util.spec_from_file_location("debug_staging", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_export_contains_only_private_regular_copies_of_run_files(self):
        (self.run / "nested").mkdir()
        (self.run / "nested/log.json").write_text('{"ok":true}')
        (self.run / ".hidden").write_text("hidden-debug")
        (self.output / "publish").mkdir()
        (self.output / "publish/token.json").write_text("publisher-fixture")
        proc, stage = self.invoke()
        self.assertIsNotNone(stage)
        self.assertFalse(stage.is_relative_to(self.output))
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.relative_to(stage).as_posix() for p in stage.rglob("*") if p.is_file()),
                         [".hidden", "nested/log.json"])
        copied = stage / "nested/log.json"
        self.assertEqual(json.loads(copied.read_text()), {"ok": True})
        self.assertNotEqual(copied.stat().st_ino, (self.run / "nested/log.json").stat().st_ino)
        self.assertEqual(copied.stat().st_nlink, 1)
        self.assertEqual(proc.stdout, "")

    def test_leaf_directory_links_hardlinks_and_fifo_cannot_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        victim = outside / "benign-fixture.txt"
        victim.write_text("HOST-FIXTURE-DO-NOT-EXPORT")
        (self.run / "safe.log").write_text("allowed")
        (self.run / "leaf").symlink_to(victim)
        (self.run / "directory").symlink_to(outside, target_is_directory=True)
        os.link(victim, self.run / "hardlink")
        os.mkfifo(self.run / "fifo")
        proc, stage = self.invoke()
        self.assertIsNotNone(stage)
        self.assertEqual(sorted(p.name for p in stage.iterdir()), ["safe.log"])
        self.assertEqual(victim.read_text(), "HOST-FIXTURE-DO-NOT-EXPORT")
        self.assertNotIn(str(outside), proc.stdout + proc.stderr)
        self.assertNotIn("benign-fixture", proc.stdout + proc.stderr)
        self.assertIn("::warning::", proc.stdout)

    def test_symlinked_run_or_ancestor_is_never_traversed(self):
        (self.run / "safe.log").write_text("allowed")
        for kind in ("run", "ancestor"):
            with self.subTest(kind=kind):
                self.outputs.write_text("")
                if kind == "run":
                    moved = self.output / "actual-run"
                    self.run.rename(moved)
                    self.run.symlink_to(moved, target_is_directory=True)
                else:
                    self.run.unlink()
                    moved.rename(self.run)
                    alias = self.base / "alias"
                    alias.symlink_to(self.output, target_is_directory=True)
                    self.env["OUTPUT_DIR"] = str(alias)
                _proc, stage = self.invoke()
                self.assertIsNone(stage)
                self.assertEqual(list(self.runner.iterdir()), [])

    def test_missing_run_or_empty_run_skips_artifact_gracefully(self):
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists:
                    self.run.rmdir()
                proc, stage = self.invoke()
                self.assertIsNone(stage)
                self.assertEqual(list(self.runner.iterdir()), [])
                self.assertEqual(proc.stdout, "")

    def test_partial_stage_removed_on_copy_error_without_dynamic_warning(self):
        helper = self.helper()
        (self.run / "safe.log").write_text("allowed")
        with mock.patch.object(helper, "copy_file", side_effect=OSError("ATTACKER-PATH")):
            with mock.patch.dict(os.environ, self.env, clear=True):
                with mock.patch("builtins.print") as printed:
                    self.assertEqual(helper.main(), 0)
        self.assertEqual(list(self.runner.iterdir()), [])
        self.assertEqual(self.outputs.read_text(), "")
        self.assertNotIn("ATTACKER-PATH", str(printed.call_args_list))

    def test_source_growth_cannot_increase_copied_size(self):
        helper = self.helper()
        source = self.run / "growing.log"
        source.write_bytes(b"a" * 8)
        fd = os.open(source, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        before = os.fstat(fd)
        source.write_bytes(b"a" * 8 + b"SHOULD-NOT-BE-COPIED")
        target = self.base / "copy"
        helper.copy_file(fd, target, before.st_size)
        self.assertEqual(target.read_bytes(), b"a" * 8)

    def test_cleanup_failure_is_sanitized_and_never_exposes_artifact_path(self):
        helper = self.helper()
        (self.run / "safe.log").write_text("allowed")
        for cleanup_error in (OSError, RecursionError):
            with self.subTest(cleanup_error=cleanup_error.__name__):
                with mock.patch.object(helper, "copy_file", side_effect=OSError("COPY-ATTACKER-PATH")):
                    with mock.patch.object(helper.shutil, "rmtree", side_effect=cleanup_error("CLEANUP-ATTACKER-PATH")):
                        with mock.patch.dict(os.environ, self.env, clear=True):
                            with mock.patch("builtins.print") as printed:
                                try:
                                    outcome = helper.main()
                                except Exception:
                                    outcome = "cleanup error escaped"
                self.assertEqual(outcome, 0)
                self.assertEqual(self.outputs.read_text(), "")
                self.assertNotIn("ATTACKER-PATH", str(printed.call_args_list))

    def test_changed_source_discards_partial_stage(self):
        helper = self.helper()
        source = self.run / "changing.log"
        source.write_text("before")
        real_copy = helper.copy_file
        def copy_then_change(fd, target, size):
            real_copy(fd, target, size)
            source.write_text("after-growth")
        with mock.patch.object(helper, "copy_file", side_effect=copy_then_change):
            with mock.patch.dict(os.environ, self.env, clear=True):
                with mock.patch("builtins.print"):
                    self.assertEqual(helper.main(), 0)
        self.assertEqual(list(self.runner.iterdir()), [])
        self.assertEqual(self.outputs.read_text(), "")

    def test_action_stages_before_upload_without_credentials_or_raw_source_path(self):
        source = (ROOT / "action.yml").read_text()
        self.assertIn("    - name: Stage debug files\n", source)
        stage = source.split("    - name: Stage debug files\n", 1)[1].split("    - name:", 1)[0]
        upload = source.split("    - name: Upload debug files\n", 1)[1]
        self.assertIn("id: debug", stage)
        self.assertIn("inputs.upload-debug == 'true'", stage)
        self.assertIn('env -i PATH="$PATH"', stage)
        self.assertIn("stage_debug.py", stage)
        self.assertNotIn("ANTHROPIC", stage)
        self.assertNotIn("GH_TOKEN", stage)
        self.assertNotIn("GITHUB_TOKEN", stage)
        self.assertIn("steps.debug.outputs.debug-dir != ''", upload)
        self.assertIn("path: ${{ steps.debug.outputs.debug-dir }}", upload)
        self.assertNotIn("path: ${{ steps.review.outputs.output-dir }}/run", upload)

    def test_actual_stage_command_clears_credential_environment(self):
        (self.run / "safe.log").write_text("allowed")
        source = (ROOT / "action.yml").read_text()
        self.assertIn("    - name: Stage debug files\n", source)
        stage = source.split("    - name: Stage debug files\n", 1)[1].split("    - name:", 1)[0]
        command = next(line.split("run: ", 1)[1] for line in stage.splitlines() if "run: " in line)
        tools = self.base / "bin"
        tools.mkdir()
        python = tools / "python3"
        python.write_text("#!" + sys.executable + "\n" +
            "import os,sys\n" +
            "assert not any(k in os.environ for k in ('ANTHROPIC_API_KEY','GH_TOKEN','GITHUB_TOKEN','REGISTRY_TOKEN'))\n" +
            "os.execve(sys.executable, [sys.executable, *sys.argv[1:]], dict(os.environ))\n")
        python.chmod(0o755)
        env = dict(self.env, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                   GITHUB_ACTION_PATH=str(ROOT), ANTHROPIC_API_KEY="synthetic-model",
                   GH_TOKEN="synthetic-gh", GITHUB_TOKEN="synthetic-write",
                   REGISTRY_TOKEN="synthetic-registry")
        proc = subprocess.run(["bash", "-e", "-c", command], env=env,
                              text=True, capture_output=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        values = dict(line.split("=", 1) for line in self.outputs.read_text().splitlines())
        self.assertEqual((Path(values["debug-dir"]) / "safe.log").read_text(), "allowed")

    def test_leaf_swapped_to_symlink_before_open_cannot_escape(self):
        helper = self.helper()
        source = self.run / "racing.log"
        source.write_text("original")
        victim = self.base / "benign-victim"
        victim.write_text("HOST-FIXTURE")
        real_open = helper.os.open
        def swap_then_open(path, flags, *args, **kwargs):
            if path == "racing.log":
                source.unlink()
                source.symlink_to(victim)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(helper.os, "open", side_effect=swap_then_open):
            with mock.patch.dict(os.environ, self.env, clear=True):
                with mock.patch("builtins.print"):
                    self.assertEqual(helper.main(), 0)
        self.assertEqual(list(self.runner.iterdir()), [])
        self.assertEqual(self.outputs.read_text(), "")
        self.assertEqual(victim.read_text(), "HOST-FIXTURE")


if __name__ == "__main__":
    unittest.main()
