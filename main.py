#!/usr/bin/env python3
"""CLI: python main.py --repo /path/to/repo --tasks tasks.json [--backend mock]"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from orchestra import orchestrator
from orchestra.bus import EventBus
from orchestra.worker import Task


def main():
    p = argparse.ArgumentParser(description="Run several Claude Code agents in parallel.")
    p.add_argument("--repo", required=True, help="git repository the agents will work on")
    p.add_argument("--tasks", required=True, help="JSON file with the task list")
    p.add_argument("--backend", choices=["claude", "mock"], default="claude")
    p.add_argument("--parallel", type=int, default=3, help="max agents running at once")
    p.add_argument("--events", default="events.jsonl", help="event log read by the dashboard")
    p.add_argument("--keep-worktrees", action="store_true",
                   help="keep worktrees and branches for inspection after the run")
    p.add_argument("--timeout", type=float, default=900,
                   help="wall-clock seconds per agent before its process group is killed")
    p.add_argument("--state-dir", default=None,
                   help="where worktrees and agent config live (default ~/.agent-orchestra)")
    args = p.parse_args()

    try:
        tasks = [Task(**t) for t in json.loads(Path(args.tasks).read_text(encoding="utf-8"))]
        bus = EventBus(args.events)
        summary = asyncio.run(orchestrator.run(
            args.repo, tasks, bus, args.backend, args.parallel, args.keep_worktrees,
            timeout=args.timeout, state_dir=args.state_dir))
    except (ValueError, RuntimeError, TypeError) as exc:  # bad task file or repository state
        sys.exit(f"error: {exc}")

    for name, state in summary.items():
        print(f"{name:12} {state}")
    sys.exit(0 if all(s == "merged" for s in summary.values()) else 1)


if __name__ == "__main__":
    main()
