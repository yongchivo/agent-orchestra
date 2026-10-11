import ast
import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from orchestra import orchestrator
from orchestra.bus import EventBus
from orchestra.worker import DEFAULT_TOOLS, Task, Worker


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


# --- security claims: each one has a test below -------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_each_agent_commits_only_in_its_own_worktree(repo, tmp_path):
    """Isolation: an agent's commit touches only its own files and is authored as that agent."""
    run(repo, [Task("alpha", "a"), Task("beta", "b")], tmp_path / "ev.jsonl")
    for name in ("alpha", "beta"):
        branch = git(repo, "for-each-ref", "--format=%(refname:short)",
                     f"refs/heads/agent/*/{name}").strip()
        assert git(repo, "show", "--format=", "--name-only", branch).split() == [f"{name}.txt"]
        assert git(repo, "show", "--format=%an", "-s", branch).strip() == "agent-orchestra"


def test_merge_conflict_is_attributed_to_one_agent_and_aborted(repo, tmp_path, monkeypatch):
    """Serialised merges: the second agent to touch a file conflicts; main is never left half-merged."""
    async def clash(self):
        (self.workdir / "shared.txt").write_text(f"{self.name}\n")
    monkeypatch.setattr(Worker, "_run_mock", clash)

    events = tmp_path / "ev.jsonl"
    assert run(repo, [Task("first", "a"), Task("second", "b")], events) == \
        {"first": "merged", "second": "conflict"}
    assert (repo / "shared.txt").read_text() == "first\n"
    assert git(repo, "status", "--porcelain") == ""
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    conflicts = [json.loads(l) for l in events.read_text().splitlines()]
    assert [e["target"] for e in conflicts if e["kind"] == "conflict"] == ["second"]


def test_claude_command_is_least_privilege(tmp_path):
    cmd = Worker(Task("x", "do it"), tmp_path, "b", EventBus(tmp_path / "e.jsonl")).command()
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert tools == DEFAULT_TOOLS
    assert "Bash" not in tools and "Bash(git *)" in tools  # git only, no unrestricted shell


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's JS")
def test_dashboard_escapes_xss_payload():
    """Untrusted output: the page's own esc() neutralises an injected payload."""
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    esc_src = re.search(r"const esc = .*;", html).group(0)
    payload = "<img src=x onerror=alert(1)>\"'&"
    out = subprocess.run(["node", "-e", f"{esc_src} process.stdout.write(esc({json.dumps(payload)}))"],
                         capture_output=True, text=True, check=True).stdout
    assert out == "&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;"
    # Every event field interpolated into innerHTML goes through esc().
    for field in ("name", "src", "tgt", "e.kind", "e.message", "runInfo.backend"):
        assert f"${{esc({field}" in html


def test_dashboard_server_binds_locally_and_streams_events(tmp_path):
    """Local by default: the server announces 127.0.0.1 and forwards only valid JSON lines."""
    events = tmp_path / "ev.jsonl"
    events.write_text('{"kind": "say", "source": "a", "message": "<b>x</b>"}\nnot json\n')
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "dashboard" / "server.py"),
                             "--events", str(events), "--port", str(port)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().startswith(f"dashboard: http://127.0.0.1:{port}")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/events", timeout=5) as r:
            assert r.readline() == b'data: {"kind": "say", "source": "a", "message": "<b>x</b>"}\n'
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert b"const esc" in r.read()
    finally:
        proc.kill()
        proc.wait()


def test_runtime_uses_only_the_standard_library():
    for path in [ROOT / "main.py", ROOT / "dashboard" / "server.py", *ROOT.glob("orchestra/*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                assert top == "orchestra" or top in sys.stdlib_module_names, f"{path.name} imports {n}"
