#!/usr/bin/env python3
"""Install and run a worktree-local pre-push gate."""
from pathlib import Path
import subprocess
import sys
import shlex


ROOT = Path(__file__).resolve().parents[1]
PRIMARY = "agent/initial-project"


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def clean_head(expected):
    if git("rev-parse", "HEAD") != expected:
        raise RuntimeError("Checkout HEAD differs from the commit being pushed.")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("Commit or remove pending changes before pushing a primary branch.")


def pre_push(lines):
    protected = {"refs/heads/main", "refs/heads/master", "refs/heads/" + PRIMARY}
    expected = set()
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise RuntimeError("Invalid pre-push input.")
        _, local_oid, remote_ref, _ = fields
        if remote_ref in protected:
            if set(local_oid) == {"0"}:
                raise RuntimeError("Deleting a primary branch is blocked.")
            expected.add(local_oid)
    if not expected:
        return
    if len(expected) != 1:
        raise RuntimeError("Primary destinations must receive the same checked commit.")
    oid = expected.pop()
    clean_head(oid)
    subprocess.run(["sh", str(ROOT / "tools/verify.sh")], cwd=ROOT, check=True)
    clean_head(oid)


def install():
    existing = git("config", "--get", "core.hooksPath") if subprocess.run(
        ["git", "-C", str(ROOT), "config", "--get", "core.hooksPath"],
        stdout=subprocess.DEVNULL).returncode == 0 else ""
    hooks = Path(git("rev-parse", "--absolute-git-dir")) / "local-check-hooks"
    if existing and Path(existing).resolve() != hooks.resolve():
        raise RuntimeError("Existing core.hooksPath is configured; refusing to overwrite it.")
    legacy = Path(git("rev-parse", "--git-common-dir"))
    if not legacy.is_absolute():
        legacy = ROOT / legacy
    if not existing and (legacy / "hooks/pre-push").exists():
        raise RuntimeError("An existing pre-push hook needs explicit integration.")
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$(git rev-parse --show-toplevel)/tools/local_checks.py" pre-push "$@"\n')
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(ROOT), "config", "extensions.worktreeConfig", "true"], check=True)
    subprocess.run(["git", "-C", str(ROOT), "config", "--worktree", "core.hooksPath", str(hooks)], check=True)
    print("Installed pre-push gate for this worktree:", hooks)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["install"]:
            install()
        elif len(sys.argv) >= 2 and sys.argv[1] == "pre-push":
            pre_push(sys.stdin)
        else:
            raise RuntimeError("Usage: python3 tools/local_checks.py install|pre-push")
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print("Local checks blocked push:", error, file=sys.stderr)
        sys.exit(1)
