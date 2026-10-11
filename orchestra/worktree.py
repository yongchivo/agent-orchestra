"""Thin wrappers around git worktrees (one isolated checkout per agent)."""
import subprocess
from pathlib import Path


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def create(repo, base_dir, run_id, name, base_ref="HEAD"):
    """Create branch agent/<run_id>/<name> checked out in base_dir/<name>."""
    branch = f"agent/{run_id}/{name}"
    path = Path(base_dir) / name
    git(repo, "worktree", "add", "-b", branch, str(path), base_ref)
    return path, branch


def commit_all(path, message):
    """Commit whatever the agent left uncommitted. Returns True if a commit was made."""
    if not git(path, "status", "--porcelain").stdout.strip():
        return False
    git(path, "add", "-A")
    git(
        path,
        "-c", "user.name=agent-orchestra",
        "-c", "user.email=orchestra@localhost",
        "commit", "-m", message,
    )
    return True


def merge(repo, branch):
    """Merge branch into the current branch of repo. Aborts cleanly on conflict."""
    r = git(
        repo,
        "-c", "user.name=agent-orchestra",
        "-c", "user.email=orchestra@localhost",
        "merge", "--no-ff", "-m", f"Merge {branch}", branch,
        check=False,
    )
    if r.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        return False, (r.stdout + r.stderr).strip()
    return True, r.stdout.strip()


def remove(repo, path):
    git(repo, "worktree", "remove", "--force", str(path), check=False)
