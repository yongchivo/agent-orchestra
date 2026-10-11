"""`uno`: the console entry point.

    uno                 open the control UI with the current project selected (same as `uno ui`)
    uno init [PATH]     set a repository up: analyse it, propose roles, write .uno/
    uno run TASKS.json  parallel mode from the terminal
    uno doctor [PATH]   check git, Python, the Claude CLI and login, state dir, project, PATH
    uno dashboard       live view of the event log
"""
import argparse
import asyncio
import json
import secrets
import sys
import webbrowser
from pathlib import Path

from . import __version__, state

COMMANDS = ("init", "ui", "run", "doctor", "dashboard")


def _common(p):
    p.add_argument("--state-dir", default=None, metavar="DIR",
                   help=f"token, trust file, recent repos and worktrees (default {state.default_dir()})")
    return p


def build_parser():
    p = argparse.ArgumentParser(prog="uno", description="Uno: run locked-down Claude Code agents on a repository, in parallel or as a role pipeline.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Without a subcommand, `uno` opens the UI. Pass -h to a subcommand for its options.")
    p.add_argument("--version", action="version", version=f"uno {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = _common(sub.add_parser("init", help="set up the repository in PATH (default: here): analyse, propose roles, write .uno/",
                               description="Deterministic, read-only analysis; then each proposed role is shown for you to accept, "
                                           "edit or remove. Only .uno/ and .gitignore are written."))
    s.add_argument("path", nargs="?", default=".", help="repository (default: current directory)")
    s.add_argument("--force", action="store_true", help="regenerate the proposed roles after confirmation")
    s.add_argument("--yes", action="store_true", help="accept every proposal without asking (non-interactive)")

    s = _common(sub.add_parser("ui", help="open the local control page (loopback only) with this project selected",
                               description="Starts the local web UI on 127.0.0.1 with a one-time token and opens the browser. "
                                           "The current git repository is pre-selected."))
    s.add_argument("--host", default="127.0.0.1", help="must be a loopback address")
    s.add_argument("--port", type=int, default=8766)
    s.add_argument("--allowed-root", action="append", default=None, metavar="DIR",
                   help="directory under which repositories may live (repeatable; default: your home and the current repository)")
    s.add_argument("--idle-timeout", type=float, default=7200, help="seconds without requests before the server exits")
    s.add_argument("--agent-timeout", type=float, default=900, help="wall-clock seconds per agent")
    s.add_argument("--no-browser", action="store_true", help="print the URL instead of opening the browser")

    s = _common(sub.add_parser("run", help="run a tasks.json in parallel mode from the terminal",
                               description="Each task runs as a locked-down agent in its own worktree; the merge gate decides what lands."))
    s.add_argument("tasks", help="JSON file with the task list (see tasks.example.json)")
    s.add_argument("--repo", default=".", help="git repository the agents work on (default: current directory)")
    s.add_argument("--backend", choices=["claude", "mock"], default="claude")
    s.add_argument("--parallel", type=int, default=3, help="max agents running at once")
    s.add_argument("--events", default=None, metavar="FILE", help="event log (default <state-dir>/events.jsonl)")
    s.add_argument("--keep-worktrees", action="store_true", help="keep worktrees and branches for inspection")
    s.add_argument("--timeout", type=float, default=900, help="wall-clock seconds per agent before its process group is killed")
    s.add_argument("--yes", action="store_true", help="answer yes to the .orchestra/ -> .uno/ migration prompt")

    s = _common(sub.add_parser("doctor", help="check the installation and the project in PATH; explain how to fix each failure"))
    s.add_argument("path", nargs="?", default=".", help="project to check (default: current directory)")

    s = _common(sub.add_parser("dashboard", help="live dashboard tailing the event log (read-only)"))
    from .dashboard.server import add_arguments
    add_arguments(s)
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv.insert(0, "ui")
    args = build_parser().parse_args(argv)
    if args.state_dir is None:
        migrated = state.migrate_legacy()
        if migrated:
            print(f"uno: migrated ~/{state.LEGACY} to {migrated}", file=sys.stderr)
    state_dir = Path(args.state_dir or state.default_dir())
    try:
        return {"init": cmd_init, "ui": cmd_ui, "run": cmd_run, "doctor": cmd_doctor, "dashboard": cmd_dashboard}[args.cmd](args, state_dir)
    except (ValueError, RuntimeError, OSError) as exc:
        sys.exit(f"uno: error: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)


def cmd_init(args, state_dir):
    from . import init
    ask = (lambda q, d: d) if args.yes else None
    return init.run(args.path, state_dir, ask=ask, force=args.force)


def cmd_doctor(args, state_dir):
    from . import doctor
    return 1 if doctor.run(args.path, state_dir) else 0


def cmd_dashboard(args, state_dir):
    from .dashboard.server import main as dashboard_main
    dashboard_main(args)
    return 0


def cmd_ui(args, state_dir):
    from .init import git_root
    from .ui.server import UIServer, write_token
    roots = [Path(r) for r in args.allowed_root] if args.allowed_root else [Path.home()]
    here = git_root(Path.cwd())
    if here and not args.allowed_root and not any(here == r.resolve() or here.is_relative_to(r.resolve()) for r in roots):
        roots.append(here)
    token = secrets.token_urlsafe(32)
    write_token(state_dir, token)
    srv = UIServer(args.host, args.port, token, state_dir, roots, args.idle_timeout, args.agent_timeout)
    if here:
        srv.remember_repo(here)   # the page pre-selects the most recent repository
    url = f"http://{args.host}:{srv.server_port}/#token={token}"
    print(f"Uno UI: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("UI stopped", flush=True)
    return 0


def cmd_run(args, state_dir):
    from . import init, orchestrator
    from .bus import EventBus
    from .worker import Task
    repo = init.git_root(Path(args.repo).resolve())
    if repo is None:
        raise ValueError(f"{args.repo} is not inside a git repository")
    ask = (lambda q, d: "y") if args.yes else (lambda q, d: input(f"{q} [{d}] ") or d)
    init.migrate_legacy_dir(repo, ask, print)
    tasks = [Task(**t) for t in json.loads(Path(args.tasks).read_text(encoding="utf-8"))]
    bus = EventBus(args.events or state_dir / "events.jsonl")
    summary = asyncio.run(orchestrator.run(repo, tasks, bus, args.backend, args.parallel, args.keep_worktrees,
                                           timeout=args.timeout, state_dir=state_dir))
    for name, verdict in summary.items():
        print(f"{name:12} {verdict}")
    return 0 if all(v == "merged" for v in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
