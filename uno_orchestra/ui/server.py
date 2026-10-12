"""Local web UI for Uno: launch and review runs without the terminal.

Single user, loopback only. The server enforces every rule and the page only displays
them: a random per-start token in a header on every API call (never a cookie or query
string), Host and Origin validation, JSON-only POST bodies with a size cap, repositories
restricted to allowed roots, permission presets only, no inline scripts, and automatic
shutdown after a period of inactivity. Static files (page, script, style, i18n dictionaries)
come from an allowlist; brand images come from four fixed routes mapped to fixed PNG files
under <state-dir>/theme/ (signature and size checked, 404 otherwise). The core (orchestra/)
is invoked as it is; nothing here relaxes a control.
"""
import asyncio
import datetime
import hmac
import json
import os
import re
import threading
import time
from dataclasses import asdict
from importlib import resources
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from uno_orchestra import __version__, config, models, orchestrator, pipeline, roles as roles_mod, scope, worktree
from uno_orchestra.bus import EventBus
from uno_orchestra.worker import NAME_RE, OUTPUTS, PRESETS, Task

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/i18n/es.json": ("i18n/es.json", "application/json"),
    "/i18n/en.json": ("i18n/en.json", "application/json"),
}
# Brand images live OUTSIDE the repository (<state-dir>/theme/uno-brand/png/): the package is
# MIT, the brand is not. Fixed routes to fixed file names, PNG only (signature checked), size
# capped; anything else is 404 and the page falls back to its neutral mark.
BRAND_DIR = Path("theme") / "uno-brand" / "png"
BRAND = {
    "/brand/lockup.png": "uno-lockup-horizontal-transparent.png",
    "/brand/mark.png": "uno-mark-512.png",
    "/brand/favicon.png": "uno-favicon-32.png",
    "/brand/apple-touch.png": "uno-apple-touch-180.png",
}
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_BRAND = 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TOKEN_HEADER = "X-Orchestra-Token"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 64 * 1024
MAX_DIFF = 200 * 1024
BACKENDS = ("mock", "claude")
TASK_FIELDS = {"name", "prompt", "paths", "preset", "max_turns", "output", "model", "effort"}
MAX_CHAT = 4000
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
RECENT_MAX = 10


# --- validation (pure functions, tested directly) -------------------------------------------

def validate_repo(raw, roots):
    """Absolute, existing, under an allowed root after resolving symlinks, root of a git repo."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("repository path is required")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("repository path must be absolute")
    real = path.resolve()
    if not real.is_dir():
        raise ValueError("repository path does not exist")
    if not any(real == r or real.is_relative_to(r) for r in roots):
        raise ValueError("repository is outside the allowed directories")
    top = worktree.git(real, "rev-parse", "--show-toplevel", check=False)
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != real:
        raise ValueError("path is not the root of a git repository")
    return real


def parse_tasks(raw):
    """Build core Task objects from UI JSON: only the known fields, presets only, paths required."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one task is required")
    tasks = []
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            raise ValueError(f"task {i}: must be an object")
        unknown = sorted(set(t) - TASK_FIELDS)
        if unknown:
            raise ValueError(f"task {i}: unknown fields {unknown}")
        for key in ("name", "prompt", "paths"):
            if key not in t:
                raise ValueError(f"task {i}: '{key}' is required")
        tasks.append(Task(str(t["name"]), str(t["prompt"]), t["paths"],
                          str(t.get("preset", "edit")), t.get("max_turns", 20),
                          output=str(t.get("output", "text")),
                          model=t.get("model"), effort=t.get("effort")))
    if len({t.name for t in tasks}) != len(tasks):
        raise ValueError("task names must be unique")
    return tasks


def advise(tasks, backend):
    """Non-blocking warnings: overlapping paths, prompts that name no path, too big a start."""
    warnings = []
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if any(ga == gb or scope._glob_re(ga).match(gb) or scope._glob_re(gb).match(ga)
                   for ga in a.paths for gb in b.paths):
                warnings.append(f"'{a.name}' and '{b.name}' have overlapping paths: "
                                "they can produce a merge conflict")
    for t in tasks:
        literal = [seg for g in t.paths for seg in g.split("/") if seg and not re.search(r"[*?]", seg)]
        if not literal:
            warnings.append(f"'{t.name}': its paths match everything; narrow them to the files it should touch")
        elif not any(seg in t.prompt for seg in literal):
            warnings.append(f"'{t.name}': the prompt does not mention any of its paths "
                            f"({', '.join(literal[:3])}); say which files to edit")
    if backend == "claude":
        if len(tasks) > 3:
            warnings.append("start small: more than 3 agents on the claude backend")
        if any(t.max_turns > 20 for t in tasks):
            warnings.append("start small: max_turns above 20 on the claude backend")
    return warnings


def brand_asset(state_dir, route):
    """Bytes of one allow-listed brand PNG, or None (missing, not a regular file inside the brand
    directory, too big, or not a PNG by signature: an SVG renamed .png is refused)."""
    name = BRAND.get(route)
    if name is None:
        return None
    root = (Path(state_dir) / BRAND_DIR).resolve()
    path = root / name
    try:
        real = path.resolve(strict=True)
        if not real.is_relative_to(root) or not real.is_file() or real.stat().st_size > MAX_BRAND:
            return None
        data = real.read_bytes()
    except OSError:
        return None
    return data if data[:8] == PNG_MAGIC and data[12:16] == b"IHDR" else None


def write_token(state_dir, token):
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    path = state_dir / "token"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return path


def _write_private_json(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# --- pipeline session (chat) -----------------------------------------------------------------

class Chat:
    """One conversational pipeline at a time. States:
    idle -> planning -> proposed -> running | paused | question -> review -> merged -> idle.
    A message while a plan runs cancels the steps in flight (the work merged so far stays on
    the integration branch) and asks the planner again: that is how the user redirects."""
    COMMANDS = {"/approve": "approve", "/pause": "pause", "/resume": "resume", "/cancel": "cancel"}
    ACTIVE = ("running", "paused", "question")

    def __init__(self, server):
        self.srv = server
        self.state = "idle"
        self.transcript = []
        self.repo = self.backend = None
        self.parallel, self.max_iterations = 2, pipeline.DEFAULT_ITERATIONS
        self.roles, self.project_context = {}, ""
        self.plan = None            # (summary, [Step])
        self.engine = None
        self.loop = self.task = self.thread = None
        self.base = self.branch = None   # original base commit; latest integration branch
        self.result = self.error = None
        self.offset = 0
        self.history = []            # pipeline.state_summary() per finished run, for re-planning
        self.fingerprint = None      # the .uno/ snapshot the loaded roles came from
        self.overrides = {}          # {role: {model, effort}} for this session's runs only
        self.planner = None          # (model, effort) for the planner: settings, or this session's choice

    def say(self, who, text):
        self.transcript.append({"who": who, "text": str(text)[:4000], "ts": round(time.time(), 3)})

    def active(self):
        return self.state in self.ACTIVE

    # -- chat
    def message(self, body):
        text = body.get("message")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHAT:
            raise ValueError(f"message must be a non-empty string of at most {MAX_CHAT} characters")
        text = text.strip()
        cmd = self.COMMANDS.get(text.lower())
        if cmd:
            return getattr(self, cmd)()
        if self.state == "planning":
            return 409, {"error": "the orchestrator is still planning"}
        if self.state == "idle" or self.state == "merged":
            if self.srv.running():
                return 409, {"error": "a run is already in progress"}
            self._setup(body)
        elif self.active():
            self._cancel_engine("redirected by the user")
        self.say("you", text)
        self.state = "planning"
        threading.Thread(target=self._plan, args=(text,), daemon=True).start()
        return 200, {"state": self.state}

    def _setup(self, body):
        repo = validate_repo(body.get("repo"), self.srv.allowed_roots)
        worktree.preflight(repo)
        backend = body.get("backend", "mock")
        if backend not in BACKENDS:
            raise ValueError(f"backend must be one of {list(BACKENDS)}")
        parallel = body.get("parallel", 2)
        if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= orchestrator.MAX_PARALLEL:
            raise ValueError(f"parallel must be an integer 1..{orchestrator.MAX_PARALLEL}")
        iters = body.get("max_iterations", pipeline.DEFAULT_ITERATIONS)
        if not isinstance(iters, int) or isinstance(iters, bool) or not 1 <= iters <= pipeline.MAX_ITERATIONS:
            raise ValueError(f"max_iterations must be an integer 1..{pipeline.MAX_ITERATIONS}")
        # Roles, context and fingerprint come from one snapshot of .uno/: what was
        # hashed is what the agents get. Checked again at approve(), right before the run.
        roles, context, fp = roles_mod.load_confirmed(repo, self.srv.state_dir)
        if not roles:
            raise ValueError("the repository has no roles: install the templates or create one first")
        overrides = pipeline.check_overrides(body.get("overrides"), roles)
        cfg = config.load(self.srv.state_dir)
        planner = body.get("planner") or {}
        if not isinstance(planner, dict) or set(planner) - {"model", "effort"}:
            raise ValueError("planner: only 'model' and 'effort' are allowed")
        planner = (planner.get("model", cfg["planner_model"]), planner.get("effort", cfg["planner_effort"]))
        models.check(*planner, override=True)   # chosen for this request only; config.py never accepts fable
        self.__init__(self.srv)
        self.repo, self.backend, self.parallel, self.max_iterations = repo, backend, parallel, iters
        self.roles, self.project_context, self.fingerprint = roles, context, fp
        self.overrides, self.planner = overrides, planner
        self.srv.remember_repo(repo)

    def _plan(self, request):
        history = list(self.history)
        rejected = None
        for _ in range(2):  # one retry with the validator's explanation
            try:
                raw = pipeline.propose(request, self.roles, self.project_context, self.backend,
                                       self.transcript[:-1], history, rejected, cwd=str(self.srv.state_dir),
                                       auth=config.auth_mode(self.srv.state_dir),
                                       model=self.planner[0], effort=self.planner[1])
                summary, steps = pipeline.validate_plan(raw, self.roles)
                for st in steps:   # effective model/effort, shown in the plan before approval
                    o = self.overrides.get(st.role, {})
                    st.model, st.effort = o.get("model", self.roles[st.role].model), o.get("effort", self.roles[st.role].effort)
            except pipeline.PlanError as exc:
                rejected = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - shown in the chat
                rejected = f"{type(exc).__name__}: {exc}"
                break
            self.plan = (summary, steps)
            lines = [summary] + [f"{i}. [{s.role}] {s.id}: {s.goal} (paths {', '.join(s.paths)}"
                                 + (f"; after {', '.join(s.depends_on)}" if s.depends_on else "") + ")"
                                 for i, s in enumerate(steps, 1)]
            self.say("orchestrator", "\n".join(lines) + "\n\nApprove to run this plan, or tell me what to change.")
            self.state = "proposed"
            return
        self.say("orchestrator", f"I could not produce a valid plan: {rejected}\nRephrase the request or adjust the roles.")
        self.state = "review" if self.branch else "idle"

    # -- controls
    def approve(self, body=None):
        if self.state != "proposed":
            return 409, {"error": "there is no plan waiting for approval"}
        if self.srv.running():
            return 409, {"error": "a run is already in progress"}
        special = pipeline.uses_override_only(self.plan[1])
        if special and not ((body or {}).get("confirm_override_only") is True):
            return 409, {"error": "this plan uses Fable (billed outside your normal limit): confirm it explicitly",
                         "code": "confirm_override_only", "steps": special}
        # .uno/ may have changed on disk since the roles were loaded: the run uses the
        # loaded roles, so it must still be exactly the confirmed snapshot.
        if (roles_mod.fingerprint(self.repo) != self.fingerprint
                or not roles_mod.trusted(self.repo, self.srv.state_dir)):
            self.plan, self.state = None, ("review" if self.branch else "idle")
            self.say("orchestrator", "Plan discarded: " + roles_mod.UNCONFIRMED)
            return 409, {"error": roles_mod.UNCONFIRMED, "state": self.state}
        summary, steps = self.plan
        self.offset = self.srv.events_path.stat().st_size if self.srv.events_path.exists() else 0
        self.engine = pipeline.Pipeline(
            self.repo, self.roles, steps, EventBus(self.srv.events_path), self.backend,
            max_parallel=self.parallel, max_iterations=self.max_iterations,
            timeout=self.srv.agent_timeout, state_dir=self.srv.state_dir,
            project_context=self.project_context, base_ref=self.branch or "HEAD", base=self.base,
            on_question=lambda q: self._question(q), overrides=self.overrides)
        self.loop = asyncio.new_event_loop()
        self.task = self.loop.create_task(self.engine.run())
        self.state = "running"
        self.say("orchestrator", f"Plan approved: running {len(steps)} steps.")
        self.thread = threading.Thread(target=self._run_thread, daemon=True)
        self.thread.start()
        return 200, {"state": self.state, "offset": self.offset}

    def _question(self, text):
        self.state = "question"
        self.say("orchestrator", text)

    def _run_thread(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.result = self.loop.run_until_complete(self.task)
        except Exception as exc:  # noqa: BLE001 - surfaced in the chat
            self.error = f"{type(exc).__name__}: {exc}"
            self.result = self.engine.result()
        finally:
            self.loop.close()
            self.base = self.engine.base
            if self.engine.branch:
                self.branch = self.engine.branch
            self.error = self.error or self.result.get("error")
            self._finish()

    def _finish(self):
        r = self.result
        statuses = ", ".join(f"{s['id']}: {s['status']}" for s in r["steps"])
        pending = r["pending_findings"]
        self.history.append(pipeline.state_summary(dict(r, error=self.error)))
        msg = f"Run finished: {statuses}."
        if self.error:
            msg += f" {self.error}."
        if pending:
            msg += f" {len(pending)} findings pending ({len(pipeline.severe(pending))} medium/high)."
        msg += " Review the consolidated diff below; nothing reaches your branch until you merge."
        self.say("orchestrator", msg)
        self.state = "review"

    def _control(self, method, want):
        if self.state not in want or not self.engine or not self.loop:
            return 409, {"error": f"cannot {method} in state {self.state}"}
        self.loop.call_soon_threadsafe(getattr(self.engine, method))
        self.state = {"pause": "paused", "resume": "running"}[method]
        self.say("you", f"/{method}")
        return 200, {"state": self.state}

    def pause(self):
        return self._control("pause", ("running",))

    def resume(self):
        return self._control("resume", ("paused", "question"))

    def _cancel_engine(self, why):
        self.loop.call_soon_threadsafe(self.engine.cancel)
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(30)
        self.say("orchestrator", f"Cancelled ({why}); the steps merged so far stay on {self.branch or 'the integration branch'}.")

    def cancel(self):
        if self.state == "proposed":
            self.plan, self.state = None, ("review" if self.branch else "idle")
            self.say("you", "/cancel")
            self.say("orchestrator", "Plan discarded.")
            return 200, {"state": self.state}
        if not self.active():
            return 409, {"error": f"cannot cancel in state {self.state}"}
        self.say("you", "/cancel")
        self._cancel_engine("by the user")
        return 200, {"state": self.state}

    def merge(self):
        """The only way to the user's branch: explicit approval, in the review state."""
        if self.state != "review" or not self.branch:
            return 409, {"error": "there is nothing approved to merge"}
        ok, out = pipeline.final_merge(self.repo, self.branch)
        if not ok:
            self.say("orchestrator", f"Merge failed and was aborted: {out[:300]}")
            return 409, {"error": "merge conflict; aborted", "detail": out[:300]}
        self.state = "merged"
        self.say("orchestrator", f"Merged {self.branch} into your branch.")
        return 200, {"state": self.state, "message": out[:300]}

    def discard(self):
        if self.state not in ("review", "merged"):
            return 409, {"error": f"cannot discard in state {self.state}"}
        branch = self.branch
        self.__init__(self.srv)
        self.say("orchestrator", f"Session closed. Branch {branch} is kept for inspection.")
        return 200, {"state": self.state}

    def snapshot(self):
        plan = None
        if self.plan:
            steps = self.engine.steps if self.engine else self.plan[1]
            plan = {"summary": self.plan[0], "steps": [s.snapshot() for s in steps]}
        out = {"state": self.state, "transcript": self.transcript, "plan": plan,
               "repo": str(self.repo) if self.repo else None, "backend": self.backend,
               "branch": self.branch, "offset": self.offset, "error": self.error,
               "max_iterations": self.max_iterations, "overrides": self.overrides,
               "planner": {"model": self.planner[0], "effort": self.planner[1]} if self.planner else None,
               "limits": {"steps": pipeline.MAX_STEPS, "iterations": pipeline.MAX_ITERATIONS,
                          "duration": pipeline.MAX_DURATION}}
        if self.engine:
            out["iterations"] = self.engine.iterations
        if self.plan:
            out["override_only_steps"] = pipeline.uses_override_only(self.engine.steps if self.engine else self.plan[1])
            out["planner_override_only"] = models.is_override_only(self.planner[0]) if self.planner else False
        if self.state in ("review", "merged") and self.branch:
            out["diff"] = pipeline.consolidated_diff(self.repo, self.base, self.branch, MAX_DIFF)
            out["pending_findings"] = (self.result or {}).get("pending_findings", [])
            out["commands"] = [f"git -C {self.repo} diff {self.base[:12]} {self.branch}",
                               f"git -C {self.repo} merge --no-ff {self.branch}"]
        return out


# --- server ----------------------------------------------------------------------------------

class UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host, port, token, state_dir, allowed_roots, idle_timeout=7200, agent_timeout=900):
        if host not in LOOPBACK:
            raise ValueError(f"refusing to bind to {host!r}: the UI is loopback-only")
        super().__init__((host, port), Handler)
        self.token = token
        self.state_dir = Path(state_dir)
        self.allowed_roots = [Path(r).expanduser().resolve() for r in allowed_roots]
        self.idle_timeout = idle_timeout
        self.agent_timeout = agent_timeout
        self.last_activity = time.monotonic()
        self.lock = threading.Lock()
        self.run = None  # the current or last run
        self.chat = Chat(self)
        self.events_path = self.state_dir / "events.jsonl"
        for d in (self.state_dir, self.state_dir / "runs"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        threading.Thread(target=self._idle_watch, daemon=True).start()

    # -- state helpers
    def running(self):
        return (self.run is not None and not self.run.get("finished")) or self.chat.active()

    def _idle_watch(self):
        while True:
            time.sleep(min(5.0, max(0.1, self.idle_timeout / 4)))
            if not self.running() and time.monotonic() - self.last_activity > self.idle_timeout:
                self.shutdown()
                return

    def recent_repos(self):
        try:
            return json.loads((self.state_dir / "recent-repos.json").read_text())
        except (OSError, ValueError):
            return []

    def configured(self):
        """Recent repositories that have a .uno/ directory (the project selector)."""
        return [r for r in self.recent_repos() if (Path(r) / roles_mod.DIR).is_dir()]

    def brand(self):
        return all(brand_asset(self.state_dir, route) is not None for route in BRAND)

    def remember_repo(self, repo):
        recent = [str(repo)] + [r for r in self.recent_repos() if r != str(repo)]
        _write_private_json(self.state_dir / "recent-repos.json", recent[:RECENT_MAX])

    def templates(self):
        return sorted(p.stem for p in (self.state_dir / "runs").glob("*.json"))

    # -- checks shared by /api/check and /api/start
    def check(self, body):
        errors, warnings = [], []
        info = {"branch": None, "on_main": False, "steering": []}
        repo = None
        try:
            repo = validate_repo(body.get("repo"), self.allowed_roots)
        except ValueError as exc:
            errors.append(str(exc))
        if repo is not None:
            ref = worktree.git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
            info["branch"] = ref or None
            info["on_main"] = ref in ("main", "master")
            try:
                worktree.preflight(repo)
            except RuntimeError as exc:
                errors.append(str(exc))
            info["steering"] = worktree.agent_instruction_files(repo)
            if not roles_mod.trusted(repo, self.state_dir):
                errors.append(roles_mod.UNCONFIRMED)  # evaluated again inside start(), under the lock
        backend = body.get("backend", "mock")
        if backend not in BACKENDS:
            errors.append(f"backend must be one of {list(BACKENDS)}")
        parallel = body.get("parallel", 2)
        if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= orchestrator.MAX_PARALLEL:
            errors.append(f"parallel must be an integer 1..{orchestrator.MAX_PARALLEL}")
        tasks = None
        try:
            tasks = parse_tasks(body.get("tasks"))
            if len(tasks) > orchestrator.MAX_TASKS:
                errors.append(f"at most {orchestrator.MAX_TASKS} tasks per run")
            warnings += advise(tasks, backend)
        except ValueError as exc:
            errors.append(str(exc))
        if self.running():
            errors.append("a run is already in progress")
        result = {"ok": not errors, "errors": errors, "warnings": warnings, **info}
        return result, repo, backend, parallel, tasks

    # -- run lifecycle
    def start(self, body):
        with self.lock:
            if self.running():
                return 409, {"error": "a run is already in progress"}
            result, repo, backend, parallel, tasks = self.check(body)
            if not result["ok"]:
                return 400, result
            offset = self.events_path.stat().st_size if self.events_path.exists() else 0
            loop = asyncio.new_event_loop()
            run = {
                "repo": repo, "branch": result["branch"], "base": worktree.head(repo),
                "backend": backend, "parallel": parallel, "tasks": tasks,
                "steering": result["steering"], "offset": offset, "loop": loop,
                "summary": None, "error": None, "results": None, "finished": False,
                "started": time.time(),
            }
            run["task"] = loop.create_task(orchestrator.run(
                repo, tasks, EventBus(self.events_path), backend, parallel,
                timeout=self.agent_timeout, state_dir=self.state_dir))
            run["thread"] = threading.Thread(target=self._run_thread, args=(run,), daemon=True)
            self.run = run
            self.remember_repo(repo)
            run["thread"].start()
        return 200, {"started": True, "offset": offset, "warnings": result["warnings"]}

    def _run_thread(self, run):
        loop = run["loop"]
        asyncio.set_event_loop(loop)
        try:
            run["summary"] = loop.run_until_complete(run["task"])
        except asyncio.CancelledError:
            run["error"] = "stopped by the user"
        except Exception as exc:  # noqa: BLE001 - surfaced in the results view
            run["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            loop.close()
            try:
                run["results"] = self._collect(run)
            finally:
                run["finished"] = True

    def stop(self):
        with self.lock:
            if not self.running():
                return 409, {"error": "no run in progress"}
            self.run["loop"].call_soon_threadsafe(self.run["task"].cancel)
        return 200, {"stopping": True}

    def _events_since(self, offset):
        if not self.events_path.exists():
            return []
        with self.events_path.open("rb") as f:
            f.seek(offset)
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _collect(self, run):
        repo = run["repo"]
        events = self._events_since(run["offset"])
        run_id = next((e.get("run_id") for e in events if e.get("kind") == "run_start"), None)
        agents = []
        for t in run["tasks"]:
            verdict = (run["summary"] or {}).get(t.name, "failed" if run["error"] else "unknown")
            gate = next((e for e in reversed(events) if e.get("target") == t.name
                         and e["kind"] in ("merge", "conflict", "out_of_scope", "needs_review")), None)
            failed = next((e for e in reversed(events) if e.get("source") == t.name and e["kind"] == "failed"), None)
            detail = (failed or gate or {}).get("message", "")
            ended = next((e for e in reversed(events) if e.get("source") == t.name and e["kind"] in ("done", "failed")), {})
            usage = {k: ended.get(k) for k in ("model", "effort", "model_used", "turns", "seconds")}
            usage["model"], usage["effort"] = t.model, t.effort
            usage["override_only"] = models.is_override_only(t.model)
            branch = f"agent/{run_id}/{t.name}" if run_id else None
            diff, commands = "", []
            if branch and worktree.git(repo, "rev-parse", "--verify", "-q", branch, check=False).returncode == 0:
                diff = worktree.git(repo, "diff", run["base"], branch).stdout[:MAX_DIFF]
                if verdict != "merged":
                    # Fail closed: the UI never merges these. Review and merge by hand.
                    commands = [f"git -C {repo} diff {run['base'][:12]} {branch}",
                                f"git -C {repo} merge --no-ff {branch}"]
            agents.append({"name": t.name, "verdict": verdict, "detail": detail, "paths": t.paths,
                           "preset": t.preset, "branch": branch, "diff": diff, "commands": commands,
                           "usage": usage})
        return {"run_id": run_id, "repo": str(repo), "branch": run["branch"], "backend": run["backend"],
                "base": run["base"], "error": run["error"], "summary": run["summary"],
                "steering": run["steering"], "agents": agents,
                "seconds": round(time.time() - run["started"], 1)}

    # -- templates
    def save_template(self, body):
        name = body.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ValueError("invalid template name: use a-z, 0-9 and '-'")
        tasks = parse_tasks(body.get("tasks"))
        for t in tasks:
            models.check(t.model, t.effort)   # a saved template is not "one run": override-only models are refused
        backend = body.get("backend", "mock")
        if backend not in BACKENDS:
            raise ValueError("invalid backend")
        data = {"repo": str(body.get("repo", ""))[:1024], "backend": backend,
                "parallel": body.get("parallel", 2), "tasks": [asdict(t) for t in tasks]}
        _write_private_json(self.state_dir / "runs" / f"{name}.json", data)
        return {"saved": name, "templates": self.templates()}

    def load_template(self, name):
        if not NAME_RE.match(name):
            raise ValueError("invalid template name")
        path = self.state_dir / "runs" / f"{name}.json"
        if not path.is_file():
            raise ValueError("no such template")
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = parse_tasks(data.get("tasks"))  # a tampered file fails the same schema
        return {"repo": str(data.get("repo", "")), "backend": data.get("backend", "mock"),
                "parallel": data.get("parallel", 2), "tasks": [asdict(t) for t in tasks]}

    # -- roles and project context (.uno/ in the repository)
    def roles_view(self, repo_raw):
        repo = validate_repo(repo_raw, self.allowed_roots)
        error, roles = None, {}
        try:
            roles = roles_mod.load_roles(repo)
            context = roles_mod.project_context(repo)
            files = roles_mod.snapshot(repo)
            protected = roles_mod.project_protected_from(files)
            truncated = roles_mod.project_truncated_from(files)
            references = [{"path": r, "size": len(files[f"../{r}"])} for r in roles_mod.project_references_from(files, repo)]
        except ValueError as exc:
            error, context, protected, truncated, references = str(exc), "", [], {}, []
        if error is None and roles_mod.has_legacy_dir(repo):
            error = (f"this repository has a pre-0.1.0 {roles_mod.LEGACY_DIR}/: run `uno init` inside it "
                     f"to rename it to {roles_mod.DIR}/ (roles are read from {roles_mod.DIR}/ only)")
        ref = worktree.git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        try:
            worktree.preflight(repo)
            preflight = None
        except RuntimeError as exc:
            preflight = str(exc)
        return {"roles": [asdict(r) for r in roles.values()], "project_context": context,
                "library": [{"name": n, "builtin": b, "description": r.description, "preset": r.preset}
                            for n, (r, b) in roles_mod.library(self.state_dir).items()],
                "max_roles": roles_mod.MAX_ROLES, "branch": ref or None, "on_main": ref in ("main", "master"), "preflight": preflight,
                "protected_paths": protected, "truncated": truncated, "references": references, "error": error, "presets": sorted(PRESETS), "outputs": list(OUTPUTS),
                "templates": [t.name for t in roles_mod.TEMPLATES if t.name not in roles],
                "uncommitted": roles_mod.uncommitted(repo, worktree.git),
                "trusted": roles_mod.trusted(repo, self.state_dir)}

    def _edited(self, repo):
        """What the user writes from the UI is what they confirmed."""
        roles_mod.trust(repo, self.state_dir)
        return self.roles_view(str(repo))

    def save_role(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        role = roles_mod.role_from_json(body.get("role"))
        roles_mod.save_role(repo, role)
        return self._edited(repo)

    def _role_in_use(self, repo, name):
        """A role named in the chat's pending or running plan cannot be renamed or deleted
        until that plan finishes or is cancelled."""
        chat = self.chat
        if chat.repo != repo or not chat.plan or chat.state not in ("proposed", "planning", *chat.ACTIVE):
            return
        steps = chat.engine.steps if chat.engine else chat.plan[1]
        if any(s.role == name for s in steps):
            raise ValueError(f"role {name!r} is used by the plan that is {chat.state}: "
                             "wait for it to finish or cancel it first")

    def delete_role(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        name = str(body.get("name", ""))
        self._role_in_use(repo, name)
        roles_mod.delete_role(repo, name)
        return self._edited(repo)

    def rename_role(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        old, new = str(body.get("name", "")), str(body.get("new_name", ""))
        self._role_in_use(repo, old)
        roles_mod.rename_role(repo, old, new)
        return self._edited(repo)

    def role_from_template(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        name = body.get("name")
        role = roles_mod.role_from_template(self.state_dir, str(body.get("template", "")),
                                            str(name) if name else None)
        if (roles_mod.roles_dir(repo) / f"{role.name}.json").exists():
            raise ValueError(f"a role named {role.name!r} already exists: give it another name")
        roles_mod.save_role(repo, role)
        return self._edited(repo)

    # -- personal template library (state dir, outside any repository)
    def role_templates_view(self):
        return {"library": [dict(asdict(r), builtin=b) for r, b in roles_mod.library(self.state_dir).values()]}

    def save_role_template(self, body):
        roles_mod.save_template(self.state_dir, roles_mod.role_from_json(body.get("role")))
        return self.role_templates_view()

    def delete_role_template(self, body):
        roles_mod.delete_template(self.state_dir, str(body.get("name", "")))
        return self.role_templates_view()

    def install_templates(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        roles_mod.install_templates(repo)
        return self._edited(repo)

    def save_project(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        roles_mod.save_project_context(repo, body.get("context"))
        return self._edited(repo)

    def trust_roles(self, body):
        """The user has reviewed .uno/ as shown in the UI and confirms it."""
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        roles_mod.load_roles(repo)       # never confirm something that does not validate
        roles_mod.project_context(repo)
        files = roles_mod.snapshot(repo)
        roles_mod.project_protected_from(files)
        roles_mod.project_truncated_from(files)
        roles_mod.project_references_from(files, repo)
        return self._edited(repo)

    def commit_roles(self, body):
        """Commit .uno/ only (roles must be committed: the run preflight needs a clean tree)."""
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        worktree.git(repo, "add", "--", roles_mod.DIR)
        r = worktree.git(repo, *worktree.IDENT, "commit", "-q", "-m", "uno: roles and project context",
                         "--", roles_mod.DIR, check=False)
        if r.returncode != 0:
            raise ValueError(f"git commit failed: {(r.stdout + r.stderr).strip()[:200]}")
        return self.roles_view(str(repo))

    def create_branch(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        if worktree.git(repo, "status", "--porcelain").stdout.strip():
            raise ValueError("the repository has uncommitted changes: commit or stash them first")
        name = body.get("name") or datetime.datetime.now().strftime("uno-run-%Y%m%d-%H%M")
        if not isinstance(name, str) or not BRANCH_RE.match(name) or name in ("main", "master"):
            raise ValueError("invalid branch name")
        r = worktree.git(repo, "checkout", "-b", name, check=False)
        if r.returncode != 0:
            raise ValueError(f"git checkout -b failed: {r.stderr.strip()[:200]}")
        return {"branch": name}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    # -- responses
    def _send(self, status, ctype, body=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if body is not None:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body is not None and self.command != "HEAD":  # HEAD: same headers, no body
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, "application/json", json.dumps(obj).encode("utf-8"))

    def _error(self, status, message):
        self._json(status, {"error": message})

    def send_error(self, code, message=None, explain=None):
        """Route the base class's own errors (501, malformed requests...) through _send,
        so every response carries the security headers."""
        self._json(code, {"error": message or self.responses.get(code, ("error",))[0]})

    # -- gates applied to every request
    def _origin_ok(self):
        port = self.server.server_port
        if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return self.command in ("GET", "HEAD")  # browsers always send Origin on fetch POST
        return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _token_ok(self):
        given = self.headers.get(TOKEN_HEADER, "")
        return hmac.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8"))

    def _gate(self, path):
        """Return False after sending an error when the request must not proceed."""
        self.server.last_activity = time.monotonic()
        if not self._origin_ok():
            self._error(403, "bad Host or Origin")
            return False
        if path.startswith("/api/") and not self._token_ok():
            self._error(401, "missing or invalid token")
            return False
        return True

    # -- routing
    def do_GET(self):
        path = urlsplit(self.path).path
        if not self._gate(path):
            return
        if path in STATIC:
            name, ctype = STATIC[path]
            return self._send(200, ctype, resources.files(__package__).joinpath(name).read_bytes())
        srv = self.server
        if path in BRAND:
            data = brand_asset(srv.state_dir, path)
            return self._send(200, "image/png", data) if data is not None else self._error(404, "not found")
        if path == "/api/status":
            run = srv.run
            return self._json(200, {
                "running": srv.running(), "offset": run["offset"] if run else 0,
                "has_results": bool(run and run.get("results")),
                "recent_repos": srv.recent_repos(), "configured": srv.configured(), "templates": srv.templates(),
                "brand": srv.brand(), "version": __version__,
                "presets": sorted(PRESETS), "limits": {"parallel": orchestrator.MAX_PARALLEL,
                                                        "tasks": orchestrator.MAX_TASKS},
                "idle_timeout": srv.idle_timeout, "state_dir": str(srv.state_dir),
                "auth": config.auth_mode(srv.state_dir), "models": models.catalog(),
                "planner": {"model": config.load(srv.state_dir)["planner_model"],
                            "effort": config.load(srv.state_dir)["planner_effort"]}})
        if path == "/api/results":
            run = srv.run
            if not run or not run.get("results"):
                return self._error(404, "no results yet")
            return self._json(200, run["results"])
        if path == "/api/events":
            return self._stream()
        if path == "/api/templates":
            return self._json(200, {"templates": srv.templates()})
        if path == "/api/pipeline":
            return self._json(200, srv.chat.snapshot())
        if path == "/api/roles":
            try:
                repo = parse_qs(urlsplit(self.path).query).get("repo", [""])[0]
                return self._json(200, srv.roles_view(repo))
            except ValueError as exc:
                return self._error(400, str(exc))
        if path == "/api/role-templates":
            return self._json(200, srv.role_templates_view())
        if path.startswith("/api/templates/"):
            try:
                return self._json(200, srv.load_template(path[len("/api/templates/"):]))
            except ValueError as exc:
                return self._error(400, str(exc))
        self._error(404, "not found")

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self._gate(path):
            return
        if not path.startswith("/api/"):
            return self._error(404, "not found")
        ctype = self.headers.get("Content-Type", "")
        if not ctype.split(";")[0].strip().lower() == "application/json":
            return self._error(415, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "bad Content-Length")
        if length > MAX_BODY:
            return self._error(413, f"body larger than {MAX_BODY} bytes")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return self._error(400, "body is not valid JSON")
        if not isinstance(body, dict):
            return self._error(400, "body must be a JSON object")
        srv = self.server
        try:
            if path == "/api/check":
                return self._json(200, srv.check(body)[0])
            if path == "/api/start":
                return self._json(*srv.start(body))
            if path == "/api/stop":
                return self._json(*srv.stop())
            if path == "/api/branch":
                return self._json(200, srv.create_branch(body))
            if path == "/api/templates":
                return self._json(200, srv.save_template(body))
            if path == "/api/roles":
                return self._json(200, srv.save_role(body))
            if path == "/api/roles/delete":
                return self._json(200, srv.delete_role(body))
            if path == "/api/roles/rename":
                return self._json(200, srv.rename_role(body))
            if path == "/api/roles/from-template":
                return self._json(200, srv.role_from_template(body))
            if path == "/api/role-templates":
                return self._json(200, srv.save_role_template(body))
            if path == "/api/role-templates/delete":
                return self._json(200, srv.delete_role_template(body))
            if path == "/api/roles/templates":
                return self._json(200, srv.install_templates(body))
            if path == "/api/roles/commit":
                return self._json(200, srv.commit_roles(body))
            if path == "/api/roles/trust":
                return self._json(200, srv.trust_roles(body))
            if path == "/api/project":
                return self._json(200, srv.save_project(body))
            if path == "/api/chat":
                with srv.lock:
                    return self._json(*srv.chat.message(body))
            if path.startswith("/api/pipeline/"):
                action = path[len("/api/pipeline/"):]
                if action == "approve":
                    with srv.lock:
                        return self._json(*srv.chat.approve(body))
                if action in ("pause", "resume", "cancel", "merge", "discard"):
                    with srv.lock:
                        return self._json(*getattr(srv.chat, action)())
        except (ValueError, RuntimeError) as exc:
            return self._error(400, str(exc))
        self._error(404, "not found")

    def _stream(self):
        """NDJSON stream of events from ?from=<offset> until the run ends."""
        srv = self.server
        try:
            pos = int(parse_qs(urlsplit(self.path).query).get("from", ["0"])[0])
        except ValueError:
            pos = 0
        self._send(200, "application/x-ndjson")
        if self.command == "HEAD":
            return
        try:
            while True:
                if srv.events_path.exists():
                    size = srv.events_path.stat().st_size
                    if size < pos:
                        pos = 0  # rotated
                    with srv.events_path.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                    if chunk:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        continue
                if not srv.running():
                    self.wfile.write(b'{"kind": "stream_end"}\n')
                    self.wfile.flush()
                    return
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass
