"""Orchestrator: create worktrees, run workers in parallel, merge the results."""
import asyncio
import uuid
from pathlib import Path

from . import worktree
from .bus import EventBus
from .worker import Task, Worker


async def run(repo, tasks, bus: EventBus, backend="claude", max_parallel=3, keep_worktrees=False):
    names = [t.name for t in tasks]
    if len(set(names)) != len(names):
        raise ValueError("task names must be unique")

    repo = Path(repo).resolve()
    run_id = uuid.uuid4().hex[:6]
    base_dir = repo.parent / f".orchestra-{run_id}"
    base_dir.mkdir()

    bus.emit("orchestrator", "run_start", f"{len(tasks)} workers",
             run_id=run_id, workers=names, backend=backend)

    # Worktrees are created sequentially: concurrent `git worktree add` can race on git's lock.
    workers = []
    for t in tasks:
        path, branch = worktree.create(repo, base_dir, run_id, t.name)
        workers.append(Worker(t, path, branch, bus, backend))

    sem = asyncio.Semaphore(max_parallel)

    async def guarded(w):
        async with sem:
            return await w.run()

    await asyncio.gather(*(guarded(w) for w in workers))

    # Merge phase: one branch at a time so a conflict is attributable to a single agent.
    summary = {}
    for w in workers:
        if w.status != "done":
            summary[w.name] = "failed"
            continue
        ok, out = worktree.merge(repo, w.branch)
        summary[w.name] = "merged" if ok else "conflict"
        bus.emit("orchestrator", "merge" if ok else "conflict", out[:200], target=w.name)

    if not keep_worktrees:
        for w in workers:
            worktree.remove(repo, w.workdir)
        try:
            base_dir.rmdir()
        except OSError:
            pass

    bus.emit("orchestrator", "run_end", str(summary), summary=summary)
    return summary
