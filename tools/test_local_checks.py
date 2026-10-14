"""Exercise the installed hook with disposable repositories; no network."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import sys

SCRIPT = Path(__file__).with_name("local_checks.py")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo with spaces"
        self.run_git("init", "-q", str(self.repo), cwd=Path(self.temp.name))
        (self.repo / "tools").mkdir()
        shutil.copyfile(SCRIPT, self.repo / "tools/local_checks.py")
        (self.repo / "tools/verify.sh").write_text("#!/bin/sh\nexit 0\n")
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "fixture")
        self.oid = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.run_git("worktree", "add", "-qb", "other", str(Path(self.temp.name) / "other"))
        subprocess.run([sys.executable, "tools/local_checks.py", "install"], cwd=self.repo,
                       check=True, capture_output=True)
        self.hook = Path(self.run_git("config", "--get", "core.hooksPath").stdout.strip()) / "pre-push"

    def run_git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo, text=True,
                              capture_output=True, check=True)

    def push(self, remote="main", oid=None):
        line = f"refs/heads/feature {oid or self.oid} refs/heads/{remote} {'0' * 40}\n"
        return subprocess.run([str(self.hook), "origin", "unused"], cwd=self.repo / "tools",
                              input=line, text=True, capture_output=True)

    def change_verifier(self, content):
        (self.repo / "tools/verify.sh").write_text(content)
        self.run_git("add", ".")
        self.run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                     "commit", "-qm", "change fixture")
        self.oid = self.run_git("rev-parse", "HEAD").stdout.strip()

    def test_destination_and_clean_commit(self):
        self.assertEqual(self.push().returncode, 0)
        self.assertEqual(self.push("master").returncode, 0)
        self.assertEqual(self.push("agent/initial-project").returncode, 0)
        self.assertNotEqual(self.push(oid="1" * 40).returncode, 0)
        self.assertNotEqual(self.push(oid="0" * 40).returncode, 0)
        (self.repo / "pending").write_text("pending")
        self.assertNotEqual(self.push().returncode, 0)
        self.assertNotEqual(self.push("agent/initial-project").returncode, 0)
        self.assertEqual(self.push("feature").returncode, 0)

    def test_failed_checks_block_push(self):
        self.change_verifier("#!/bin/sh\nexit 7\n")
        self.assertNotEqual(self.push().returncode, 0)

    def test_changes_during_checks_block_push(self):
        self.change_verifier("#!/bin/sh\necho pending > pending\n")
        self.assertNotEqual(self.push().returncode, 0)

    def test_installation_is_worktree_local(self):
        other = Path(self.temp.name) / "other"
        result = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                                cwd=other, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.run_git("config", "--worktree", "--get", "core.hooksPath").stdout.strip(),
                         str(self.hook.parent))

    def test_existing_hook_configuration_preserved(self):
        self.run_git("config", "--worktree", "core.hooksPath", "/existing-hooks")
        result = subprocess.run([sys.executable, "tools/local_checks.py", "install"],
                                cwd=self.repo, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.run_git("config", "--get", "core.hooksPath").stdout.strip(),
                         "/existing-hooks")


if __name__ == "__main__":
    unittest.main()
