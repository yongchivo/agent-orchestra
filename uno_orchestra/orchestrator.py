"""Orchestrator: create worktrees, run workers in parallel, gate and merge the results."""
import asyncio
import os
import shutil
import sys
import uuid
from pathlib import Path

from . import roles as roles_mod, scope, state, worktree
from .bus import EventBus
from .worker import Task, Worker, write_agent_config

MAX_PARALLEL = 8
MAX_TASKS = 16


def _mkdir_private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)  # mkdir's mode is subject to umask; make 0700 unconditional


async def run(repo, tasks, bus: EventBus, backend="claude", max_parallel=3,
              keep_worktrees=False, timeout=900, state_dir=None):
    names = [t.name for t in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique")
    if not 1 <= len(tasks) <= MAX_TASKS:
        raise ValueError(f"between 1 and {MAX_TASKS} tasks per run")
    if not 1 <= max_parallel <= MAX_PARALLEL:
        raise ValueError(f"--parallel must be 1..{MAX_PARALLEL}")

    repo = Path(repo).resolve()
    branch = worktree.preflight(repo)
    base = worktree.head(repo)
    steering = worktree.agent_instruction_files(repo)
    protected = roles_mod.project_protected(repo)   # only adds restrictions: no trust needed
    if steering:
        # Only --bare (API-key auth) keeps these out of the model's context. See README.
        msg = ("the repository contains agent instruction files that may reach the model: "
               + ", ".join(steering[:10]) + (" ..." if len(steering) > 10 else ""))
        print(f"warning: {msg}", file=sys.stderr)
        bus.emit("orchestrator", "warning", msg[:300], files=steering)
    run_id = uuid.uuid4().hex[:6]
    # Worktrees live under the state directory, never next to the repository.
    state_dir = Path(state_dir or state.default_dir())
    run_dir = state_dir / "worktrees" / run_id
    for d in (state_dir, state_dir / "worktrees", run_dir):
        _mkdir_private(d)
    config_dir = run_dir / "_config"  # "_" is not a valid task name character: no clash
    write_agent_config(config_dir)

    bus.emit("orchestrator", "run_start", f"{len(tasks)} workers on {branch}",
             run_id=run_id, workers=names, backend=backend)

    workers = []
    summary = {}
    try:
        # Worktrees are created sequentially: concurrent `git worktree add` can race on git's lock.
        for t in tasks:
            path, br = worktree.create(repo, run_dir, run_id, t.name)
            workers.append(Worker(t, path, br, bus, backend, timeout, config_dir))

        sem = asyncio.Semaphore(max_parallel)

        async def guarded(w):
            async with sem:
                return await w.run()

        await asyncio.gather(*(guarded(w) for w in workers))

        # Merge phase: one branch at a time so a conflict is attributable to a single agent.
        for w in workers:
            if w.status != "done":
                summary[w.name] = "failed"
                continue
            verdict, detail = scope.check(worktree.changes(repo, base, w.branch), w.task.paths,
                                          read=lambda path, b=w.branch: worktree.show(repo, b, path),
                                          protected=protected)
            if verdict != "ok":
                summary[w.name] = verdict
                bus.emit("orchestrator", verdict, detail[:300], target=w.name, branch=w.branch)
                continue
            ok, out = worktree.merge(repo, w.branch)
            summary[w.name] = "merged" if ok else "conflict"
            bus.emit("orchestrator", "merge" if ok else "conflict", out[:200], target=w.name)
    except BaseException as exc:
        bus.emit("orchestrator", "failed", f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        # Always: no agent process and no worktree survives the run, even after Ctrl+C.
        for w in workers:
            await w.terminate()
        if not keep_worktrees:
            for w in workers:
                worktree.remove(repo, w.workdir)
            shutil.rmtree(run_dir, ignore_errors=True)

    bus.emit("orchestrator", "run_end", str(summary), summary=summary)
    return summary
