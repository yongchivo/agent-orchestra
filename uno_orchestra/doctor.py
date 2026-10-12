"""`uno doctor`: one check per prerequisite, each failure with the command that fixes it."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import config, roles as roles_mod, state, worktree
from .worker import agent_env

MIN_PYTHON = (3, 10)
OK, WARN, FAIL = "ok", "warn", "FAIL"


def check_python():
    v = sys.version_info
    if v >= MIN_PYTHON:
        return OK, "python", f"{v.major}.{v.minor}.{v.micro} at {sys.executable}", ""
    return FAIL, "python", f"{v.major}.{v.minor} is too old", \
        f"install Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ and reinstall: pipx reinstall uno-orchestra --python python3.12"


def check_git(which=shutil.which):
    exe = which("git")
    if not exe:
        return FAIL, "git", "not found on PATH", "install git (macOS: xcode-select --install; Debian: apt install git)"
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()
    return OK, "git", out, ""


def check_claude(which=shutil.which, env=None, state_dir=None):
    """The CLI, then the active auth mode: account (the CLI login) or api-key (uno config)."""
    exe = which("claude")
    if not exe:
        return FAIL, "claude CLI", "not found on PATH", \
            "install Claude Code: npm install -g @anthropic-ai/claude-code (or see https://docs.anthropic.com/claude-code)"
    environ = os.environ if env is None else env
    auth = config.auth_mode(state_dir) if state_dir is not None else "account"
    key_set = "ANTHROPIC_API_KEY" in environ
    if auth == "api-key":
        if key_set:
            return OK, "auth", "api-key: ANTHROPIC_API_KEY from the environment (agents run with --bare)", ""
        return FAIL, "auth", "api-key mode but ANTHROPIC_API_KEY is not set", \
            "export ANTHROPIC_API_KEY=...   or go back to your login: uno config set auth account"
    env = agent_env(environ, auth="account")
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30, env=env,
                           stdin=subprocess.DEVNULL)
        info = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info = {}
    ignored = ("; ANTHROPIC_API_KEY is set in the environment but IGNORED (uno config set auth api-key to use it)"
               if key_set else "")
    if info.get("loggedIn"):
        detail = f"account: logged in ({info.get('authMethod', '?')}, {info.get('email', '')})".replace(", )", ")")
        return (WARN if key_set else OK), "auth", detail + ignored, ""
    return FAIL, "auth", "account mode but the Claude CLI is not logged in" + ignored, \
        "run: claude auth login   (or, to use the key: uno config set auth api-key)"


def check_state_dir(state_dir):
    d = Path(state_dir)
    if not d.exists():
        return OK, "state dir", f"{d} (created on first use)", ""
    mode = stat.S_IMODE(d.stat().st_mode)
    loose = [str(d)] if mode & 0o077 else []
    loose += [str(p) for p in d.iterdir() if p.is_file() and stat.S_IMODE(p.stat().st_mode) & 0o077]
    if loose:
        return FAIL, "state dir", "readable by other users: " + ", ".join(loose), \
            f"chmod 700 {d} && chmod 600 {d}/*"
    if not os.access(d, os.W_OK):
        return FAIL, "state dir", f"{d} is not writable", f"chown $(id -un) {d} && chmod 700 {d}"
    return OK, "state dir", f"{d} (0700)", ""


def check_legacy_state(home=None):
    old = state.default_dir(home).with_name(state.LEGACY)
    new = state.default_dir(home)
    if old.is_dir() and new.exists():
        return WARN, "legacy state", f"{old} still exists next to {new}", \
            f"it was not migrated because {new} already existed; compare them and remove the old one: rm -r {old}"
    return OK, "legacy state", "none", ""


def check_project(path, state_dir):
    repo = worktree.git(path, "rev-parse", "--show-toplevel", check=False)
    if repo.returncode != 0:
        return [(FAIL, "project", f"{path} is not inside a git repository", "cd into your project and run: uno init  (it offers git init)")]
    repo = Path(repo.stdout.strip())
    out = []
    if roles_mod.has_legacy_dir(repo):
        out.append((WARN, "project", f"{roles_mod.LEGACY_DIR}/ is the pre-0.1.0 name", f"cd {repo} && uno init   (it renames it to {roles_mod.DIR}/)"))
    if not (repo / roles_mod.DIR).is_dir():
        out.append((FAIL, "project", f"no {roles_mod.DIR}/ in {repo}", f"cd {repo} && uno init"))
        return out
    try:
        roles = roles_mod.load_roles(repo)
        roles_mod.project_context(repo)
    except ValueError as exc:
        return out + [(FAIL, "project", str(exc), f"fix the file or regenerate: cd {repo} && uno init --force")]
    if not roles:
        out.append((FAIL, "project", f"{roles_mod.DIR}/ has no roles", f"cd {repo} && uno init --force"))
    else:
        out.append((OK, "project", f"{repo} with roles {', '.join(sorted(roles))}", ""))
    if not roles_mod.trusted(repo, state_dir):
        out.append((FAIL, "roles confirmed", "the roles were not confirmed from this machine",
                    "review them: uno ui -> Roles & project -> confirm   (or: uno init --force)"))
    pending = roles_mod.uncommitted(repo, worktree.git)
    if pending:
        out.append((WARN, "roles committed", "uncommitted: " + ", ".join(pending), f"git -C {repo} add {roles_mod.DIR} && git -C {repo} commit -m 'Add Uno roles'"))
    branch = worktree.git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if branch in ("main", "master"):
        out.append((WARN, "branch", f"on {branch}: runs need a feature branch", f"git -C {repo} checkout -b uno-run"))
    return out


def check_shadowing(which=shutil.which, argv0=None):
    argv0 = sys.argv[0] if argv0 is None else argv0
    found = which("uno")
    if not found:
        return FAIL, "uno on PATH", "`uno` is not on PATH", \
            "pipx ensurepath (then open a new shell), or add the directory that holds `uno` to PATH"
    if Path(argv0).name != "uno":
        return OK, "uno on PATH", f"{found} (this process was started as {argv0})", ""
    if Path(found).resolve() != Path(argv0).resolve():
        return FAIL, "uno on PATH", f"`uno` resolves to {found}, but this is {argv0}", \
            f"another `uno` shadows this one: rename or remove {found}, or put {Path(argv0).parent} first in PATH"
    return OK, "uno on PATH", found, ""


def run(path, state_dir, say=print, home=None, which=shutil.which, env=None, argv0=None):
    """Print every check and return the number of failures (the exit code)."""
    results = [check_python(), check_git(which), check_claude(which, env, state_dir), check_state_dir(state_dir),
               check_legacy_state(home), *check_project(path, state_dir), check_shadowing(which, argv0)]
    for status, name, detail, fix in results:
        say(f"[{status:>4}] {name}: {detail}")
        if fix:
            say(f"       fix: {fix}")
    fails = sum(1 for r in results if r[0] == FAIL)
    say("all checks passed" if not fails else f"{fails} check(s) failed")
    return fails
