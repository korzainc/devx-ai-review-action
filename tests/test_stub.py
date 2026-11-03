"""Stub engine SARIF output, run against a throwaway git repo; no image or GitHub call."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "stub-engine/review.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class StubSarifTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo, self.out = base / "repo", base / "out"
        self.repo.mkdir()
        self.out.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "stub@example.com")
        git(self.repo, "config", "user.name", "stub")
        (self.repo / "app.py").write_text("a = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "app.py").write_text("a = 1\nb = 2\n")
        (self.repo / "lib.py").write_text("c = 3\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "head")
        self.head_sha = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self):
        self.tmp.cleanup()

    def run_stub(self, skills):
        env = {"PATH": os.environ["PATH"], "HOME": self.tmp.name, "REVIEW_SKILLS": skills,
               "REVIEW_OUTPUT_DIR": str(self.out), "BASE_SHA": self.base_sha,
               "HEAD_SHA": self.head_sha}
        subprocess.run(["bash", str(STUB)], cwd=self.repo, env=env, check=True,
                       capture_output=True, text=True)
        sarif = self.out / "results.sarif"
        return json.loads(sarif.read_text()) if sarif.exists() else None

    def test_no_sarif_without_the_flag(self):
        self.assertIsNone(self.run_stub("blocking"))

    def test_sarif_mirrors_the_findings(self):
        sarif = self.run_stub("blocking,sarif")
        result = json.loads((self.out / "result.json").read_text())
        self.assertEqual(result["verdict"]["status"], "fail")
        self.assertEqual(sarif["version"], "2.1.0")
        run = sarif["runs"][0]
        self.assertEqual({r["id"] for r in run["tool"]["driver"]["rules"]},
                         {"stub/medium-check", "stub/low-note"})
        got = [(r["ruleId"], r["level"],
                r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"],
                r["locations"][0]["physicalLocation"]["region"]["startLine"])
               for r in run["results"]]
        want = [(f["rule_id"], {"medium": "warning", "low": "note"}[f["severity"]],
                 f["location"]["path"], f["location"]["start_line"])
                for f in result["findings"]]
        self.assertEqual(got, want)

    def test_clean_run_writes_an_empty_sarif(self):
        sarif = self.run_stub("clean,sarif")
        self.assertEqual(sarif["runs"][0]["results"], [])
        self.assertEqual(sarif["runs"][0]["tool"]["driver"]["rules"], [])


if __name__ == "__main__":
    unittest.main()
