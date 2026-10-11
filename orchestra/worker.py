"""A Worker runs one agent inside its own worktree and reports to the bus.

Backends:
  claude  - headless Claude Code CLI, locked down (see command(), agent_settings(), agent_env())
  mock    - fake agent for tests and for developing the dashboard without API usage

The target repository and everything an agent produces are untrusted: they may carry
prompt injection. Agents never commit; the orchestrator commits and gates the merge.
"""
import asyncio
import json
import os
import re
import signal
from dataclasses import dataclass

from . import worktree
from .bus import EventBus

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PRESETS = {
    "readonly": ["Read", "Glob", "Grep"],
    "edit": ["Read", "Glob", "Grep", "Edit", "Write"],
}
# Removed from the agent whatever the preset says. "mcp__*" removes every MCP tool.
DENIED_TOOLS = ["Bash", "WebFetch", "WebSearch", "mcp__*"]
MAX_TURNS = 50
KILL_GRACE = 5  # seconds between SIGTERM and SIGKILL
OUTPUTS = ("text", "findings")
# Structured output of a reviewer role (pipeline mode). Parsed strictly by pipeline.parse_findings.
FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "properties": {"severity": {"type": "string", "enum": ["low", "medium", "high"]},
                       "file": {"type": "string"}, "line": {"type": "integer"},
                       "description": {"type": "string"}, "recommendation": {"type": "string"}},
        "required": ["severity", "file", "description", "recommendation"]}}},
    "required": ["findings"],
}

# Credential stores the agent must never read, even though --restricted already confines
# the file tools to the worktree. ".env*" is denied everywhere, the worktree included.
SENSITIVE_DIRS = ["~/.ssh", "~/.aws", "~/.config/gh", "~/.gnupg", "~/Library/Keychains"]

# The subprocess environment is built from this allowlist: no GITHUB_TOKEN, AWS_*, ...
# and none of the CLAUDE* variables a parent Claude Code session exports.
ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "TERM", "USER", "TMPDIR",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
)


def agent_env(environ=None):
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k in ENV_ALLOWLIST}


def agent_settings():
    """Settings passed with --settings (the only non-managed settings --restricted loads)."""
    return {
        "disableAllHooks": True,
        "permissions": {
            # .orchestra/ holds the roles and project context: no agent edits it (the merge
            # gate blocks it deterministically too; this is the first layer).
            "deny": [f"Read({d}/**)" for d in SENSITIVE_DIRS] + ["Read(//**/.env*)"]
                    + ["Edit(//**/.orchestra/**)", "Write(//**/.orchestra/**)"] + DENIED_TOOLS,
        },
        # Defence in depth: Bash is removed above. If a future preset brought it back, the
        # OS sandbox (seatbelt on macOS, bubblewrap on Linux) would still confine it.
        # The sandbox covers shell commands only, not the file tools.
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "network": {"allowedDomains": []},
            "filesystem": {"denyRead": SENSITIVE_DIRS},
        },
    }


def write_agent_config(config_dir):
    """Write settings.json and an empty MCP config (0600) for the agents of one run."""
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    for name, data in (("settings.json", agent_settings()), ("mcp.json", {"mcpServers": {}})):
        fd = os.open(config_dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


@dataclass
class Task:
    name: str
    prompt: str
    paths: list          # globs the agent may change; anything else blocks the merge
    preset: str = "edit"
    max_turns: int = 20
    output: str = "text"  # "findings": the agent must return FINDINGS_SCHEMA (pipeline reviewers)

    def __post_init__(self):
        # The name becomes a directory and a branch: validate it strictly.
        if not NAME_RE.match(self.name):
            raise ValueError(f"invalid task name {self.name!r}: use a-z, 0-9 and '-'")
        if self.preset not in PRESETS:
            raise ValueError(f"unknown preset {self.preset!r}: use one of {sorted(PRESETS)}")
        if (not isinstance(self.paths, list) or not self.paths
                or not all(isinstance(p, str) and p.strip() for p in self.paths)):
            raise ValueError(f"task {self.name!r}: 'paths' must be a non-empty list of globs")
        if not isinstance(self.max_turns, int) or isinstance(self.max_turns, bool):
            raise ValueError(f"task {self.name!r}: max_turns must be an integer")
        if not 1 <= self.max_turns <= MAX_TURNS:
            raise ValueError(f"task {self.name!r}: max_turns must be 1..{MAX_TURNS}")
        if self.output not in OUTPUTS:
            raise ValueError(f"task {self.name!r}: output must be one of {list(OUTPUTS)}")

    @property
    def tools(self):
        return PRESETS[self.preset]


class Worker:
    def __init__(self, task, workdir, branch, bus: EventBus, backend="claude",
                 timeout=900, config_dir=None):
        self.task = task
        self.workdir = workdir
        self.branch = branch
        self.bus = bus
        self.backend = backend
        self.timeout = timeout
        self.config_dir = config_dir
        self.status = "pending"
        self.progress = 0
        self.proc = None
        self.output = None   # final text, or the structured output of a "findings" task
        self.round = 1       # correction round in pipeline mode (the mock reviewer uses it)

    @property
    def name(self):
        return self.task.name

    def _progress(self, pct):
        self.progress = max(self.progress, min(100, int(pct)))
        self.bus.emit(self.name, "progress", f"{self.progress}%", progress=self.progress)

    async def run(self):
        self.status = "running"
        self.bus.emit(self.name, "start", self.task.prompt[:120], target="orchestrator",
                      branch=self.branch)
        try:
            backend = self._run_mock() if self.backend == "mock" else self._run_claude()
            await asyncio.wait_for(backend, self.timeout)
            committed = worktree.commit_all(self.workdir, f"agent {self.name}: work in progress")
            self.status = "done"
            self._progress(100)
            self.bus.emit(self.name, "done", "finished", target="orchestrator",
                          extra_commit=committed)
        except asyncio.TimeoutError:
            self.status = "failed"
            self.bus.emit(self.name, "failed", f"timeout after {self.timeout}s", target="orchestrator")
        except Exception as exc:  # noqa: BLE001 - report any failure to the bus
            self.status = "failed"
            self.bus.emit(self.name, "failed", str(exc), target="orchestrator")
        return self.status

    async def _run_mock(self):
        import random
        if self.task.output == "findings":
            # A mock reviewer: reads, then flags one medium finding on the first round and is
            # satisfied on the next, so the mock pipeline shows one correction loop.
            for i, tool in enumerate(("Read", "Grep"), 1):
                await asyncio.sleep(random.uniform(0.1, 0.3))
                self.bus.emit(self.name, "tool", tool, tool=tool)
                self._progress(50 * i)
            target = next((p.name for p in sorted(self.workdir.glob("*.txt"))), "-")
            self.output = {"findings": [] if self.round > 1 else [
                {"severity": "medium", "file": target, "line": 1,
                 "description": "mock finding: the new code does not validate its input",
                 "recommendation": "validate the input before using it"}]}
            return
        steps = random.randint(4, 7)
        for i in range(1, steps + 1):
            await asyncio.sleep(random.uniform(0.1, 0.4))
            self.bus.emit(self.name, "tool", f"Edit {self.name}/step{i}.txt", tool="Edit")
            self._progress(100 * i / steps)
        # Append (not overwrite) so repeated runs always produce a real commit to merge.
        with (self.workdir / f"{self.name}.txt").open("a", encoding="utf-8") as f:
            f.write(f"work of agent {self.name} on {self.branch}\n")
        self.output = f"mock summary: appended one line to {self.name}.txt"

    def command(self, env=None):
        """Headless CLI invocation. Every flag below exists in Claude Code 2.1.296."""
        env = agent_env() if env is None else env
        tools = ",".join(self.task.tools)
        cmd = [
            "claude", "-p", self.task.prompt,
            "--output-format", "stream-json", "--verbose",
            "--max-turns", str(self.task.max_turns),
            # No command/code tools or WebFetch, file tools confined to the worktree, and
            # user/project/local settings ignored (only managed settings and --settings load).
            "--restricted",
            "--tools", tools, "--allowedTools", tools,
            "--disallowedTools", ",".join(DENIED_TOOLS),
            # dontAsk denies anything that would prompt; none = nobody can answer a prompt.
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--strict-mcp-config", "--mcp-config", str(self.config_dir / "mcp.json"),
            "--settings", str(self.config_dir / "settings.json"),
            "--disable-slash-commands", "--no-session-persistence",
        ]
        if self.task.output == "findings":
            cmd += ["--json-schema", json.dumps(FINDINGS_SCHEMA)]
        if "ANTHROPIC_API_KEY" in env:
            # Skips hooks, skills, plugins, .mcp.json and the repository's CLAUDE.md. It only
            # works with API-key auth (bare mode never reads OAuth or the keychain).
            cmd.append("--bare")
        return cmd

    async def _run_claude(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.command(), cwd=str(self.workdir), env=agent_env(),
            start_new_session=True,  # own process group: terminate() can kill the whole tree
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await self._read_stream(self.proc)
        finally:
            await self.terminate()

    async def terminate(self):
        """Stop the agent and everything it spawned: SIGTERM to the group, SIGKILL after a grace."""
        proc = self.proc
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), KILL_GRACE)
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # whatever is left: the agent or its orphans
        except ProcessLookupError:
            pass
        await proc.wait()

    async def _read_stream(self, proc):
        turns = 0
        async for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "assistant":
                turns += 1
                for block in msg.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block["text"].strip():
                        self.bus.emit(self.name, "say", block["text"].strip()[:200])
                    elif block.get("type") == "tool_use":
                        self.bus.emit(self.name, "tool", block.get("name", "?"),
                                      tool=block.get("name"))
                # Real progress is unknowable; estimate from turns used (capped at 95%).
                self._progress(min(95, 100 * turns / self.task.max_turns))
            elif kind == "result":
                if msg.get("is_error"):
                    raise RuntimeError(msg.get("result", "agent reported an error"))
                self.output = msg.get("structured_output", msg.get("result"))
        code = await proc.wait()
        if code != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(f"claude exited with {code}: {err[:300]}")
