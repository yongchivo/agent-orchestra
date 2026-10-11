# agent-orchestra

**Run several Claude Code agents in parallel on one repository — each in its own git worktree,
locked down as an untrusted worker — and watch them work live in a browser dashboard.**

![agent-orchestra dashboard](docs/dashboard.png)

## Why

- **Isolation by worktree.** Every agent gets its own git worktree and branch under
  `~/.agent-orchestra`, so agents never overwrite each other's files and every change is
  attributable to exactly one agent.
- **Parallel orchestration.** Agents run concurrently up to a configurable limit; when they
  finish, branches are merged one at a time and conflicts are reported per agent.
- **Live observability.** Every state change is appended to `events.jsonl`; a dependency-free
  dashboard streams it over Server-Sent Events, so you see tools used, progress and merges as
  they happen.
- **Untrusted by design.** The target repository and everything an agent produces may carry
  prompt injection. Agents get no shell, no network tools, no MCP servers, no repository hooks
  or settings, a filtered environment and a wall-clock timeout; a deterministic gate decides
  what gets merged. See [Security controls](#security-controls) and the
  [threat model](#threat-model).

## Quickstart (no API usage)

The `mock` backend simulates agents, so you can try the whole flow without calling Claude.

```bash
# a throwaway repository on a feature branch (the orchestrator refuses main/master)
git init -b main /tmp/demo && git -C /tmp/demo commit --allow-empty -m init
git -C /tmp/demo checkout -b demo

# terminal 1 — dashboard (binds to 127.0.0.1 only)
python dashboard/server.py --events events.jsonl --port 8765

# terminal 2 — a run with three fake agents
python main.py --repo /tmp/demo --tasks tasks.example.json --backend mock --events events.jsonl
git -C /tmp/demo log --oneline --graph
```

Requires Python 3.10+ and git. No third-party runtime dependencies.

For a real run, install and log in to the [Claude Code CLI](https://code.claude.com/docs/en/overview),
check out a feature branch in the target repository, then:

```bash
python main.py --repo /path/to/repo --tasks tasks.json --parallel 3 --timeout 900
```

## How it works

```
tasks.json ──> orchestrator ──> worktree + branch per agent ──> `claude -p --restricted ...` (in parallel)
                    │                                                │
                    └── events.jsonl  <──  start / tool / progress / done / failed
                    │
                    └── gate + merge phase: diff vs task "paths" and protected files,
                        then one branch at a time, abort + report on conflict
```

| File | Role |
|------|------|
| `orchestra/bus.py` | Append-only JSONL event bus: 0600, secrets redacted, size-rotated |
| `orchestra/worktree.py` | git worktree create / commit / diff / merge / remove, hooks disabled |
| `orchestra/scope.py` | Deterministic merge gate: task `paths`, protected files, symlinks, binaries |
| `orchestra/worker.py` | Runs one agent (`claude` or `mock` backend) in a locked-down subprocess |
| `orchestra/orchestrator.py` | Preflight, limits, parallelism, gate, merge phase, cleanup |
| `main.py` | CLI |
| `dashboard/server.py` | Serves the dashboard and tails `events.jsonl` over SSE (stdlib only) |
| `dashboard/index.html` | Live view: orchestrator in the centre, one node per agent, message bus below |

Task format (`tasks.json`):

```json
[{"name": "billing",
  "prompt": "Port billing to Rust under rust/billing. Keep the public API.",
  "paths": ["rust/billing/**"],
  "preset": "edit",
  "max_turns": 25}]
```

- `name`: `^[a-z0-9][a-z0-9-]{0,31}$` (it becomes a directory and a branch name).
- `paths`: globs of the files the agent may change. Anything else blocks the merge.
- `preset`: `edit` (Read, Glob, Grep, Edit, Write) or `readonly` (Read, Glob, Grep).
- `max_turns`: 1..50. Agents do not commit; the orchestrator commits their worktree.

Each agent ends in one state: `merged`, `conflict`, `out_of_scope` (touched files outside
`paths`), `needs_review` (touched a protected file, added a symlink or a large binary) or
`failed` (error or timeout). Branches `agent/<run>/<name>` are kept for the last three so you
can inspect them (`git diff <branch>...<your-branch>`, `git merge <branch>` once reviewed). When
you no longer need them:

```bash
git branch --list 'agent/*'                                   # see what is there
git for-each-ref --format='%(refname:short)' refs/heads/agent/ | xargs git branch -D
```

Event format (`events.jsonl`, one JSON object per line):

```json
{"ts": 1791463170.1, "source": "billing", "target": "orchestrator",
 "kind": "progress", "message": "40%", "progress": 40}
```

Kinds: `run_start`, `warning`, `start`, `say`, `tool`, `progress`, `done`, `failed`, `merge`,
`conflict`, `out_of_scope`, `needs_review`, `run_end`.

## Dashboard

```bash
python dashboard/server.py --events events.jsonl --port 8765
```

Open http://127.0.0.1:8765. Nodes turn green when an agent's branch merges, red on failure or
merge conflict. The page is vanilla HTML/JS with no dependencies. `--host 0.0.0.0` exposes it on
the network; it has no authentication, so only do that on a trusted LAN.

## Security controls

Every control below has a test in `tests/`; the [threat model](#threat-model) maps them to
attackers. Flags were verified against `claude --help` of Claude Code 2.1.296.

**Agent permissions** (`orchestra/worker.py`)

- No shell, ever. Presets are `readonly` (Read, Glob, Grep) and `edit` (+ Edit, Write). There is
  no way to enable Bash from a task file: `git` alone can run arbitrary commands through
  `core.pager`, `core.sshCommand`, `core.fsmonitor` or `!` aliases.
- `--restricted`: removes Bash and the other code-running tools and WebFetch, confines the file
  tools to the worktree, and ignores user, project and local settings files.
- `--tools` fixes the available built-ins to the preset; `--disallowedTools
  "Bash,WebFetch,WebSearch,mcp__*"` is the explicit deny list on top.
- `--strict-mcp-config --mcp-config '{"mcpServers": {}}'`: no MCP server, from anywhere.
- `--settings <own file>`: `disableAllHooks`, `Read` deny rules for `~/.ssh`, `~/.aws`,
  `~/.config/gh`, `~/.gnupg`, `~/Library/Keychains` and `.env*` anywhere, and the native
  sandbox enabled. The sandbox (seatbelt on macOS, bubblewrap on Linux) only covers shell
  commands, which are already removed; it is defence in depth in case a preset ever brings
  Bash back.
- `--permission-mode dontAsk --permission-prompts none`: anything that would prompt is denied.
- `--no-session-persistence`: no transcript with repository contents is written to disk.
- `--bare` is added when `ANTHROPIC_API_KEY` is set: it also skips the repository's CLAUDE.md,
  skills and plugins. Bare mode never reads OAuth credentials, so with a subscription login the
  repository's CLAUDE.md can still reach the model (see residual risks).

**Subprocess environment**

- Built from an allowlist: `PATH`, `HOME`, `LANG`, `TERM`, `USER`, `TMPDIR` and the Anthropic
  auth variables. No `GITHUB_TOKEN`, `AWS_*`, `OPENAI_*`, `SSH_AUTH_SOCK` or `CLAUDE*` state
  from a parent session.
- Each agent runs in its own process group (`start_new_session=True`).

**Limits**

- Wall-clock timeout per agent (`--timeout`, default 15 min). On expiry or cancellation the
  whole process group gets SIGTERM, then SIGKILL after 5 s.
- Hard caps: 8 agents in parallel, 16 agents per run, 50 turns per agent.

**Git**

- Every git call runs with `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, never with
  `shell=True`, so a repository cannot run code through hooks or the fsmonitor command.
- Preflight: the tree must be clean and the current branch must not be `main` or `master`. If
  the repository tracks `CLAUDE.md`, `AGENTS.md` or a `.claude/` directory at any depth, the run
  prints a warning and emits a `warning` event naming them: those files can steer the agent and
  only `--bare` keeps them out of context (see residual risks).
- Worktrees live in `~/.agent-orchestra/worktrees/<run_id>` (0700), not next to the repository.
- `try/finally`: agent processes and worktrees are always cleaned up, also after an error or
  Ctrl+C.

**Merge gate** (`orchestra/scope.py`, deterministic: it reads the diff, not the model)

- Each task declares `paths`. A diff that touches anything else is not merged: `out_of_scope`.
- Protected files are never merged automatically even when `paths` cover them: `.github/`,
  `.claude/`, `CLAUDE.md`, `.git*`, `.gitattributes`, `.gitmodules`, `Makefile`, `*.sh`,
  `setup.py`, `pyproject.toml`, `package.json`, `requirements*.txt`, `Dockerfile`. New symlinks,
  new binaries over 1 MiB and any `.env*` file are blocked too: `needs_review`.
- Every text file in the diff is scanned with the same secret patterns the log redacts
  (`sk-…`, `ghp_…`, `AKIA…`, `BEGIN PRIVATE KEY`). A hit is reported as `file:line (possible
  secret)` without the secret itself, and the branch is not merged.
- Merges are serialised and aborted on conflict, so a conflict is attributable to one agent and
  the branch is never left half-merged.

**Event log**

- `events.jsonl` is 0600 and the state directory 0700.
- Messages are cut at 500 characters and obvious secrets (`sk-…`, `ghp_…`, `AKIA…`,
  `BEGIN PRIVATE KEY`) are redacted before writing.
- The log rotates at 10 MiB.

**Dashboard**

- Serves an allowlist (`/` and `/index.html`) and nothing else from its directory.
- Caps simultaneous SSE clients (`--max-clients`, default 8) and binds to 127.0.0.1.
- Escapes every event field before rendering and sends a CSP with `default-src 'none'` and
  `connect-src 'self'`, so an injected payload can neither run as script nor talk off-origin.

**Project**: [SECURITY.md](SECURITY.md), Dependabot for dev dependencies and actions, CI with
`permissions: contents: read` and actions pinned by commit SHA.

## Threat model

**Assets**: the host (files, credentials, shell), the target repository's integrity (what lands
on the branch), the user's Anthropic quota, and the event log.

**Attackers and what stops them**

| Attacker | What they try | Control |
|----------|---------------|---------|
| Malicious target repository | Run code through git hooks, `.claude/settings.json` hooks, `.mcp.json`, skills | hooks disabled on every git call; `--restricted` ignores repo settings; `--strict-mcp-config`; `--disable-slash-commands`; `--bare` with API-key auth |
| Prompt-injected agent | Read `~/.ssh` or `.env`, exfiltrate, run commands, edit CI or build files, write outside its task | no Bash/WebFetch/WebSearch/MCP; file tools confined to the worktree plus deny rules; merge gate (`paths`, protected files, symlinks); timeout and turn cap |
| Malicious web content | Reach the agent through fetched pages | WebFetch and WebSearch removed |
| Another device on the network | Read the dashboard or the event stream | dashboard bound to 127.0.0.1; SSE client cap |
| Another user of the Mac | Read worktrees, agent config or the log | state dir 0700, config and log 0600 |

**STRIDE, briefly**

| Category | Example here | Control |
|----------|--------------|---------|
| Spoofing | An agent impersonating the orchestrator on the bus | the orchestrator writes the log; agent output only enters as `message` fields, escaped |
| Tampering | Agent edits `.github/workflows` or `Makefile` to run code later | protected files need manual review; `paths` gate |
| Repudiation | Which agent made a change? | one branch and one author per agent, serialised merges |
| Information disclosure | Credentials in the log or reachable by the agent | env allowlist, deny rules, `--restricted`, redaction, 0600 |
| Denial of service | Runaway agent, fork bomb, unbounded log, SSE flood | process-group timeout, hard caps, log rotation, client cap |
| Elevation of privilege | Agent gains a shell or network | no Bash; sandbox as a second layer; no MCP; no hooks |

## Residual risks

- **No OS sandbox around the file tools.** Claude Code's sandbox only covers shell commands,
  which are removed. The file tools are confined by Claude Code's own permission checks, not by
  the kernel. A bug there is a bug here. A container per agent (only its worktree mounted, no
  network except the API) would close this gap.
- **The model can be manipulated inside its scope.** Within `paths`, an injected agent can still
  write bad code. The gate limits where, not what. Review the diff before you ship it.
- **Agent instruction files with OAuth login.** A repository's `CLAUDE.md`, `AGENTS.md` or
  `.claude/` can carry prompt injection. Only `--bare` skips them, and bare mode requires
  `ANTHROPIC_API_KEY`. With a subscription login they are loaded into context; the preflight
  warning tells you when that is the case, and the merge gate still limits what such an agent
  can land.
- **`--restricted` is a Claude Code feature.** The confinement of the file tools and the
  ignoring of repository settings are enforced by the CLI (v2.1.248+), not by this project.
- **Secret redaction is pattern-based.** It catches obvious key formats, not every secret.
- **No cost cap** beyond `max_turns`; `--max-budget-usd` is a one-line addition if you need it.
- **Agents do not talk to each other**; they only report to the orchestrator.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests drive the `mock` backend against a temporary git repository on a feature branch and
back every control above: full runs with merges, per-agent isolation, a merge conflict attributed
to one agent and aborted cleanly, Bash impossible to enable, MCP and repository settings
disabled, the environment allowlist, a timeout that kills a process tree, git hooks and
fsmonitor traps that never fire, preflight refusals and the instruction-file warning,
out-of-scope and protected-file diffs blocked, symlinks, large binaries, `.env` files and
secrets in the diff blocked, cleanup after an exception, secret redaction,
0600/0700 permissions, log rotation, the dashboard allowlist (404 elsewhere), the SSE cap, and
the HTML escaping against an XSS payload (needs `node`). CI runs the same suite.

## Limitations

- `progress` for real agents is an estimate (turns used / `max_turns`), not true completion.
- CLI flags were verified against Claude Code 2.1.296. `--max-turns` is still accepted but no
  longer listed in `claude --help`; `--restricted` and `--permission-prompts` need v2.1.259+.

## License

[MIT](LICENSE)
