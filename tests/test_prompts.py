"""Prompt Studio (prompts.py and /api/prompts/*): one test per control."""
import http.client
import json
import re
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from uno_orchestra import models, pipeline, prompts, roles as roles_mod
from uno_orchestra.ui.server import MAX_CHAT, UIServer

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t0ken-for-tests-" + "x" * 27
SECRET = "sk-abcdefghijklmnopqrstuvwxyz0123"
GH = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def commit(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "utils.py").write_text("def add(a, b):\n    return a + b\n")
    (path / "notes.txt").write_text("RAW FILE CONTENT MUST NOT REACH THE GENERATOR\n")
    commit(path, "init")
    git(path, "checkout", "-q", "-b", "work")
    roles_mod.install_templates(path)
    roles_mod.save_project_context(path, "A tiny Python library.")
    commit(path, "roles")
    return path


@pytest.fixture
def ui(tmp_path):
    srv = UIServer("127.0.0.1", 0, TOKEN, tmp_path / "state", [tmp_path], idle_timeout=3600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, token=TOKEN, headers=None):
    port = srv.server_port
    h = {"Host": f"127.0.0.1:{port}"}
    if token is not None:
        h["X-Orchestra-Token"] = token
    data = None
    if body is not None:
        h["Origin"] = f"http://127.0.0.1:{port}"
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    h.update(headers or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    text = r.read().decode(errors="replace")
    conn.close()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    return r.status, parsed, r.headers


def wait_state(srv, *states, timeout=20):
    for _ in range(int(timeout / 0.05)):
        if srv.chat.state in states:
            return srv.chat.state
        time.sleep(0.05)
    return srv.chat.state


def spy(monkeypatch):
    """Capture exactly what the model would receive; answer with the mock backend."""
    seen = []
    real = prompts.ask

    def fake(kind, system, prompt, schema, **kw):
        seen.append({"kind": kind, "system": system, "prompt": prompt, "schema": schema, "model": kw.get("model")})
        return real(kind, system, prompt, schema, **dict(kw, backend="mock"))
    monkeypatch.setattr(prompts, "ask", fake)
    return seen


def good_output(mode="create"):
    return prompts.mock_answer("generate", prompts.check_inputs({"idea": "x", "mode": mode, "prompt": "p", "changes": "c"}))


def perfect(lang="es", target="uno"):
    inp = prompts.check_inputs({"idea": "x", "target": target})
    return prompts.render(prompts.check_output(prompts.mock_answer("generate", inp), "create"), lang)


# --- schemas -----------------------------------------------------------------------------------

def test_clarification_schema_is_validated():
    ok = prompts.check_clarify({"questions": [{"question": "Which files?", "why": "scope"}]})
    assert ok == [{"question": "Which files?", "why": "scope"}]
    assert prompts.check_clarify({"questions": []}) == []
    for bad in ({"questions": [{"question": "q", "why": "w"}] * 6},               # more than 5
                {"questions": [{"question": "q"}]},                               # missing why
                {"questions": [{"question": "q", "why": "w", "tool": "Bash"}]},   # unknown field
                {"questions": [{"question": "", "why": "w"}]},                    # empty
                {"questions": [{"question": "q" * 301, "why": "w"}]},             # too long
                {"questions": "none"}, [], {"questions": [], "run": True}):
        with pytest.raises(prompts.PromptError):
            prompts.check_clarify(bad)
    assert prompts.check_clarify({"questions": [{"question": f"key {SECRET}?", "why": "w"}]})[0]["question"] == "key [REDACTED]?"


def test_output_schema_is_validated_field_by_field():
    data = prompts.check_output(good_output(), "create")
    assert set(data) == set(prompts.TEXTS) | set(prompts.LISTS)
    assert data["suggested_roles"] == ["developer", "tester", "docs-writer"]
    with pytest.raises(prompts.PromptError, match="unknown fields"):
        prompts.check_output(dict(good_output(), tools=["Bash"]), "create")
    with pytest.raises(prompts.PromptError, match="missing fields"):
        prompts.check_output({k: v for k, v in good_output().items() if k != "tests"}, "create")
    with pytest.raises(prompts.PromptError, match="at most"):
        prompts.check_output(dict(good_output(), tasks=["t"] * (prompts.MAX_ITEMS + 1)), "create")
    with pytest.raises(prompts.PromptError, match="at most"):
        prompts.check_output(dict(good_output(), objective="x" * (prompts.MAX_TEXT + 1)), "create")
    with pytest.raises(prompts.PromptError, match="string"):
        prompts.check_output(dict(good_output(), tasks=[1, 2]), "create")
    with pytest.raises(prompts.PromptError, match="no 'review' block"):
        prompts.check_output(dict(good_output(), review={"grade": 1, "problems": []}), "create")
    with pytest.raises(prompts.PromptError, match="review"):
        prompts.check_output(good_output(), "review")
    for bad in ({"grade": 101, "problems": []}, {"grade": "9", "problems": []}, {"grade": True, "problems": []},
                {"grade": 5, "problems": "none"}, {"grade": 5}):
        with pytest.raises(prompts.PromptError):
            prompts.check_output(dict(good_output(), review=bad), "review")
    rv = prompts.check_output(dict(good_output(), review={"grade": 40, "problems": ["vague"]}), "review")["review"]
    assert rv == {"grade": 40, "problems": ["vague"]}
    # Role names the model invents outside the regex are dropped, never passed on.
    out = prompts.check_output(dict(good_output(), suggested_roles=["developer", "../x", "Bash Runner", "a" * 40, "ok-1"]), "create")
    assert out["suggested_roles"] == ["developer", "ok-1"]
    assert "review" in prompts.schema_for("review")["required"] and "review" not in prompts.PROMPT_SCHEMA["required"]
    assert "review" not in prompts.schema_for("fix")["properties"] and "review" in prompts.schema_for("review")["properties"]


def test_inputs_are_capped_enumerated_and_redacted():
    with pytest.raises(prompts.PromptError, match="idea is required"):
        prompts.check_inputs({"idea": "  "})
    with pytest.raises(prompts.PromptError, match="at most"):
        prompts.check_inputs({"idea": "x" * (prompts.MAX_IDEA + 1)})
    for bad in ({"mode": "deploy"}, {"detail": "huge"}, {"lang": "fr"}, {"answers": [{}] * 6}, {"answers": "x"},
                {"template": "../x"}, {"stack": 3}, {"prompt": "p" * (prompts.MAX_PROMPT_IN + 1), "mode": "review"}):
        with pytest.raises(prompts.PromptError):
            prompts.check_inputs({"idea": "x", **bad})
    with pytest.raises(prompts.PromptError, match="prompt is required"):
        prompts.check_inputs({"idea": "x", "mode": "review"})
    with pytest.raises(prompts.PromptError, match="changes is required"):
        prompts.check_inputs({"idea": "x", "mode": "edit", "prompt": "p"})
    inp = prompts.check_inputs({"idea": f"use {SECRET}", "stack": f"token {GH}", "mode": "edit", "prompt": f"key {SECRET}",
                                "changes": "c", "answers": [{"question": "q", "answer": f"a {SECRET}"}]})
    assert SECRET not in json.dumps(inp) and GH not in json.dumps(inp)
    assert inp["idea"] == "use [REDACTED]" and inp["answers"][0]["answer"] == "a [REDACTED]"


# --- the generator sees redacted, labelled data only -------------------------------------------

def test_secret_is_redacted_before_it_reaches_the_model(ui, repo, monkeypatch):
    seen = spy(monkeypatch)
    roles_mod.save_project_context(repo, f"Deploy with {GH} please")   # a secret in the repository's own context
    base = {"repo": str(repo), "backend": "claude", "idea": f"fix login, key {SECRET}", "stack": f"Flutter {SECRET}"}
    status, body, _ = call(ui, "POST", "/api/prompts/clarify", base)
    assert status == 200 and len(body["questions"]) == 2
    status, body, _ = call(ui, "POST", "/api/prompts/generate", dict(base, mode="review", prompt=f"token {GH} here",
                                                                     answers=[{"question": body["questions"][0]["question"], "answer": f"in {SECRET}"}]))
    assert status == 200
    assert len(seen) == 2
    for s in seen:
        blob = s["prompt"] + s["system"]
        assert SECRET not in blob and GH not in blob and "[REDACTED]" in s["prompt"], s["kind"]
    assert SECRET not in json.dumps(body) and GH not in json.dumps(body)
    assert "[REDACTED]" in body["text"] and "[REDACTED]" in body["diff"]


def test_project_context_is_code_generated_and_labelled_untrusted(ui, repo, monkeypatch):
    seen = spy(monkeypatch)
    assert call(ui, "POST", "/api/prompts/generate", {"repo": str(repo), "idea": "add a feature"})[0] == 200
    p = seen[0]["prompt"]
    assert prompts.UNTRUSTED_BEGIN in p and prompts.UNTRUSTED_END in p and pipeline.ARTIFACT_NOTICE in p
    block = p[p.index(prompts.UNTRUSTED_BEGIN):p.index(prompts.UNTRUSTED_END)]
    assert "developer: can edit" in block and "security-analyst: read-only" in block and "A tiny Python library." in block
    assert "RAW FILE CONTENT" not in p and "def add" not in p                     # never raw repository files
    assert "Project: repo" in block
    # Without a project nothing about any repository is sent.
    assert call(ui, "POST", "/api/prompts/generate", {"idea": "add a feature"})[0] == 200
    assert prompts.UNTRUSTED_BEGIN not in seen[1]["prompt"]
    # A repository outside the allowed roots is refused like everywhere else.
    assert call(ui, "POST", "/api/prompts/generate", {"repo": "/", "idea": "x"})[0] == 400


def test_generator_runs_with_no_tools_and_its_own_schema(monkeypatch):
    cmds = []
    monkeypatch.setattr(pipeline, "ask_json", lambda cmd, **kw: cmds.append(cmd) or prompts.mock_answer("clarify", None))
    inp = prompts.check_inputs({"idea": "x"})
    prompts.clarify(inp, backend="claude", model="haiku", effort="low")
    cmd = cmds[0]
    assert cmd[0] == "claude" and "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "--restricted" in cmd and "--disallowedTools" in cmd and "Bash,WebFetch,WebSearch,mcp__*" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == prompts.CLARIFY_SCHEMA
    assert cmd[cmd.index("--model") + 1] == "haiku" and cmd[cmd.index("--effort") + 1] == "low"
    # The plan command is unchanged: same lockdown, its own schema.
    pc = pipeline.planner_command("r", "s")
    assert json.loads(pc[pc.index("--json-schema") + 1]) == pipeline.PLAN_SCHEMA and "--tools" in pc


def test_targets_shape_the_system_prompt_and_the_output(ui, repo, monkeypatch):
    seen = spy(monkeypatch)
    with pytest.raises(prompts.PromptError, match="target"):
        prompts.check_inputs({"idea": "x", "target": "cursor"})
    assert prompts.check_inputs({"idea": "x"})["target"] == "claude-code"
    _, view, _ = call(ui, "GET", "/api/prompts")
    assert view["targets"] == ["claude-code", "uno"]
    _, cc, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "repo": str(repo), "target": "claude-code"})
    _, uno, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "repo": str(repo), "target": "uno"})
    sys_cc, sys_uno = seen[0]["system"], seen[1]["system"]
    assert "small commits on that branch are allowed" in sys_cc and "only after the user's explicit confirmation" in sys_cc
    assert "NEVER commit, push, create branches or open pull requests" in sys_uno and "ignore that part of the template" in sys_uno
    assert "one responsibility that a single role can own" in sys_uno and "paths of the roles" in sys_uno
    assert "Target: Uno's orchestrator" in seen[1]["prompt"] and "Target: Claude Code" in seen[0]["prompt"]
    assert cc["target"] == "claude-code" and uno["target"] == "uno"
    # The same git policy is stated whatever the mode, for Claude Code.
    for mode in ("fix", "audit", "review"):
        call(ui, "POST", "/api/prompts/generate", {"idea": "x", "mode": mode, "prompt": "p", "target": "claude-code"})
        assert "small commits on that branch are allowed" in seen[-1]["system"]
    # Lint: git instructions are only a problem for Uno; the mock closing for Claude Code names them.
    assert cc["lint"]["score"] == 100 and uno["lint"]["score"] == 100
    assert [w["code"] for w in prompts.lint(cc["text"], "uno")["warnings"]] == ["git_instructions"] * 2
    assert prompts.lint(uno["text"], "uno")["warnings"] == [] and prompts.lint(uno["text"], "claude-code")["warnings"] == []
    for line in ("Haz commit en la rama de trabajo", "Crea la rama feature/x", "git push origin", "Open a pull request", "Abre un PR al final",
                 "create a new branch", "commits pequeños", "Trabaja en una rama nueva"):
        assert [w["code"] for w in prompts.lint("OBJETIVO\n" + line, "uno")["warnings"] if w["code"] == "git_instructions"] == ["git_instructions"], line
        assert not [w for w in prompts.lint("OBJETIVO\n" + line, "claude-code")["warnings"] if w["code"] == "git_instructions"], line
        assert not [w for w in prompts.lint("OBJETIVO\n" + line)["warnings"] if w["code"] == "git_instructions"], line
    assert not [w for w in prompts.lint("OBJETIVO\nNada llega a tu rama hasta que fusiones.", "uno")["warnings"] if w["code"] == "git_instructions"]
    with pytest.raises(prompts.PromptError):
        prompts.lint("x", "cursor")
    assert call(ui, "POST", "/api/prompts/lint", {"text": "OBJETIVO\ngit push", "target": "uno"})[1]["warnings"][-1]["code"] == "git_instructions"
    assert call(ui, "POST", "/api/prompts/lint", {"text": "x", "target": "cursor"})[0] == 400
    for lang in ("es", "en"):
        d = json.loads((ROOT / "uno_orchestra" / "ui" / "i18n" / f"{lang}.json").read_text())
        assert {"lint.git_instructions", "ps.target.uno", "ps.target.claude-code", "ps.send.regenerate"} <= set(d)


# --- linter ------------------------------------------------------------------------------------

def test_linter_detects_each_problem():
    text = perfect()
    clean = prompts.lint(text)
    assert clean["score"] == 100 and clean["warnings"] == [] and clean["sections"] == list(prompts.SECTIONS)
    assert prompts.lint(text) == clean                                             # deterministic
    assert prompts.lint(perfect("en"))["score"] == 100                             # English headings too
    codes = lambda t: [w["code"] for w in prompts.lint(t)["warnings"]]
    assert "missing_section" in codes(text.replace("SEGURIDAD\n", "SEGURIDAD-ish\n"))
    assert "duplicate_section" in codes(text + "\n\nOBJETIVO\nagain")
    assert "empty_section" in codes(re.sub(r"CONTEXTO\n.*\n", "CONTEXTO\n", text))
    assert "no_out_of_scope" in codes(text.replace("Fuera de alcance:", "Otros:"))
    assert "tasks_not_numbered" in codes(text.replace("1. ", "- ").replace("2. ", "- ").replace("3. ", "- "))
    assert "vague_criterion" in codes(text.replace("- The new test passes.", "- que funcione bien y rápido"))
    assert "vague_criterion" not in codes(text.replace("- The new test passes.", "- funciona: pytest -q pasa con 0 fallos"))
    assert "no_tests" in codes(re.sub(r"TESTS\n- .*\n", "TESTS\n", text))
    assert "no_stop" in codes(re.sub(r"PUNTOS DE PARADA\n(- .*\n)+", "PUNTOS DE PARADA\n", text))
    assert codes(text.replace("CONTEXTO\n", "CONTEXTO\nProyecto <NOMBRE> en {{ruta}}, TODO\n")).count("placeholder") == 3
    assert "too_long" in codes(text + "\n" + "x" * prompts.LINT_MAX_LEN)
    w = prompts.lint(text.replace("CONTEXTO\n", f"CONTEXTO\nkey {SECRET}\n"))
    assert [x for x in w["warnings"] if x["code"] == "secret"][0]["severity"] == "high" and SECRET not in json.dumps(w)
    assert w["score"] == 70
    nothing = prompts.lint("hola")
    assert nothing["score"] == 0 and len([w for w in nothing["warnings"] if w["code"] == "missing_section"]) == 9
    for code in prompts.PENALTY:
        assert code in prompts.SEVERITY or code in ("empty_section", "no_out_of_scope", "tasks_not_numbered", "too_long")
    with pytest.raises(prompts.PromptError):
        prompts.lint(None)
    # The same patterns as the event log's redaction.
    assert prompts.has_secret(f"x {GH}") and prompts.has_secret("-----BEGIN RSA PRIVATE KEY-----\nabc") and not prompts.has_secret("sk-short")


def test_lint_endpoint_and_dictionaries_cover_every_code(ui):
    status, body, _ = call(ui, "POST", "/api/prompts/lint", {"text": "OBJETIVO\nx"})
    assert status == 200 and body["score"] < 100
    assert call(ui, "POST", "/api/prompts/lint", {"text": "x" * (prompts.MAX_PROMPT_OUT + 1)})[0] == 400
    assert call(ui, "POST", "/api/prompts/lint", {"text": 3})[0] == 400
    for lang in ("es", "en"):
        d = json.loads((ROOT / "uno_orchestra" / "ui" / "i18n" / f"{lang}.json").read_text())
        for code in prompts.PENALTY:
            assert f"lint.{code}" in d, (lang, code)
        for mode in prompts.MODES:
            assert f"ps.mode.{mode}" in d


# --- send to the orchestrator: a plan to approve, never a run ----------------------------------

def test_send_to_orchestrator_opens_a_plan_and_never_runs(ui, repo):
    roles_mod.trust(repo, ui.state_dir)
    head = git(repo, "rev-parse", "HEAD")
    text = perfect()
    status, body, _ = call(ui, "POST", "/api/prompts/send", {"text": text, "target": "uno", "repo": str(repo), "backend": "mock"})
    assert status == 200 and body["state"] in ("planning", "proposed")   # the mock planner may already be done
    assert wait_state(ui, "proposed", "idle") == "proposed"
    assert not ui.running() and ui.run is None and ui.chat.engine is None            # proposed, not started
    assert ui.chat.transcript[0]["who"] == "you" and ui.chat.transcript[0]["text"].startswith(text[:1000])
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "worktree", "list").count("\n") == 1
    assert not (ui.state_dir / "worktrees").exists() or not list((ui.state_dir / "worktrees").iterdir())
    _, snap, _ = call(ui, "GET", "/api/pipeline")
    assert snap["state"] == "proposed" and snap["plan"]["steps"]
    # The chat box keeps its own, shorter limit: the studio's path is the only one that accepts a long prompt.
    long_text = perfect() + "\n" + "x" * MAX_CHAT
    assert len(long_text) > MAX_CHAT
    assert call(ui, "POST", "/api/chat", {"message": long_text, "repo": str(repo), "backend": "mock"})[0] == 400
    assert call(ui, "POST", "/api/prompts/send", {"text": "y" * (prompts.MAX_PROMPT_OUT + 1), "target": "uno", "repo": str(repo), "backend": "mock"})[0] == 400
    # A prompt in which the linter still sees a secret is refused, whoever wrote it.
    status, body, _ = call(ui, "POST", "/api/prompts/send", {"text": f"{text}\nkey {SECRET}", "target": "uno", "repo": str(repo), "backend": "mock"})
    assert status == 400 and "secret" in body["error"]
    # The same rules as the chat: no roles confirmed, no plan.
    assert call(ui, "POST", "/api/pipeline/cancel", {})[0] == 200
    (repo / ".uno" / "project.json").write_text(json.dumps({"context": "changed outside Uno"}))
    assert call(ui, "POST", "/api/prompts/send", {"text": text, "target": "uno", "repo": str(repo), "backend": "mock"})[0] == 400


def test_send_requires_a_prompt_generated_for_uno(ui, repo, monkeypatch):
    roles_mod.trust(repo, ui.state_dir)
    spy(monkeypatch)
    _, cc, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "target": "claude-code", "backend": "mock"})
    assert cc["target"] == "claude-code"
    for body in ({"text": cc["text"]}, {"text": cc["text"], "target": "claude-code"}, {"text": cc["text"], "target": "uno "}):
        status, r, _ = call(ui, "POST", "/api/prompts/send", {**body, "repo": str(repo), "backend": "mock"})
        assert status == 409 and r["code"] == "target_not_uno", body
    assert ui.chat.state == "idle"
    assert call(ui, "POST", "/api/prompts/library", {"name": "cc", "text": cc["text"], "target": "claude-code"})[0] == 200
    status, r, _ = call(ui, "POST", "/api/prompts/send", {"name": "cc", "repo": str(repo), "backend": "mock"})
    assert status == 409 and r["code"] == "target_not_uno"      # the library entry's own target decides, not the request
    assert call(ui, "POST", "/api/prompts/library", {"name": "u", "text": perfect(), "target": "uno"})[0] == 200
    assert call(ui, "POST", "/api/prompts/send", {"name": "u", "repo": str(repo), "backend": "mock"})[0] == 200
    assert wait_state(ui, "proposed", "idle") == "proposed"
    assert call(ui, "POST", "/api/prompts/library", {"name": "bad", "text": "t", "target": "cursor"})[0] == 400
    js = (ROOT / "uno_orchestra" / "ui" / "app.js").read_text()
    assert "t('ps.send.regenerate')" in js and "target: 'uno'" in js      # the page offers to regenerate, never sends as it is


def test_send_from_the_library_by_name_and_version(ui, repo):
    roles_mod.trust(repo, ui.state_dir)
    assert call(ui, "POST", "/api/prompts/library", {"name": "p", "text": perfect(), "mode": "fix", "target": "uno"})[0] == 200
    assert call(ui, "POST", "/api/prompts/library", {"name": "p", "text": perfect() + "\nv2", "mode": "fix", "target": "uno"})[0] == 200
    status, body, _ = call(ui, "POST", "/api/prompts/send", {"name": "p", "version": 1, "repo": str(repo), "backend": "mock"})
    assert status == 200 and wait_state(ui, "proposed", "idle") == "proposed"
    assert not ui.chat.transcript[0]["text"].endswith("v2")
    assert call(ui, "POST", "/api/prompts/send", {"name": "p", "version": 9, "repo": str(repo), "backend": "mock"})[0] == 400
    assert call(ui, "POST", "/api/prompts/send", {"name": "../p", "repo": str(repo), "backend": "mock"})[0] == 400


# --- model policy ------------------------------------------------------------------------------

def test_fable_is_impossible_without_override_and_confirmation(ui, monkeypatch):
    seen = spy(monkeypatch)
    _, view, _ = call(ui, "GET", "/api/prompts")
    assert view["default"] == {"model": "sonnet", "effort": "high"} and models.default_for("prompt-studio") == ("sonnet", "high")
    assert not models.is_override_only(view["default"]["model"])
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "model": "fable", "effort": "max"})
    assert status == 409 and body["code"] == "confirm_override_only" and seen == []
    status, body, _ = call(ui, "POST", "/api/prompts/clarify", {"idea": "x", "model": "fable", "effort": "max", "confirm_override_only": "yes"})
    assert status == 409 and seen == []
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "model": "fable", "effort": "max", "confirm_override_only": True})
    assert status == 200 and body["override_only"] is True and body["model"] == "fable" and seen[-1]["model"] == "fable"
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "model": "opus", "effort": "max"})
    assert status == 200 and body["override_only"] is False
    for bad in ({"model": "gpt-4"}, {"effort": "ultra"}, {"model": ""}):
        assert call(ui, "POST", "/api/prompts/generate", {"idea": "x", **bad})[0] == 400
    # Templates never carry a model; a saved prompt records the model as text and nothing reads it back.
    status, body, _ = call(ui, "POST", "/api/prompts/templates", {"name": "t", "mode": "fix", "instructions": "i", "model": "fable"})
    assert status == 400 and "unknown fields" in body["error"]
    assert call(ui, "POST", "/api/prompts/library", {"name": "p", "text": perfect(), "model": "fable"})[0] == 200
    _, entry, _ = call(ui, "GET", "/api/prompts/library?name=p")
    assert entry["model"] == "fable"
    src = (ROOT / "uno_orchestra" / "prompts.py").read_text()
    assert "models.check" not in src                                               # the server validates per request; the library never feeds a model anywhere
    # One generation at a time.
    import time as _t
    monkeypatch.setattr(prompts, "ask", lambda *a, **k: _t.sleep(0.8) or prompts.mock_answer("clarify", None))
    t = threading.Thread(target=lambda: call(ui, "POST", "/api/prompts/clarify", {"idea": "slow"}))
    t.start()
    time.sleep(0.2)
    assert call(ui, "POST", "/api/prompts/clarify", {"idea": "second"})[0] == 409
    t.join()


# --- library and templates ---------------------------------------------------------------------

def test_library_is_private_versioned_tagged_and_searchable(ui):
    state = ui.state_dir
    assert call(ui, "POST", "/api/prompts/library", {"name": "login-fix", "text": perfect(), "tags": ["auth", "flutter"], "mode": "fix", "idea": "fix login", "score": 100})[0] == 200
    assert call(ui, "POST", "/api/prompts/library", {"name": "login-fix", "text": perfect() + "\nv2", "tags": ["auth"], "mode": "fix", "idea": "fix login"})[0] == 200
    assert call(ui, "POST", "/api/prompts/library", {"name": "new-page", "text": perfect("en"), "mode": "create", "lang": "en"})[0] == 200
    lib = state / "prompts" / "library"
    for d in (state / "prompts", lib, lib / "login-fix"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    for f in lib.rglob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f
    assert sorted(p.name for p in (lib / "login-fix").iterdir()) == ["1.json", "2.json"]
    _, res, _ = call(ui, "GET", "/api/prompts/library")
    assert [e["name"] for e in res["entries"]] == ["login-fix", "new-page"] and "text" not in res["entries"][0]
    assert res["entries"][0]["version"] == 2 and res["entries"][0]["versions"] == [1, 2] and res["entries"][0]["tags"] == ["auth"]
    assert res["entries"][0]["target"] == "claude-code"                                 # the default when not given
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library?q=flutter")[1]["entries"]] == []      # tags of the latest version only
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library?q=LOGIN")[1]["entries"]] == ["login-fix"]
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library?tag=auth")[1]["entries"]] == ["login-fix"]
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library?mode=create")[1]["entries"]] == ["new-page"]
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library?q=OBJECTIVE")[1]["entries"]] == ["new-page"]  # full text is searched
    assert call(ui, "GET", "/api/prompts/library?q=" + "x" * 201)[0] == 400
    _, v1, _ = call(ui, "GET", "/api/prompts/library?name=login-fix&version=1")
    assert v1["version"] == 1 and v1["tags"] == ["auth", "flutter"] and not v1["text"].endswith("v2")
    assert call(ui, "GET", "/api/prompts/library?name=login-fix&version=x")[0] == 400
    assert call(ui, "GET", "/api/prompts/library?name=login-fix&version=3")[0] == 400
    assert call(ui, "GET", "/api/prompts/library?name=..%2Ftoken")[0] == 400 and call(ui, "GET", "/api/prompts/library?name=nope")[0] == 400
    _, d, _ = call(ui, "POST", "/api/prompts/diff", {"name": "login-fix", "a": 1, "b": 2})
    assert d["diff"].startswith("--- v1\n+++ v2") and "+v2" in d["diff"]
    assert call(ui, "POST", "/api/prompts/diff", {"name": "login-fix", "a": "1", "b": 2})[0] == 400
    assert "+b" in call(ui, "POST", "/api/prompts/diff", {"text_a": "a", "text_b": "b"})[1]["diff"]
    for bad in ({"name": "../x", "text": "t"}, {"name": "x", "text": ""}, {"name": "x", "text": "t", "tags": ["Bad Tag"]},
                {"name": "x", "text": "t", "tags": ["t"] * 11}, {"name": "x", "text": "t", "score": 101},
                {"name": "x", "text": "t", "mode": "deploy"}, {"name": "x", "text": "t", "extra": 1},
                {"name": "x", "text": "t" * (prompts.MAX_PROMPT_OUT + 1)}):
        assert call(ui, "POST", "/api/prompts/library", bad)[0] == 400, bad
    assert not list(state.glob("**/x")) and not list(state.glob("**/..*"))
    # A file edited by hand that no longer validates is an error on load, not a surprise.
    (lib / "login-fix" / "2.json").write_text(json.dumps({"name": "login-fix", "text": "t", "tools": ["Bash"]}))
    assert call(ui, "GET", "/api/prompts/library?name=login-fix")[0] == 400
    assert [e["name"] for e in call(ui, "GET", "/api/prompts/library")[1]["entries"]] == ["new-page"]
    assert call(ui, "POST", "/api/prompts/library/delete", {"name": "login-fix", "version": 2})[0] == 200
    _, v, _ = call(ui, "GET", "/api/prompts/library?name=login-fix")
    assert v["version"] == 1 and v["versions"] == [1]
    assert call(ui, "POST", "/api/prompts/library/delete", {"name": "login-fix"})[0] == 200
    assert not (lib / "login-fix").exists()
    assert call(ui, "POST", "/api/prompts/library/delete", {"name": "login-fix"})[0] == 400


def test_templates_builtin_and_personal_versions(ui, monkeypatch):
    _, view, _ = call(ui, "GET", "/api/prompts")
    names = {t["name"]: t for t in view["templates"]}
    assert set(names) == set(prompts.MODES) and all(t["builtin"] for t in names.values())
    assert all(names[m]["mode"] == m for m in prompts.MODES)
    assert "phase prompts" in names["fix"]["instructions"] and "CLOSING" in names["create"]["instructions"]
    status, body, _ = call(ui, "POST", "/api/prompts/templates", {"name": "fix", "mode": "fix", "instructions": "my own rules"})
    assert status == 200 and body["version"] == 1
    status, body, _ = call(ui, "POST", "/api/prompts/templates", {"name": "fix", "mode": "fix", "instructions": "my own rules v2"})
    assert status == 200 and body["version"] == 2
    mine = {t["name"]: t for t in body["templates"]}["fix"]
    assert mine["builtin"] is False and mine["version"] == 2 and mine["versions"] == [1, 2] and mine["instructions"] == "my own rules v2"
    d = ui.state_dir / "prompts" / "templates" / "fix"
    assert stat.S_IMODE(d.stat().st_mode) == 0o700 and stat.S_IMODE((d / "2.json").stat().st_mode) == 0o600
    seen = spy(monkeypatch)
    assert call(ui, "POST", "/api/prompts/generate", {"idea": "x", "mode": "fix"})[0] == 200
    assert "my own rules v2" in seen[-1]["system"] and "my own rules\n" not in seen[-1]["system"]
    assert call(ui, "POST", "/api/prompts/generate", {"idea": "x", "mode": "fix", "template": "create"})[0] == 200   # any template by name
    assert "my own rules" not in seen[-1]["system"]
    assert call(ui, "POST", "/api/prompts/generate", {"idea": "x", "template": "nope"})[0] == 400
    for bad in ({"name": "x", "mode": "deploy", "instructions": "i"}, {"name": "x", "mode": "fix", "instructions": ""},
                {"name": "x", "mode": "fix", "instructions": "i" * 4001}, {"name": "../x", "mode": "fix", "instructions": "i"}):
        assert call(ui, "POST", "/api/prompts/templates", bad)[0] == 400, bad
    status, body, _ = call(ui, "POST", "/api/prompts/templates/delete", {"name": "fix"})
    assert status == 200 and {t["name"]: t for t in body["templates"]}["fix"]["builtin"] is True
    assert call(ui, "POST", "/api/prompts/templates/delete", {"name": "create"})[0] == 400       # built-ins cannot be deleted


# --- output as data ----------------------------------------------------------------------------

def test_prompt_text_with_html_is_data_and_rendered_as_text(ui, repo, monkeypatch):
    spy(monkeypatch)
    payload = "<img src=x onerror=alert(1)>"
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": f"fix {payload}", "repo": str(repo), "mode": "fix"})
    assert status == 200 and payload in body["text"] and payload in body["title"]
    assert body["missing_roles"] == ["tester", "docs-writer"]   # install_templates() writes no tester
    assert body["suggested_roles"] == ["developer", "tester", "docs-writer"]
    assert body["lint"]["score"] == 100 and body["open_questions"] == [] and body["mode"] == "fix"
    assert call(ui, "POST", "/api/prompts/library", {"name": "x", "text": body["text"]})[0] == 200
    assert payload in call(ui, "GET", "/api/prompts/library?name=x")[1]["text"]
    js = (ROOT / "uno_orchestra" / "ui" / "app.js").read_text()
    assert "$('psText').value = r.text" in js and "textContent" in js
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in js
    html = (ROOT / "uno_orchestra" / "ui" / "index.html").read_text()
    assert 'id="studio"' in html and 'data-tab="prompts"' in html and not re.search(r"\son\w+=", html)
    # Review mode: the model's grade and the diff are data too.
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "mode": "review", "prompt": f"do it {payload}"})
    assert status == 200 and body["review"]["grade"] == 55 and payload in body["diff"] and body["lint_input"]["score"] < 50
    status, body, _ = call(ui, "POST", "/api/prompts/generate", {"idea": "x", "mode": "edit", "prompt": "old", "changes": "new"})
    assert status == 200 and body["diff"].startswith("--- yours\n+++ new") and "review" not in body


def test_get_and_head_never_touch_studio_state(ui):
    assert call(ui, "POST", "/api/prompts/library", {"name": "seed", "text": perfect()})[0] == 200
    before = {str(p): p.read_bytes() for p in ui.state_dir.rglob("*.json")}
    for method in ("GET", "HEAD"):
        for path in ("/api/prompts/generate", "/api/prompts/clarify", "/api/prompts/send", "/api/prompts/lint",
                     "/api/prompts/library/delete", "/api/prompts/templates", "/api/prompts/templates/delete", "/api/prompts/diff",
                     "/api/prompts/send?text=hi&repo=/tmp", "/api/prompts/library/delete?name=seed"):
            assert call(ui, method, path)[0] == 404, (method, path)
        for path in ("/api/prompts", "/api/prompts/library", "/api/prompts/library?q=seed", "/api/prompts/library?name=seed", "/api/prompts/library?name=none"):
            assert call(ui, method, path)[0] in (200, 400), (method, path)
    assert {str(p): p.read_bytes() for p in ui.state_dir.rglob("*.json")} == before
    assert call(ui, "GET", "/api/prompts", token=None)[0] == 401 and call(ui, "POST", "/api/prompts/lint", {"text": "x"}, token=None)[0] == 401
    assert call(ui, "POST", "/api/prompts/lint", {"text": "x"}, headers={"Origin": "http://evil.example"})[0] == 403


def test_version_and_changelog():
    from uno_orchestra import __version__
    assert __version__ == "0.3.1"                                                  # phase 6 + stream-json line fix
    assert f'version = "{__version__}"' in (ROOT / "pyproject.toml").read_text()
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text()
    assert "## Prompt Studio" in (ROOT / "README.md").read_text()
