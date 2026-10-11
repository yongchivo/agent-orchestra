"""One test per control of the local web UI (ui/server.py). The server enforces every rule."""
import http.client
import json
import os
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from orchestra.worker import Worker
from ui.server import MAX_BODY, UIServer, write_token

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t0ken-for-tests-" + "x" * 27


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    git(path, "checkout", "-q", "-b", "work")
    return path


@pytest.fixture
def ui(tmp_path):
    srv = UIServer("127.0.0.1", 0, TOKEN, tmp_path / "state", [tmp_path], idle_timeout=3600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, token=TOKEN, headers=None, ctype="application/json", raw=None):
    """Return (status, parsed body, headers). Host/Origin default to what a browser would send."""
    port = srv.server_port
    h = {"Host": f"127.0.0.1:{port}"}
    if token is not None:
        h["X-Orchestra-Token"] = token
    data = raw
    if body is not None or raw is not None:
        h["Origin"] = f"http://127.0.0.1:{port}"
        h["Content-Type"] = ctype
        data = raw if raw is not None else json.dumps(body).encode()
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    text = r.read().decode()
    conn.close()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    return r.status, parsed, r.headers


def tasks_for(*names, paths="**"):
    return [{"name": n, "prompt": f"edit {n}.txt", "paths": [paths] if isinstance(paths, str) else paths,
             "preset": "edit", "max_turns": 5} for n in names]


def form(repo, *names, **kw):
    return {"repo": str(repo), "backend": "mock", "parallel": 2, "tasks": tasks_for(*names, **kw)}


def wait_results(srv, timeout=30):
    for _ in range(int(timeout / 0.1)):
        if srv.run and srv.run.get("finished"):
            return srv.run["results"]
        time.sleep(0.1)
    pytest.fail("run did not finish")


# --- token --------------------------------------------------------------------------------

def test_missing_token_is_rejected(ui):
    assert call(ui, "GET", "/api/status", token=None)[0] == 401


def test_wrong_token_is_rejected(ui):
    assert call(ui, "GET", "/api/status", token=TOKEN[:-1] + "y")[0] == 401
    assert call(ui, "POST", "/api/check", {}, token="")[0] == 401


def test_token_in_query_string_is_ignored(ui):
    assert call(ui, "GET", f"/api/status?token={TOKEN}", token=None)[0] == 401


def test_event_stream_without_token_is_rejected(ui):
    assert call(ui, "GET", "/api/events?from=0", token=None)[0] == 401


# --- origin / host / body ------------------------------------------------------------------

def test_forged_origin_is_rejected(ui):
    assert call(ui, "POST", "/api/check", {}, headers={"Origin": "http://evil.example"})[0] == 403
    assert call(ui, "POST", "/api/check", {}, headers={"Origin": f"http://127.0.0.1:{ui.server_port + 1}"})[0] == 403


def test_forged_host_is_rejected(ui):
    assert call(ui, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert call(ui, "GET", "/", headers={"Host": f"127.0.0.1.evil.example:{ui.server_port}"})[0] == 403


def test_post_without_json_content_type_is_rejected(ui):
    assert call(ui, "POST", "/api/check", raw=b"{}", ctype="text/plain")[0] == 415
    assert call(ui, "POST", "/api/check", raw=b"a=1", ctype="application/x-www-form-urlencoded")[0] == 415


def test_oversized_body_is_rejected(ui):
    big = b'{"repo": "' + b"a" * MAX_BODY + b'"}'
    assert call(ui, "POST", "/api/check", raw=big)[0] == 413


# --- repository and task validation --------------------------------------------------------

def test_repo_outside_allowed_roots_is_rejected(ui, tmp_path):
    status, body, _ = call(ui, "POST", "/api/check", {"repo": "/", "tasks": tasks_for("a")})
    assert status == 200 and not body["ok"] and any("outside the allowed" in e for e in body["errors"])
    assert call(ui, "POST", "/api/start", {"repo": "/", "tasks": tasks_for("a")})[0] == 400


def test_symlink_escaping_the_roots_is_rejected(ui, tmp_path):
    (tmp_path / "escape").symlink_to("/")
    _, body, _ = call(ui, "POST", "/api/check", {"repo": str(tmp_path / "escape"), "tasks": tasks_for("a")})
    assert any("outside the allowed" in e for e in body["errors"])


def test_invalid_preset_is_rejected(ui, repo):
    f = form(repo, "a")
    f["tasks"][0]["preset"] = "shell"
    _, body, _ = call(ui, "POST", "/api/check", f)
    assert any("preset" in e for e in body["errors"])
    assert call(ui, "POST", "/api/start", f)[0] == 400
    f["tasks"][0] = {"name": "a", "prompt": "x", "paths": ["**"], "allowed_tools": ["Bash"]}
    _, body, _ = call(ui, "POST", "/api/check", f)
    assert any("unknown fields" in e for e in body["errors"])


def test_task_without_paths_is_rejected(ui, repo):
    f = form(repo, "a")
    del f["tasks"][0]["paths"]
    _, body, _ = call(ui, "POST", "/api/check", f)
    assert any("'paths' is required" in e for e in body["errors"])
    f["tasks"][0]["paths"] = []
    _, body, _ = call(ui, "POST", "/api/check", f)
    assert any("paths" in e for e in body["errors"])


def test_template_name_traversal_is_rejected(ui, repo):
    for name in ("../evil", "..", "a/b", "evil.json"):
        status, body, _ = call(ui, "POST", "/api/templates", {"name": name, **form(repo, "a")})
        assert status == 400 and "template name" in body["error"], name
    assert call(ui, "GET", "/api/templates/../evil")[0] == 400
    assert call(ui, "GET", "/api/templates/..%2Fevil")[0] == 400
    assert not list((ui.state_dir).glob("**/evil*"))


def test_templates_roundtrip_and_recent_repos(ui, repo):
    status, body, _ = call(ui, "POST", "/api/templates", {"name": "docs", **form(repo, "a", "b", paths="a.txt")})
    assert status == 200 and body["templates"] == ["docs"]
    assert stat.S_IMODE((ui.state_dir / "runs" / "docs.json").stat().st_mode) == 0o600
    _, loaded, _ = call(ui, "GET", "/api/templates/docs")
    assert [t["name"] for t in loaded["tasks"]] == ["a", "b"] and loaded["tasks"][0]["paths"] == ["a.txt"]
    # A tampered template fails the same schema on load.
    (ui.state_dir / "runs" / "docs.json").write_text('{"tasks": [{"name": "a", "prompt": "p", "paths": "a.txt"}]}')
    assert call(ui, "GET", "/api/templates/docs")[0] == 400
    assert call(ui, "POST", "/api/start", form(repo, "a"))[0] == 200
    wait_results(ui)
    _, status_body, _ = call(ui, "GET", "/api/status")
    assert status_body["recent_repos"] == [str(repo.resolve())]


# --- start rules enforced by the server ----------------------------------------------------

def test_dirty_tree_blocks_start(ui, repo):
    (repo / "dirty.txt").write_text("x")
    _, body, _ = call(ui, "POST", "/api/check", form(repo, "a"))
    assert not body["ok"] and any("uncommitted" in e for e in body["errors"])
    assert call(ui, "POST", "/api/start", form(repo, "a"))[0] == 400


def test_main_branch_blocks_start_until_a_branch_is_created(ui, repo):
    git(repo, "checkout", "-q", "main")
    _, body, _ = call(ui, "POST", "/api/check", form(repo, "a"))
    assert not body["ok"] and body["on_main"] and any("refusing to run on 'main'" in e for e in body["errors"])
    assert call(ui, "POST", "/api/start", form(repo, "a"))[0] == 400
    for bad in ("main", "../x", "-x"):
        assert call(ui, "POST", "/api/branch", {"repo": str(repo), "name": bad})[0] == 400
    status, created, _ = call(ui, "POST", "/api/branch", {"repo": str(repo)})
    assert status == 200 and created["branch"].startswith("orchestra-run-")
    assert git(repo, "symbolic-ref", "--short", "HEAD").strip() == created["branch"]
    _, body, _ = call(ui, "POST", "/api/check", form(repo, "a"))
    assert body["ok"] and not body["on_main"]


def test_second_simultaneous_run_is_refused(ui, repo, monkeypatch):
    async def slow(self):
        import asyncio
        await asyncio.sleep(1.5)
        (self.workdir / "a.txt").write_text("x")
    monkeypatch.setattr(Worker, "_run_mock", slow)
    assert call(ui, "POST", "/api/start", form(repo, "a"))[0] == 200
    assert call(ui, "POST", "/api/start", form(repo, "b"))[0] == 409
    _, body, _ = call(ui, "POST", "/api/check", form(repo, "b"))
    assert any("already in progress" in e for e in body["errors"])
    wait_results(ui)


def test_advice_warnings(ui, repo):
    f = {"repo": str(repo), "backend": "claude", "parallel": 2, "tasks": [
        {"name": "a", "prompt": "document calc.py", "paths": ["src/**"], "max_turns": 25},
        {"name": "b", "prompt": "do things", "paths": ["src/x.py"]},
        {"name": "c", "prompt": "x", "paths": ["**"]}, {"name": "d", "prompt": "d.txt", "paths": ["d.txt"]}]}
    _, body, _ = call(ui, "POST", "/api/check", f)
    w = " | ".join(body["warnings"])
    assert "'a' and 'b' have overlapping paths" in w
    assert "'b': the prompt does not mention any of its paths" in w
    assert "'c': its paths match everything" in w
    assert "more than 3 agents" in w and "max_turns above 20" in w
    assert body["ok"]  # warnings never block


def test_steering_files_are_reported(ui, repo):
    (repo / "CLAUDE.md").write_text("x")
    git(repo, "add", "CLAUDE.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    _, body, _ = call(ui, "POST", "/api/check", form(repo, "a"))
    assert body["steering"] == ["CLAUDE.md"] and body["ok"]


# --- server hardening ----------------------------------------------------------------------

def test_non_loopback_host_is_refused(tmp_path):
    for host in ("0.0.0.0", "192.168.1.10", ""):
        with pytest.raises(ValueError, match="loopback"):
            UIServer(host, 0, TOKEN, tmp_path / "s", [tmp_path])


def test_token_file_is_private(tmp_path):
    path = write_token(tmp_path / "state", TOKEN)
    assert path.read_text() == TOKEN
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700


def test_security_headers_and_no_inline_script(ui):
    for path, token in (("/", None), ("/api/status", TOKEN)):
        status, _, h = call(ui, "GET", path, token=token)
        assert status == 200
        assert h["Content-Security-Policy"].startswith("default-src 'none'; script-src 'self'")
        assert h["X-Frame-Options"] == "DENY" and h["Referrer-Policy"] == "no-referrer"
        assert h["X-Content-Type-Options"] == "nosniff" and h["Cache-Control"] == "no-store"
    html = (ROOT / "ui" / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and " on[a-z]+=" not in html
    assert not re.search(r"\son\w+=", html)
    assert call(ui, "GET", "/server.py")[0] == 404 and call(ui, "GET", "/../main.py")[0] == 404


def test_idle_timeout_shuts_the_server_down(tmp_path):
    srv = UIServer("127.0.0.1", 0, TOKEN, tmp_path / "s", [tmp_path], idle_timeout=0.5)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()
    srv.server_close()


# --- full flow: run, stream, results, fail-closed review ------------------------------------

def test_full_mock_flow_with_out_of_scope_and_xss_diff(ui, repo, monkeypatch):
    payload = "<img src=x onerror=alert(1)>"

    async def write(self):
        (self.workdir / f"{self.name}.txt").write_text(f"{payload}\n")
    monkeypatch.setattr(Worker, "_run_mock", write)

    f = form(repo, "good", "bad", paths="good.txt")
    status, started, _ = call(ui, "POST", "/api/start", f)
    assert status == 200 and started["started"]

    # Stream NDJSON with the token in a header until the server ends it.
    port = ui.server_port
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", f"/api/events?from={started['offset']}",
                 headers={"Host": f"127.0.0.1:{port}", "X-Orchestra-Token": TOKEN})
    r = conn.getresponse()
    assert r.status == 200 and r.headers["Content-Type"] == "application/x-ndjson"
    kinds = []
    while True:
        line = r.readline()
        if not line:
            break
        e = json.loads(line)
        kinds.append(e["kind"])
        if e["kind"] == "stream_end":
            break
    conn.close()
    assert kinds[0] == "run_start" and "out_of_scope" in kinds and kinds[-2:] == ["run_end", "stream_end"]

    _, res, _ = call(ui, "GET", "/api/results")
    by = {a["name"]: a for a in res["agents"]}
    assert by["good"]["verdict"] == "merged" and by["good"]["commands"] == []
    assert by["bad"]["verdict"] == "out_of_scope" and "bad.txt" in by["bad"]["detail"]
    assert by["bad"]["commands"] == [f"git -C {repo.resolve()} diff {res['base'][:12]} {by['bad']['branch']}",
                                     f"git -C {repo.resolve()} merge --no-ff {by['bad']['branch']}"]
    assert not (repo / "bad.txt").exists() and (repo / "good.txt").exists()
    # The diff is returned as data and the page renders it with textContent only.
    assert f"+{payload}" in by["good"]["diff"] and f"+{payload}" in by["bad"]["diff"]
    js = (ROOT / "ui" / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in js
    assert stat.S_IMODE(ui.events_path.stat().st_mode) == 0o600


def test_stop_cancels_the_run_and_cleans_up(ui, repo, monkeypatch):
    async def forever(self):
        import asyncio
        await asyncio.sleep(60)
    monkeypatch.setattr(Worker, "_run_mock", forever)
    assert call(ui, "POST", "/api/start", form(repo, "a"))[0] == 200
    time.sleep(0.5)
    assert call(ui, "POST", "/api/stop", {})[0] == 200
    res = wait_results(ui, 15)
    assert res["error"] == "stopped by the user"
    assert git(repo, "worktree", "list").count("\n") == 1
    assert not list((ui.state_dir / "worktrees").iterdir())
    assert call(ui, "POST", "/api/stop", {})[0] == 409


SECURITY_HEADERS = ("Content-Security-Policy", "X-Frame-Options", "Referrer-Policy",
                    "X-Content-Type-Options", "Cache-Control")


def test_head_matches_get_without_a_body(ui):
    for path, token in (("/", None), ("/api/status", TOKEN), ("/app.js", None)):
        gs, gbody, gh = call(ui, "GET", path, token=token)
        port = ui.server_port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": f"127.0.0.1:{port}"}
        if token:
            h["X-Orchestra-Token"] = token
        conn.request("HEAD", path, headers=h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        assert gs == r.status == 200
        assert raw == b""
        assert r.headers["Content-Length"] == gh["Content-Length"]
        for name in SECURITY_HEADERS + ("Content-Type",):
            assert r.headers[name] == gh[name], name
    assert call(ui, "HEAD", "/api/status", token=None)[0] == 401
    assert call(ui, "HEAD", "/api/events?from=0")[0] == 200  # headers only, no endless stream


def test_every_error_response_carries_security_headers(ui):
    cases = [
        ("GET", "/api/status", dict(token=None)),                                   # 401
        ("POST", "/api/check", dict(body={}, headers={"Origin": "http://evil"})),   # 403
        ("GET", "/nope", {}),                                                        # 404
        ("POST", "/api/check", dict(raw=b"x" * (MAX_BODY + 1))),                    # 413
        ("POST", "/api/check", dict(raw=b"{}", ctype="text/plain")),                # 415
        ("PUT", "/api/status", dict(raw=b"{}")),                                     # 501 from the base class
        ("DELETE", "/", {}),                                                         # 501 from the base class
    ]
    seen = set()
    for method, path, kw in cases:
        status, body, h = call(ui, method, path, **kw)
        assert status >= 400 and isinstance(body, dict) and "error" in body, (method, path)
        for name in SECURITY_HEADERS:
            assert name in h, (method, path, name)
        assert h["Content-Security-Policy"].startswith("default-src 'none'")
        assert h["X-Frame-Options"] == "DENY" and h["Cache-Control"] == "no-store"
        seen.add(status)
    assert {401, 403, 404, 413, 415, 501} <= seen


def test_forged_origin_is_rejected_on_get_and_head_too(ui):
    for method in ("GET", "HEAD"):
        for path, token in (("/", None), ("/api/status", TOKEN), ("/api/events?from=0", TOKEN)):
            for origin in ("http://evil.example", "null", f"http://127.0.0.1:{ui.server_port + 1}",
                           f"https://127.0.0.1:{ui.server_port}"):
                status = call(ui, method, path, token=token, headers={"Origin": origin})[0]
                assert status == 403, (method, path, origin, status)
        # The genuine origin still works.
        assert call(ui, method, "/api/status", headers={"Origin": f"http://127.0.0.1:{ui.server_port}"})[0] == 200


def _fingerprint(ui):
    files = {}
    for p in sorted(ui.state_dir.rglob("*")):
        st = p.lstat()
        files[str(p)] = (st.st_mode, st.st_size, p.read_bytes() if p.is_file() else None)
    return files, ui.run, ui.recent_repos(), ui.templates()


def test_get_and_head_never_modify_state(ui, repo):
    # Seed some state through the legitimate POST paths first.
    assert call(ui, "POST", "/api/templates", {"name": "seed", **form(repo, "a")})[0] == 200
    head_before = git(repo, "rev-parse", "HEAD")
    branches_before = git(repo, "branch", "--list")
    before = _fingerprint(ui)

    action_paths = ["/api/start", "/api/stop", "/api/branch", "/api/check", "/api/templates",
                    f"/api/start?repo={repo}", f"/api/branch?repo={repo}&name=evil"]
    read_paths = ["/", "/app.js", "/style.css", "/api/status", "/api/results", "/api/templates",
                  "/api/templates/seed", "/api/templates/missing", "/api/events?from=0"]
    for method in ("GET", "HEAD"):
        for path in action_paths:
            status = call(ui, method, path)[0]
            # /api/templates is a legitimate read (list); every other action is unknown to GET/HEAD.
            assert status == (200 if path == "/api/templates" else 404), (method, path, status)
        for path in read_paths:
            assert call(ui, method, path)[0] in (200, 400, 404), (method, path)

    assert _fingerprint(ui) == before
    assert ui.run is None and not ui.running()
    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "branch", "--list") == branches_before
    assert git(repo, "status", "--porcelain") == ""
