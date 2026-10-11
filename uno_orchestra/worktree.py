"""Thin wrappers around git worktrees (one isolated checkout per agent).

Every git call disables hooks and the fsmonitor daemon: the target repository is untrusted
and both are ways for a repository to run code on the host. Never shell=True.
"""
import subprocess
from pathlib import Path

SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
IDENT = ["-c", "user.name=uno-orchestra", "-c", "user.email=uno@localhost"]


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *SAFE, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def preflight(repo):
    """Refuse a dirty tree or main/master: agent branches merge into the current branch."""
    if git(repo, "status", "--porcelain").stdout.strip():
        raise RuntimeError("the repository has uncommitted changes: commit or stash them first")
    r = git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = r.stdout.strip()
    if r.returncode != 0 or not branch:
        raise RuntimeError("the repository is in detached HEAD state: check out a branch first")
    if branch in ("main", "master"):
        raise RuntimeError(f"refusing to run on {branch!r}: create and check out a feature "
                           "branch first (agent branches are merged into the current branch)")
    return branch


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


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
    git(path, *IDENT, "commit", "-m", message)
    return True


def changes(repo, base, branch):
    """Files on branch that differ from base: path, status, new mode, size and binary flag."""
    raw = git(repo, "diff", "--raw", "--no-renames", "-z", base, branch).stdout.split("\0")
    numstat = git(repo, "diff", "--numstat", "--no-renames", "-z", base, branch).stdout
    binary = {line.split("\t", 2)[2] for line in numstat.split("\0") if line.startswith("-\t-\t")}
    out = []
    for meta, path in zip(raw[::2], raw[1::2]):
        # ":<old mode> <new mode> <old sha> <new sha> <status>"
        _, new_mode, _, new_sha, status = meta[1:].split(" ")
        size = 0 if status == "D" else int(git(repo, "cat-file", "-s", new_sha).stdout)
        out.append({"path": path, "status": status, "mode": new_mode,
                    "size": size, "binary": path in binary})
    return out


def show(repo, branch, path):
    """Content of path on branch, as bytes."""
    return subprocess.run(["git", *SAFE, "-C", str(repo), "show", f"{branch}:{path}"],
                          capture_output=True, check=True).stdout


AGENT_INSTRUCTION_FILES = {"CLAUDE.md", "AGENTS.md", ".claude"}


def agent_instruction_files(repo):
    """Tracked files that can steer an agent (CLAUDE.md, AGENTS.md, .claude/) at any depth."""
    hits = []
    for path in git(repo, "ls-files", "-z").stdout.split("\0"):
        parts = path.split("/")
        if parts[-1] in AGENT_INSTRUCTION_FILES or ".claude" in parts[:-1]:
            hits.append(path)
    return hits


def merge(repo, branch):
    """Merge branch into the current branch of repo. Aborts cleanly on conflict."""
    r = git(repo, *IDENT, "merge", "--no-ff", "-m", f"Merge {branch}", branch, check=False)
    if r.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        return False, (r.stdout + r.stderr).strip()
    return True, r.stdout.strip()


def remove(repo, path):
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)


def delete_branch(repo, branch):
    git(repo, "branch", "-D", branch, check=False)
