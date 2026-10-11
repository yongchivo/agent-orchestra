"""Phase 4: `uno init`, `uno doctor`, the two migrations, package data and the installed CLI."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from uno_orchestra import cli, doctor, init, roles as roles_mod, scope, state
from uno_orchestra.worker import agent_settings

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def commit(repo, msg="c"):
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    commit(path, "init")
    return path


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "state"


def accept_all(q, d):
    return d


def snapshot(root):
    """{relative path: (mode, sha256)} of every file, .git included."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = (stat.S_IMODE(p.lstat().st_mode), hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def python_layout(repo):
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "__init__.py").write_text("x = 1\n")
    (repo / "src" / "pkg" / "core.py").write_text("def f():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_core.py").write_text("def test_f():\n    assert True\n")
    (repo / "pyproject.toml").write_text("[project]\nname='pkg'\n")
    (repo / "README.md").write_text("# pkg\n")
    commit(repo, "layout")


def js_layout(repo):
    (repo / "app").mkdir()
    (repo / "app" / "index.js").write_text("module.exports = 1;\n")
    (repo / "app" / "util.ts").write_text("export const x = 1;\n")
    (repo / "__tests__").mkdir()
    (repo / "__tests__" / "index.test.js").write_text("test('x', () => {});\n")
    (repo / "package.json").write_text('{"name": "app"}\n')
    (repo / "package-lock.json").write_text("{}\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    commit(repo, "layout")


# --- init ------------------------------------------------------------------------------------

def test_init_in_an_empty_repo_writes_four_trusted_roles(repo, state_dir):
    said = []
    assert init.run(repo, state_dir, ask=accept_all, say=said.append) == 0
    roles = roles_mod.load_roles(repo)
    assert set(roles) == {"developer", "tester", "security-analyst", "architect"}
    assert roles["developer"].paths == ["**"] and roles["developer"].preset == "edit"
    assert roles["tester"].paths == ["tests/**"] and roles["tester"].preset == "edit"
    assert roles["security-analyst"].preset == "readonly" and roles["architect"].preset == "readonly"
    assert roles_mod.trusted(repo, state_dir)                       # written by the user: confirmed
    assert stat.S_IMODE((state_dir / "trust.json").stat().st_mode) == 0o600
    assert "events.jsonl" in (repo / ".gitignore").read_text().split()
    assert "none detected" in roles_mod.project_context(repo)
    assert any("git add .uno .gitignore" in s for s in said)


def test_init_detects_a_python_layout(repo, state_dir):
    python_layout(repo)
    a = init.analyze(repo)
    assert a["languages"][0] == "Python" and a["package_manager"] == "pip" and a["has_tests"]
    assert a["dirs"] == ["src", "tests"] and a["readme"] == "README.md" and not a["claude_md"]
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    roles = roles_mod.load_roles(repo)
    assert roles["developer"].paths == ["src/**"]
    assert roles["tester"].paths == ["tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
    assert "Extend the existing test suite" in roles["tester"].instructions
    ctx = roles_mod.project_context(repo)
    assert "Python (3 files)" in ctx and "src/, tests/" in ctx and "pip" in ctx and "Tests: yes" in ctx


def test_init_detects_a_js_layout(repo, state_dir):
    js_layout(repo)
    a = init.analyze(repo)
    assert a["languages"] == ["JavaScript", "TypeScript"] and a["package_manager"] == "npm"
    assert a["dirs"] == ["app", "__tests__", ".github"] and a["has_tests"]
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    roles = roles_mod.load_roles(repo)
    assert roles["developer"].paths == ["app/**"]
    assert roles["tester"].paths[0] == "__tests__/**" and "**/*.test.ts" in roles["tester"].paths


def test_init_without_tests_proposes_creating_them(repo, state_dir):
    (repo / "main.go").write_text("package main\n")
    commit(repo)
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    tester = roles_mod.load_roles(repo)["tester"]
    assert "no tests yet" in tester.instructions and tester.paths == ["**/*_test.go"]
    assert roles_mod.load_roles(repo)["developer"].paths == ["**/*.go"]


def test_init_in_a_folder_without_git_offers_git_init(tmp_path, state_dir):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "a.py").write_text("x = 1\n")
    declined = []
    assert init.run(folder, state_dir, ask=lambda q, d: "n" if "git init" in q else d, say=declined.append) == 1
    assert not (folder / ".git").exists() and not (folder / ".uno").exists()
    assert init.run(folder, state_dir, ask=accept_all, say=lambda s: None) == 0
    assert git(folder, "log", "--format=%s").split() == ["Initial", "commit"][:1] or "Initial commit" in git(folder, "log", "--format=%s")
    assert "a.py" in git(folder, "ls-files")                  # the project as it was, committed first
    assert ".uno" not in git(folder, "ls-files")               # .uno/ is left for the user to commit
    assert roles_mod.load_roles(folder) and roles_mod.trusted(folder, state_dir)


def test_init_asks_before_using_claude_md(repo, state_dir):
    (repo / "CLAUDE.md").write_text("Always use tabs.\n")
    commit(repo)
    assert init.run(repo, state_dir, ask=lambda q, d: "n" if "CLAUDE.md" in q else d, say=lambda s: None) == 0
    assert "tabs" not in roles_mod.project_context(repo)
    assert init.run(repo, state_dir, ask=lambda q, d: "y", say=lambda s: None, force=True) == 0
    assert "From CLAUDE.md:\nAlways use tabs." in roles_mod.project_context(repo)


def test_init_lets_the_user_edit_paths_and_remove_roles(repo, state_dir):
    python_layout(repo)
    answers = iter(["e", "src/pkg/** docs/**",   # developer: edit paths
                    "r",                           # tester: remove
                    "e", "/etc/**", "a",           # security-analyst: bad paths, then accept
                    "a"])                          # architect

    def ask(q, d):
        return next(answers) if "[a]ccept" in q or "paths" in q else d
    said = []
    assert init.run(repo, state_dir, ask=ask, say=said.append) == 0
    roles = roles_mod.load_roles(repo)
    assert set(roles) == {"developer", "security-analyst", "architect"}
    assert roles["developer"].paths == ["src/pkg/**", "docs/**"]
    assert roles["security-analyst"].paths == ["**"] and any("must be relative" in s for s in said)


def test_init_is_idempotent_and_force_asks(repo, state_dir):
    python_layout(repo)
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    before = snapshot(repo)
    roles_mod.save_role(repo, roles_mod.role_from_json({"name": "ops", "description": "d", "instructions": "i",
                                                        "preset": "readonly", "paths": ["ops/**"]}))
    roles_mod.trust(repo, state_dir)
    said = []
    assert init.run(repo, state_dir, ask=accept_all, say=said.append) == 0
    assert any("Nothing to do" in s for s in said)
    assert {k: v for k, v in snapshot(repo).items() if "ops.json" not in k} == before
    # --force: declined -> untouched; accepted -> regenerated, extra roles kept
    assert init.run(repo, state_dir, ask=lambda q, d: "n" if "Regenerate" in q else d, say=lambda s: None, force=True) == 1
    (repo / ".uno" / "roles" / "developer.json").write_text(json.dumps({
        "name": "developer", "description": "edited", "instructions": "i", "preset": "edit", "paths": ["x/**"]}))
    assert init.run(repo, state_dir, ask=lambda q, d: "y", say=lambda s: None, force=True) == 0
    roles = roles_mod.load_roles(repo)
    assert roles["developer"].paths == ["src/**"] and "ops" in roles and roles_mod.trusted(repo, state_dir)


def test_init_touches_nothing_outside_uno_and_gitignore(repo, state_dir):
    python_layout(repo)
    (repo / ".gitignore").write_text("*.pyc\n")
    commit(repo)
    before = snapshot(repo)
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    after = snapshot(repo)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    assert changed and all(p.startswith(".uno/") or p == ".gitignore" for p in changed), changed
    assert (repo / ".gitignore").read_text().startswith("*.pyc\n")
    status = {line[3:] for line in git(repo, "status", "--porcelain").splitlines()}
    assert status == {".uno/", ".gitignore"}
    assert init.ensure_gitignore(repo) is False                  # second time: nothing appended


# --- doctor ------------------------------------------------------------------------------------

def fake_claude(tmp_path, logged_in):
    exe = tmp_path / "claude"
    exe.write_text(f"#!/bin/sh\necho '{json.dumps({'loggedIn': logged_in, 'authMethod': 'claude.ai'})}'\n")
    exe.chmod(0o755)
    return str(exe)


def results(repo, state_dir, **kw):
    said = []
    kw.setdefault("env", {})
    fails = doctor.run(repo, state_dir, say=said.append, **kw)
    return fails, "\n".join(said)


def test_doctor_passes_on_a_healthy_setup(repo, state_dir, tmp_path):
    git(repo, "checkout", "-q", "-b", "work")
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    commit(repo, "roles")
    claude = fake_claude(tmp_path, True)
    uno = tmp_path / "bin" / "uno"
    uno.parent.mkdir()
    uno.write_text("#!/bin/sh\n")
    which = lambda n: {"claude": claude, "uno": str(uno), "git": "/usr/bin/git"}.get(n)  # noqa: E731
    fails, out = results(repo, state_dir, which=which, argv0=str(uno), home=tmp_path)
    assert fails == 0 and "all checks passed" in out and "FAIL" not in out and "warn" not in out


def test_doctor_detects_each_failure(repo, state_dir, tmp_path):
    which_none = lambda n: None  # noqa: E731
    fails, out = results(repo, state_dir, which=which_none, argv0="uno")
    assert "FAIL] git: not found" in out and "install git" in out
    assert "FAIL] claude CLI: not found" in out and "npm install -g @anthropic-ai/claude-code" in out
    assert "FAIL] project: no .uno/" in out and "uno init" in out
    assert "FAIL] uno on PATH: `uno` is not on PATH" in out and "pipx ensurepath" in out
    assert fails == 4

    # not logged in
    claude = fake_claude(tmp_path, False)
    _, out = results(repo, state_dir, which=lambda n: claude if n == "claude" else None)
    assert "FAIL] claude login: not logged in" in out and "claude auth login" in out
    # API key counts as logged in, and --bare is mentioned
    _, out = results(repo, state_dir, which=lambda n: claude if n == "claude" else None, env={"ANTHROPIC_API_KEY": "x"})
    assert "ok] claude login: ANTHROPIC_API_KEY" in out

    # state dir too open, token too open
    state_dir.mkdir()
    (state_dir / "token").write_text("t")
    os.chmod(state_dir, 0o755)
    os.chmod(state_dir / "token", 0o644)
    _, out = results(repo, state_dir, which=which_none)
    assert "FAIL] state dir: readable by other users" in out and f"chmod 700 {state_dir}" in out
    os.chmod(state_dir, 0o700)
    os.chmod(state_dir / "token", 0o600)
    _, out = results(repo, state_dir, which=which_none)
    assert "ok] state dir" in out

    # legacy state dir left behind
    (tmp_path / state.LEGACY).mkdir()
    (tmp_path / state.NAME).mkdir()
    _, out = results(repo, state_dir, which=which_none, home=tmp_path)
    assert "warn] legacy state" in out and f"rm -r {tmp_path / state.LEGACY}" in out

    # project: invalid role, unconfirmed roles, uncommitted roles, main branch, legacy .orchestra/
    assert init.run(repo, state_dir, ask=accept_all, say=lambda s: None) == 0
    _, out = results(repo, state_dir, which=which_none)
    assert "warn] roles committed: uncommitted" in out and "warn] branch: on main" in out
    (state_dir / "trust.json").unlink()
    _, out = results(repo, state_dir, which=which_none)
    assert "FAIL] roles confirmed" in out and "Roles & project" in out
    (repo / ".uno" / "roles" / "developer.json").write_text('{"name": "developer", "tools": ["Bash"]}')
    _, out = results(repo, state_dir, which=which_none)
    assert "FAIL] project: .uno/roles/developer.json" in out and "uno init --force" in out
    git(repo, "checkout", "-q", "-b", "work")
    (repo / ".uno").rename(repo / ".orchestra")
    _, out = results(repo, state_dir, which=which_none)
    assert "warn] project: .orchestra/ is the pre-0.1.0 name" in out and "FAIL] project: no .uno/" in out

    # not a repository
    _, out = results(tmp_path / "nowhere", state_dir, which=which_none)
    assert "FAIL] project" in out and "not inside a git repository" in out

    # shadowed by another `uno`
    other = tmp_path / "other-uno"
    other.write_text("")
    _, out = results(repo, state_dir, which=lambda n: str(other) if n == "uno" else None, argv0=str(tmp_path / "uno"))
    assert "FAIL] uno on PATH: `uno` resolves to" in out and "shadows this one" in out


# --- migrations --------------------------------------------------------------------------------

def test_state_dir_migration_copies_verifies_and_removes_the_old_one(tmp_path):
    old = tmp_path / state.LEGACY
    (old / "runs").mkdir(parents=True)
    (old / "worktrees").mkdir()
    (old / "token").write_text("secret")
    (old / "trust.json").write_text('{"/r": "abc"}')
    (old / "recent-repos.json").write_text('["/r"]')
    (old / "runs" / "t.json").write_text("{}")
    os.chmod(old, 0o755)
    os.chmod(old / "token", 0o644)
    new = state.migrate_legacy(tmp_path)
    assert new == tmp_path / state.NAME and not old.exists()
    assert (new / "token").read_text() == "secret" and json.loads((new / "trust.json").read_text()) == {"/r": "abc"}
    assert (new / "recent-repos.json").read_text() == '["/r"]' and (new / "runs" / "t.json").is_file()
    assert stat.S_IMODE(new.stat().st_mode) == 0o700 and stat.S_IMODE((new / "runs").stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in new.rglob("*") if p.is_file())
    assert state.migrate_legacy(tmp_path) is None           # nothing left to migrate
    # an existing new dir is never overwritten, and the old one is left alone
    old.mkdir()
    (old / "token").write_text("other")
    assert state.migrate_legacy(tmp_path) is None and old.is_dir() and (new / "token").read_text() == "secret"


def test_state_dir_migration_keeps_the_old_one_when_the_copy_is_bad(tmp_path, monkeypatch):
    old = tmp_path / state.LEGACY
    old.mkdir()
    (old / "token").write_text("secret")
    real = state.shutil.copytree

    def corrupt(src, dst, **kw):
        real(src, dst, **kw)
        (Path(dst) / "token").write_text("corrupt")
    monkeypatch.setattr(state.shutil, "copytree", corrupt)
    with pytest.raises(RuntimeError, match="verification"):
        state.migrate_legacy(tmp_path)
    assert (old / "token").read_text() == "secret" and not (tmp_path / state.NAME).exists()
    assert not (tmp_path / (state.NAME + ".migrating")).exists()


def test_cli_migrates_the_state_dir_on_first_start(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / state.LEGACY).mkdir()
    (tmp_path / state.LEGACY / "token").write_text("t")
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    assert cli.main(["doctor", str(repo)]) == 1                 # some checks fail; only the migration matters here
    assert (tmp_path / state.NAME / "token").read_text() == "t" and not (tmp_path / state.LEGACY).exists()


def test_legacy_project_dir_is_renamed_only_with_confirmation(repo, state_dir):
    roles_mod.DIR, legacy = roles_mod.LEGACY_DIR, roles_mod.DIR   # write a pre-0.1.0 .orchestra/
    try:
        roles_mod.install_templates(repo)
    finally:
        roles_mod.DIR = legacy
    commit(repo, "old roles")
    assert roles_mod.has_legacy_dir(repo) and not roles_mod.load_roles(repo)
    said = []
    assert init.migrate_legacy_dir(repo, lambda q, d: "n", said.append) is False
    assert (repo / ".orchestra").is_dir() and not (repo / ".uno").exists() and "stays protected" in said[-1]
    assert init.migrate_legacy_dir(repo, lambda q, d: "y", said.append) is True
    assert not (repo / ".orchestra").exists() and set(roles_mod.load_roles(repo)) == {"developer", "security-analyst", "architect"}
    assert ".uno/roles/developer.json" in git(repo, "status", "--porcelain")   # git mv, staged
    assert not roles_mod.has_legacy_dir(repo)
    # `uno init` on such a repository migrates and then reports "nothing to do"
    (repo / ".uno").rename(repo / ".orchestra")
    said.clear()
    assert init.run(repo, state_dir, ask=accept_all, say=said.append) == 0
    assert (repo / ".uno").is_dir() and any("Nothing to do" in s for s in said)


def test_uno_dir_is_protected_everywhere():
    assert scope.is_protected(".uno/project.json") and scope.is_protected("sub/.uno/roles/x.json")
    assert scope.is_protected(".orchestra/project.json")
    assert scope.check([{"path": ".uno/roles/dev.json", "status": "M", "mode": "100644", "size": 1, "binary": False}],
                       ["**"])[0] == "needs_review"
    deny = agent_settings()["permissions"]["deny"]
    assert "Edit(//**/.uno/**)" in deny and "Write(//**/.uno/**)" in deny
    with pytest.raises(ValueError, match="outside .uno/"):
        roles_mod.role_from_json({"name": "x", "description": "", "instructions": "", "preset": "edit", "paths": [".uno/**"]})
    with pytest.raises(ValueError, match="outside .uno/"):
        roles_mod.role_from_json({"name": "x", "description": "", "instructions": "", "preset": "edit", "paths": [".orchestra/**"]})


# --- packaging: wheel, clean venv, package data ---------------------------------------------------

def test_wheel_installs_in_a_clean_venv_and_serves_the_ui(tmp_path):
    dist = tmp_path / "dist"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-q", "-w", str(dist), str(ROOT)],
                   check=True, cwd=tmp_path, timeout=600)
    wheel, = dist.glob("uno_orchestra-0.1.0-*.whl")
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=120)
    pip = venv / "bin" / "pip"
    subprocess.run([str(pip), "install", "-q", "--no-deps", str(wheel)], check=True, cwd=tmp_path, timeout=300)
    uno = venv / "bin" / "uno"
    out = subprocess.run([str(uno), "--version"], capture_output=True, text=True, check=True, cwd=tmp_path)
    assert out.stdout.strip() == "uno 0.1.0"
    where = subprocess.run([str(venv / "bin" / "python"), "-c", "import uno_orchestra; print(uno_orchestra.__file__)"],
                           capture_output=True, text=True, check=True, cwd=tmp_path).stdout
    assert "site-packages" in where                          # the installed copy, not the checkout
    proc = subprocess.Popen([str(uno), "ui", "--no-browser", "--port", "0", "--state-dir", str(tmp_path / "s")],
                            stdout=subprocess.PIPE, text=True, cwd=tmp_path)
    try:
        url = proc.stdout.readline()
        m = re.match(r"Uno UI: (http://127\.0\.0\.1:\d+)/#token=([A-Za-z0-9_-]+)", url)
        assert m, url
        base, token = m.groups()
        for path, marker in (("/", b"<title>Uno"), ("/app.js", b"Uno control UI"), ("/style.css", b"{")):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                assert r.status == 200 and marker in r.read(), path
        req = urllib.request.Request(base + "/api/status", headers={"X-Orchestra-Token": token})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read())["state_dir"] == str(tmp_path / "s")
        assert stat.S_IMODE((tmp_path / "s" / "token").stat().st_mode) == 0o600
    finally:
        proc.kill()
        proc.wait()
    # the dashboard's page is package data too
    proc = subprocess.Popen([str(uno), "dashboard", "--port", "0", "--events", str(tmp_path / "e.jsonl"),
                             "--state-dir", str(tmp_path / "s")],
                            stdout=subprocess.PIPE, text=True, cwd=tmp_path)
    try:
        banner = proc.stdout.readline()
        m = re.match(r"dashboard: (http://127\.0\.0\.1:\d+)", banner)
        assert m, banner
        with urllib.request.urlopen(m.group(1) + "/", timeout=5) as r:
            assert b"<title>Uno" in r.read()
    finally:
        proc.kill()
        proc.wait()


def test_cli_defaults_to_ui_and_has_help_per_subcommand(capsys):
    for args in (["--help"], ["init", "--help"], ["ui", "--help"], ["run", "--help"], ["doctor", "--help"], ["dashboard", "--help"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(args)
        assert exc.value.code == 0 and "usage: uno" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == "uno 0.1.0"
    parser = cli.build_parser()
    assert parser.parse_args(["ui", "--no-browser"]).cmd == "ui"
    assert cli.main.__module__ == "uno_orchestra.cli"
