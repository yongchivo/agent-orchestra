"""Local web UI for agent-orchestra: launch and review runs without the terminal.

Single user, loopback only. The server enforces every rule and the page only displays
them: a random per-start token in a header on every API call (never a cookie or query
string), Host and Origin validation, JSON-only POST bodies with a size cap, repositories
restricted to allowed roots, permission presets only, no inline scripts, and automatic
shutdown after a period of inactivity. The core (orchestra/) is invoked as it is; nothing
here relaxes a control.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from orchestra import orchestrator, scope, worktree
from orchestra.bus import EventBus
from orchestra.worker import NAME_RE, PRESETS, Task

HERE = Path(__file__).resolve().parent
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
TOKEN_HEADER = "X-Orchestra-Token"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 64 * 1024
MAX_DIFF = 200 * 1024
BACKENDS = ("mock", "claude")
TASK_FIELDS = {"name", "prompt", "paths", "preset", "max_turns"}
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
                          str(t.get("preset", "edit")), t.get("max_turns", 20)))
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
        self.events_path = self.state_dir / "events.jsonl"
        for d in (self.state_dir, self.state_dir / "runs"):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(d, 0o700)
        threading.Thread(target=self._idle_watch, daemon=True).start()

    # -- state helpers
    def running(self):
        return self.run is not None and not self.run.get("finished")

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
            branch = f"agent/{run_id}/{t.name}" if run_id else None
            diff, commands = "", []
            if branch and worktree.git(repo, "rev-parse", "--verify", "-q", branch, check=False).returncode == 0:
                diff = worktree.git(repo, "diff", run["base"], branch).stdout[:MAX_DIFF]
                if verdict != "merged":
                    # Fail closed: the UI never merges these. Review and merge by hand.
                    commands = [f"git -C {repo} diff {run['base'][:12]} {branch}",
                                f"git -C {repo} merge --no-ff {branch}"]
            agents.append({"name": t.name, "verdict": verdict, "detail": detail, "paths": t.paths,
                           "preset": t.preset, "branch": branch, "diff": diff, "commands": commands})
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

    def create_branch(self, body):
        repo = validate_repo(body.get("repo"), self.allowed_roots)
        if worktree.git(repo, "status", "--porcelain").stdout.strip():
            raise ValueError("the repository has uncommitted changes: commit or stash them first")
        name = body.get("name") or datetime.datetime.now().strftime("orchestra-run-%Y%m%d-%H%M")
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
            return self._send(200, ctype, (HERE / name).read_bytes())
        srv = self.server
        if path == "/api/status":
            run = srv.run
            return self._json(200, {
                "running": srv.running(), "offset": run["offset"] if run else 0,
                "has_results": bool(run and run.get("results")),
                "recent_repos": srv.recent_repos(), "templates": srv.templates(),
                "presets": sorted(PRESETS), "limits": {"parallel": orchestrator.MAX_PARALLEL,
                                                        "tasks": orchestrator.MAX_TASKS},
                "idle_timeout": srv.idle_timeout, "state_dir": str(srv.state_dir)})
        if path == "/api/results":
            run = srv.run
            if not run or not run.get("results"):
                return self._error(404, "no results yet")
            return self._json(200, run["results"])
        if path == "/api/events":
            return self._stream()
        if path == "/api/templates":
            return self._json(200, {"templates": srv.templates()})
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
        except ValueError as exc:
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
