import asyncio
import json
import subprocess

import pytest

from orchestra import orchestrator
from orchestra.bus import EventBus
from orchestra.worker import Task


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    return path


def run(repo, tasks, events):
    return asyncio.run(orchestrator.run(repo, tasks, EventBus(events), backend="mock"))


def test_mock_run_merges_every_agent_twice(repo, tmp_path):
    events = tmp_path / "events.jsonl"
    tasks = [Task("alpha", "do a"), Task("beta", "do b")]

    for _ in range(2):  # the second run must also produce real merges
        assert run(repo, tasks, events) == {"alpha": "merged", "beta": "merged"}

    merges = git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merges) == 4
    assert len((repo / "alpha.txt").read_text().splitlines()) == 2
    # Worktrees and the scratch directory are cleaned up.
    assert git(repo, "worktree", "list").count("\n") == 1
    assert not list(tmp_path.glob(".orchestra-*"))

    kinds = [json.loads(line)["kind"] for line in events.read_text().splitlines()]
    assert kinds.count("merge") == 4 and kinds[-1] == "run_end"


@pytest.mark.parametrize("name", ["../evil", "a/b", "Upper", "-lead", "", "x" * 33])
def test_invalid_task_names_are_rejected(name):
    with pytest.raises(ValueError, match="invalid task name"):
        Task(name, "prompt")


def test_duplicate_task_names_are_rejected(repo, tmp_path):
    with pytest.raises(ValueError, match="unique"):
        run(repo, [Task("dup", "a"), Task("dup", "b")], tmp_path / "events.jsonl")
    assert not list(tmp_path.glob(".orchestra-*"))
