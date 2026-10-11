"""A Worker runs one agent inside its own worktree and reports to the bus.

Backends:
  claude  - headless Claude Code CLI (`claude -p ... --output-format stream-json`)
  mock    - fake agent for tests and for developing the dashboard without API usage
"""
import asyncio
import json
import random
import re
from dataclasses import dataclass, field

from . import worktree
from .bus import EventBus

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# Least privilege by default: edit files and use git; nothing else.
DEFAULT_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(git *)"]


@dataclass
class Task:
    name: str
    prompt: str
    allowed_tools: list = field(default_factory=lambda: list(DEFAULT_TOOLS))
    max_turns: int = 20

    def __post_init__(self):
        # The name becomes a directory and a branch: validate it strictly.
        if not NAME_RE.match(self.name):
            raise ValueError(f"invalid task name {self.name!r}: use a-z, 0-9 and '-'")


class Worker:
    def __init__(self, task, workdir, branch, bus: EventBus, backend="claude"):
        self.task = task
        self.workdir = workdir
        self.branch = branch
        self.bus = bus
        self.backend = backend
        self.status = "pending"
        self.progress = 0

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
            if self.backend == "mock":
                await self._run_mock()
            else:
                await self._run_claude()
            committed = worktree.commit_all(self.workdir, f"agent {self.name}: work in progress")
            self.status = "done"
            self._progress(100)
            self.bus.emit(self.name, "done", "finished", target="orchestrator",
                          extra_commit=committed)
        except Exception as exc:  # noqa: BLE001 - report any failure to the bus
            self.status = "failed"
            self.bus.emit(self.name, "failed", str(exc), target="orchestrator")
        return self.status

    async def _run_mock(self):
        steps = random.randint(4, 7)
        for i in range(1, steps + 1):
            await asyncio.sleep(random.uniform(0.3, 1.0))
            self.bus.emit(self.name, "tool", f"Edit {self.name}/step{i}.txt", tool="Edit")
            self._progress(100 * i / steps)
        # Append (not overwrite) so repeated runs always produce a real commit to merge.
        with (self.workdir / f"{self.name}.txt").open("a", encoding="utf-8") as f:
            f.write(f"work of agent {self.name} on {self.branch}\n")

    def command(self):
        """Headless CLI invocation. Separate from _run_claude so tests can check the flags."""
        return [
            "claude", "-p", self.task.prompt,
            "--output-format", "stream-json", "--verbose",
            "--max-turns", str(self.task.max_turns),
            "--allowedTools", ",".join(self.task.allowed_tools),
            # dontAsk: anything not in --allowedTools is denied, never prompted.
            "--permission-mode", "dontAsk",
        ]

    async def _run_claude(self):
        proc = await asyncio.create_subprocess_exec(
            *self.command(), cwd=str(self.workdir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            await self._read_stream(proc)
        finally:
            if proc.returncode is None:  # e.g. the agent reported an error mid-stream
                proc.kill()
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
            elif kind == "result" and msg.get("is_error"):
                raise RuntimeError(msg.get("result", "agent reported an error"))
        code = await proc.wait()
        if code != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            raise RuntimeError(f"claude exited with {code}: {err[:300]}")
