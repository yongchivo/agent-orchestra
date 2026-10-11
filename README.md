# agent-orchestra

**Run several Claude Code agents in parallel on one repository — each in its own git worktree — and watch them work live in a browser dashboard.**

![agent-orchestra dashboard](docs/dashboard.png)

## Why

- **Isolation by worktree.** Every agent gets its own git worktree and branch, so agents never
  overwrite each other's files and every change is attributable to exactly one agent.
- **Parallel orchestration.** Agents run concurrently up to a configurable limit; when they
  finish, branches are merged one at a time and conflicts are reported per agent.
- **Live observability.** Every state change is appended to `events.jsonl`; a dependency-free
  dashboard streams it over Server-Sent Events, so you see tools used, progress and merges as
  they happen.

## Quickstart (no API usage)

The `mock` backend simulates agents, so you can try the whole flow without calling Claude.

```bash
# a throwaway repository to work on
git init /tmp/demo && git -C /tmp/demo commit --allow-empty -m init

# terminal 1 — dashboard (binds to 127.0.0.1 only)
python dashboard/server.py --events events.jsonl --port 8765

# terminal 2 — a run with three fake agents
python main.py --repo /tmp/demo --tasks tasks.example.json --backend mock --events events.jsonl
git -C /tmp/demo log --oneline --graph
```

Requires Python 3.10+ and git. No third-party runtime dependencies.

For a real run, install and log in to the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), then:

```bash
python main.py --repo /path/to/repo --tasks tasks.json --parallel 3
```

## How it works

```
tasks.json ──> orchestrator ──> worktree + branch per agent ──> `claude -p` (headless, in parallel)
                    │                                                │
                    └── events.jsonl  <──  start / tool / progress / done / failed
                    │
                    └── merge phase: one branch at a time, abort + report on conflict
```

| File | Role |
|------|------|
| `orchestra/bus.py` | Append-only JSONL event bus |
| `orchestra/worktree.py` | git worktree create / commit / merge / remove |
| `orchestra/worker.py` | Runs one agent (`claude` or `mock` backend) and emits events |
| `orchestra/orchestrator.py` | Parallelism limit, merge phase, summary |
| `main.py` | CLI |
| `dashboard/server.py` | Serves the dashboard and tails `events.jsonl` over SSE (stdlib only) |
| `dashboard/index.html` | Live view: orchestrator in the centre, one node per agent, message bus below |

Task format (`tasks.json`):

```json
[{"name": "billing", "prompt": "Port billing to Rust...", "max_turns": 25}]
```

Optional per task: `allowed_tools` (list) to widen or narrow the default tool set.

Event format (`events.jsonl`, one JSON object per line):

```json
{"ts": 1791463170.1, "source": "billing", "target": "orchestrator",
 "kind": "progress", "message": "40%", "progress": 40}
```

Kinds: `run_start`, `start`, `say`, `tool`, `progress`, `done`, `failed`, `merge`, `conflict`, `run_end`.

## Dashboard

```bash
python dashboard/server.py --events events.jsonl --port 8765
```

Open http://127.0.0.1:8765. Nodes turn green when an agent's branch merges, red on failure or
merge conflict. The page is vanilla HTML/JS with no dependencies. `--host 0.0.0.0` exposes it on
the network; it has no authentication, so only do that on a trusted LAN.

## Security notes (design decisions)

- **Least privilege:** each agent only gets `Read, Edit, Write, Glob, Grep, Bash(git *)` unless the
  task explicitly sets `allowed_tools`. Agents run with `--permission-mode dontAsk`, so any tool
  outside that list is denied instead of prompted. No arbitrary shell by default.
- **Isolation:** one worktree per agent; agents cannot touch each other's working tree.
- **Input validation:** task names become directories and branch names, so they are restricted to
  `^[a-z0-9][a-z0-9-]{0,31}$` (blocks path traversal like `../evil` and option injection like `-x`).
- **Untrusted output:** agent messages may echo prompt-injected content from the repository, so the
  dashboard HTML-escapes every event field before rendering (no XSS via the event stream).
- **Serialised merges:** a conflict is always attributable to exactly one agent and never leaves
  the main branch half-merged (`git merge --abort`).
- **Local by default:** the dashboard binds to `127.0.0.1`.
- **Known limits / future work:**
  - Agents share the machine's filesystem outside their worktree; a container or sandbox per agent
    would close that gap.
  - `Bash(git *)` is not a hard boundary: git can run arbitrary commands through config, aliases
    and hooks. Treat it as a guardrail, not a sandbox.
  - No cost cap per agent besides `max_turns`.
  - Agents do not talk to each other; they only report to the orchestrator.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests drive the `mock` backend against a temporary git repository: a full run with merges,
rejection of invalid task names and of duplicate names.

## Limitations

- `progress` for real agents is an estimate (turns used / `max_turns`), not true completion.
- CLI flags were verified against Claude Code 2.1.296. `--max-turns` is still accepted but no
  longer listed in `claude --help`, so it may disappear; check `claude --help` on other versions.

## License

[MIT](LICENSE)
