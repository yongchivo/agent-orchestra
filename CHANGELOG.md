# Changelog

All notable changes to Uno. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-10-12 — Phase 5: identity and ease of use

The control page is redesigned around one flow: open, tell the orchestrator what you want,
watch the agents, approve the result. No control was relaxed: the CSP, the token, the
security headers and every validation are the same or stricter, and there are still no
third-party dependencies.

### Added
- **Simple / Advanced modes.** Simple (default) shows the chat with the orchestrator, the
  live agents and "Review & approval" only; backend, parallelism, `max_turns`, presets, paths,
  templates, the role editor and the raw event log appear in Advanced. The mode is a CSS
  class on the page: it never changes a permission or a limit, which the server enforces
  regardless of what the page sends.
- **Spanish / English.** Static dictionaries (`ui/i18n/es.json`, `en.json`) shipped as package
  data and served from the static allowlist; Spanish by default. Only the interface is
  translated; what the agents write is shown as it comes. Every string reaches the page
  through `textContent` or `placeholder`, so a dictionary value with HTML is displayed as text.
- **Visual identity: Uno, a yongchivo sub-brand.** Night-blue background with purple and teal
  glows, magenta → violet → sky-blue gradient, thick rounded strokes with a neon glow,
  spaced uppercase labels. One theme, system fonts, nothing loaded from outside. Status
  colours (merged green, review amber, failed / out of scope red) are independent of the
  gradient, always shown as icon + text, and meet WCAG AA on the background.
- **Brand assets from outside the repository.** The logo PNGs live in
  `~/.uno-orchestra/theme/uno-brand/png/` (the package is MIT, the brand is not) and are
  served by four fixed routes (`/brand/lockup.png`, `mark.png`, `favicon.png`,
  `apple-touch.png`) mapped to fixed file names: PNG only (signature checked), at most 1 MiB,
  regular files inside that directory, `image/png` with `nosniff`. Anything else is 404 and
  the page uses a neutral version of the same design (an inline SVG mark). Without the
  directory the UI is fully usable.
- **Live agents panel**: one node per agent with its role, status and last message, an
  orchestrator node, and animated pulses for each message between them (start, hand-off,
  done, merge, verdicts). Empty state: the logo with a soft glow.
- **Review & approval panel** with an always-visible pending counter: the proposed plan with
  Approve / Reject, then the run result with the reviewers' findings, the consolidated diff
  and Approve-and-merge / Reject. `Cmd/Ctrl+Enter` approves the pending item after a
  confirmation; `Enter` sends the chat.
- **Project selector** of recent repositories (those with `.uno/` are marked as configured);
  the free path field stays in Advanced. Plain-language project state with the action to
  take: "The branch is main: [Create branch]", uncommitted changes, roles changed outside
  Uno ([Review roles]), no `.uno/` (run `uno init`). Clear empty states ("Choose a project",
  "Nothing pending"). A visible notice when the token is missing or expired, or the local
  server is gone, with the instruction to run `uno` again.
- **Preferences** (language, mode, last project) in `localStorage`; the token never is (it
  stays in memory, read once from the URL fragment, which also accepts `lang=` and `mode=`).
- **Role management from both modes.** A "Roles" button always in the header opens a dialog:
  create a role (from scratch, by duplicating one, or from a template), rename it, edit every
  field, delete it (with confirmation) and save it as a template. Simple mode shows four
  plain fields (name, what it does, can modify files or read-only, which folders); Advanced
  adds description, output, output format and `max_turns`. A role used by a plan that is
  proposed or running cannot be renamed or deleted until it finishes or is cancelled.
- **Personal template library** in `~/.uno-orchestra/role-templates/` (outside any
  repository, 0600): the four built-in roles (developer, tester, security-analyst,
  architect) plus your own; "Add from template" in any project, "Save as template" from any
  role. `uno init` also offers a [n]ew role (the same four questions) and roles from the
  library after the proposals. Limits unchanged or new: names by the same regex, presets
  only, validated paths, protected paths for every role, at most 12 roles per project; UI
  changes are confirmed in the fingerprint, manual edits in the repository still ask.
- **Model and effort.** `uno_orchestra/models.json` is the only list of models, effort levels
  and simple-mode profiles (verified against Claude Code 2.1.296: `--model`, `--effort
  low|medium|high|xhigh|max`; and against the account's login: fable, opus, sonnet, haiku).
  Each role carries its own `model` and `effort`; the planner's come from `uno config`
  (`planner_model`, `planner_effort`). Opus is the maximum for every role, agent and the
  planner. Simple mode offers three profiles per role (⚡ Fast = haiku/medium, ⚖️ Balanced =
  sonnet/high, 🧠 Max = opus/max; defaults: planner Balanced, security-analyst Max,
  architect and developer Balanced, tester Fast); Advanced sets the exact pair and can
  override it for one run (the role file is untouched). **Fable is override-only**: never a
  default, refused by the schema in roles, templates, run templates and settings (a
  repository role file with it is invalid), ignored if the planner's output names a model,
  chosen only per run from the UI (chips in simple mode, the table in advanced), with a
  separate confirmation when approving a plan that uses it; no fallback or retry can reach
  it; every event and result marks its use. A note next
  to Approve says when the plan uses Max profiles. Every `start` event and every result
  records the model and effort requested, the model the CLI reported, the turns and the
  seconds. A model the CLI rejects is a clear error naming the alternatives, never a silent
  switch. Server-side, model and effort are an enum validated against the allowlist and
  passed to the CLI as separate arguments.
- `/api/status` reports `brand`, `configured` (recent repositories with `.uno/`) and
  `version`; `/api/roles` reports the branch, whether it is `main`/`master` and the preflight
  result, so the page needs one call per project.
- Tests: SVG or oversized brand file refused, symlink out of the brand directory refused,
  brand routes only from the allowlist, no brand directory → neutral page, i18n dictionaries
  served and consistent, simple mode changes no permission or limit, `localStorage` never
  holds the token, CSP unchanged, nothing external in the CSS, status colours WCAG AA.
  `scripts/smoke_security.sh` checks the i18n and brand routes against a live server.

### Changed
- Default backend in the page is `claude` (the mock stays one click away in Advanced).
- Version 0.2.0; `README.md` gains a "Personalization" section.

## [0.1.0] — 2026-10-12 — Phases 4 and 4b: Uno

### Added (phase 4: installable `uno`)
- Rename to **Uno**: installable package `uno_orchestra` with the `uno` console command
  (`pipx install`), no runtime dependencies, UI and dashboard pages as package data.
  `main.py` / `start.py` become `uno run` and `uno ui`; `uno` alone opens the UI with the
  current repository selected.
- `uno init [PATH]`: deterministic, read-only analysis of a repository (languages,
  directories, dependency managers, tests, instruction files), proposed roles (`developer`,
  `tester`, `security-analyst`, `architect`) the user accepts, edits or removes, `CLAUDE.md`
  offered as context, `.uno/` written and recorded as confirmed; only run state is added to
  `.gitignore`. Idempotent; `--force` regenerates; `--yes` is non-interactive. In a folder
  that is not a repository it offers `git init` plus an initial commit.
- `uno doctor [PATH]`: git, Python, the Claude CLI and its login, state directory
  permissions, project configuration and `PATH` shadowing, with a fix per failure.
- `uno config [get KEY | set KEY VALUE]` in `~/.uno-orchestra/config.json` (0600).
- Per-project directory `.orchestra/` → `.uno/` (renamed on confirmation, `git mv` when
  tracked; both stay protected and denied to agents) and `~/.agent-orchestra` →
  `~/.uno-orchestra` migrated on first start: copy, verify every byte, make private, then
  remove the old directory.
- Tests for init, doctor, both migrations, protection of `.uno/`, and a wheel installed in a
  clean venv that serves the UI and the dashboard.

### Added (phase 4b: detection and trust)
- `uno init` detects more: Dart, Kotlin (`.kts`), Objective-C, Scala, Vue, Svelte; manifests
  `pubspec.yaml`, `Podfile`, `*.csproj`/`*.sln`, `Package.swift`; frameworks by structure
  (Flutter, Next.js, React, Vue, Angular, Svelte, Express, Django, Flask, FastAPI, Rails,
  Laravel, Spring) and services (Firebase, Supabase, Docker, Terraform). Dependency, build and
  generated platform directories are left out of the language count; Shell counts only with
  at least three `.sh` files outside `scripts/`.
- Project **protected paths** in `.uno/project.json` (service rules and config, lockfiles,
  Flutter platform directories and `pubspec.yaml`), proposed by `init` and applied by the
  gate in parallel and pipeline runs on top of the global list; the UI's context editor
  preserves them. `tester` paths include every top-level `*test*` directory; the security
  analyst gets a project-specific focus. Warnings for `*.log` files git does not ignore and
  for Firebase app configuration files.
- Instruction files (`CLAUDE.md`, `AGENTS.md`, `*.md` named claude/agent/prompt/scaffold)
  are offered as **reference by path** (one line, agents can `Read` the file), embedded or
  skipped; anything that would not fit is referenced by default. An embedded document that
  does not fit is cut at the last markdown heading that fits, with the omitted characters
  recorded as `truncated` and reported by `init` and the Roles & project panel.
- **Referenced documents are validated** when `.uno/project.json` is loaded (relative, inside
  the repository, no symlink leaving it, `.md`/`.txt`, never a protected or credential file),
  read into the trust snapshot so the fingerprint covers `.uno/` plus their content (a changed
  document needs confirmation like a role, checked when each run starts), listed in the UI's
  confirmation preview, and **protected from the agents**: a change to one ends in
  `needs_review`.
- **Account login by default.** Agents and the planner use the Claude CLI login;
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are passed (with
  `--bare`) only after `uno config set auth api-key`. `uno doctor` shows the active mode and
  warns when a key in the environment is ignored.

[0.2.0]: https://github.com/yongchivo/uno-orchestra/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/yongchivo/uno-orchestra/releases/tag/v0.1.0
