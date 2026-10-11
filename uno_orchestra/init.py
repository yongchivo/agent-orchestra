"""`uno init`: set a repository up for Uno without touching anything but .uno/ and .gitignore.

The analysis is deterministic and read-only (extensions, well-known directories, manifest
files); no model is involved. The proposed roles are shown one by one and the user accepts,
edits the paths or removes each. What is written is recorded as confirmed by the user
(roles.trust), the same mechanism the UI uses.
"""
import collections
import os
from pathlib import Path

from . import roles as roles_mod, worktree
from .roles import MAX_CONTEXT, Role

SKIP_DIRS = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "vendor", "coverage"}
LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust",
    ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".swift": "Swift", ".sh": "Shell", ".sql": "SQL",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML",
}
DOC_LANGUAGES = {"Markdown", "YAML", "HTML", "CSS"}  # never the main language of a project
MANAGERS = [("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("package-lock.json", "npm"),
            ("package.json", "npm"), ("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile", "pipenv"),
            ("pyproject.toml", "pip"), ("setup.py", "pip"), ("requirements.txt", "pip"),
            ("Cargo.toml", "cargo"), ("go.mod", "go modules"), ("Gemfile", "bundler"), ("pom.xml", "maven"),
            ("build.gradle", "gradle"), ("build.gradle.kts", "gradle"), ("composer.json", "composer")]
TYPICAL_DIRS = ("src", "app", "lib", "pkg", "cmd", "tests", "test", "spec", "__tests__", "docs", ".github", "scripts")
SOURCE_DIRS = ("src", "app", "lib", "pkg", "cmd")
TEST_DIRS = ("tests", "test", "spec", "__tests__")
TEST_GLOBS = {"Python": ["**/test_*.py", "**/*_test.py", "**/conftest.py"],
              "JavaScript": ["**/*.test.js", "**/*.spec.js"], "TypeScript": ["**/*.test.ts", "**/*.spec.ts", "**/*.test.tsx"],
              "Go": ["**/*_test.go"], "Rust": ["tests/**"], "Java": ["src/test/**"], "Kotlin": ["src/test/**"],
              "Ruby": ["spec/**", "test/**"], "PHP": ["tests/**"], "C#": ["**/*Tests/**"], "Swift": ["Tests/**"]}
MAX_FILES = 50_000
GITIGNORE_BLOCK = ["# Uno: run state (the roles in .uno/ are meant to be committed)", "events.jsonl", "events.jsonl.1"]


def _is_test(rel_dir, name):
    parts = Path(rel_dir).parts
    return any(p in TEST_DIRS for p in parts) or name.startswith("test_") or ".test." in name or ".spec." in name


def analyze(repo):
    """Read-only facts about the repository. Hidden directories are skipped (except that
    .github at the top level is reported)."""
    repo = Path(repo)
    counts, files, test_files = collections.Counter(), 0, 0
    for root, dirs, names in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        rel = Path(root).relative_to(repo)
        for name in names:
            files += 1
            lang = LANGUAGES.get(Path(name).suffix.lower())
            if lang:
                counts[lang] += 1
            if lang and lang not in DOC_LANGUAGES and _is_test(rel, name):
                test_files += 1
        if files > MAX_FILES:
            break
    top = {p.name for p in repo.iterdir()}
    languages = [lang for lang, _ in counts.most_common() if lang not in DOC_LANGUAGES]
    return {
        "languages": languages, "counts": dict(counts), "files": files,
        "dirs": [d for d in TYPICAL_DIRS if (repo / d).is_dir()],
        "package_manager": next((m for f, m in MANAGERS if f in top), None),
        "has_tests": test_files > 0, "test_files": test_files,
        "readme": next((f for f in sorted(top) if f.lower().startswith("readme")), None),
        "claude_md": "CLAUDE.md" in top, "agents_md": "AGENTS.md" in top,
    }


def _ext_globs(languages):
    return sorted({f"**/*{ext}" for ext, lang in LANGUAGES.items() if lang in languages})


def propose_roles(analysis):
    """Four roles whose paths and presets follow what was detected. Reviewers are read-only."""
    langs = analysis["languages"][:3]
    src_dirs = [d for d in analysis["dirs"] if d in SOURCE_DIRS]
    test_dirs = [d for d in analysis["dirs"] if d in TEST_DIRS]
    dev_paths = [f"{d}/**" for d in src_dirs] or _ext_globs(langs) or ["**"]
    test_paths = [f"{d}/**" for d in test_dirs] + [g for lang in langs for g in TEST_GLOBS.get(lang, [])]
    test_paths = list(dict.fromkeys(test_paths)) or ["tests/**"]
    templates = {t.name: t for t in roles_mod.TEMPLATES}
    stack = ", ".join(langs) or "the project's language"
    manager = analysis["package_manager"]
    tester_hint = ("Extend the existing test suite" if analysis["has_tests"]
                   else "The project has no tests yet: create them under " + test_paths[0].split("/")[0] + "/")
    return [
        Role(name="developer", description=templates["developer"].description,
             instructions=templates["developer"].instructions, preset="edit", paths=dev_paths,
             output="text", output_format=templates["developer"].output_format, max_turns=25),
        Role(name="tester",
             description=f"Writes and updates tests ({stack}) for the change it is given. Never commits.",
             instructions=(f"{tester_hint}, following the project's existing test framework and style"
                           + (f" ({manager} project)" if manager else "") + ". Cover the behaviour described "
                           "in the task and the diff you are given: expected cases, edge cases and failure "
                           "cases. Only change files inside your paths; never touch .uno/, CI, build or "
                           "dependency files, and do not commit. Do not weaken or delete existing tests to "
                           "make them pass."),
             preset="edit", paths=test_paths, output="text",
             output_format="A short summary: which tests you added or changed and what each one covers.",
             max_turns=20),
        templates["security-analyst"],
        templates["architect"],
    ]


def project_context(analysis, claude_md=None):
    """Deterministic context from the analysis, plus CLAUDE.md when the user wants it."""
    langs = ", ".join(f"{lang} ({analysis['counts'][lang]} files)" for lang in analysis["languages"][:5]) or "none detected"
    lines = [f"Languages: {langs}.",
             f"Layout: {', '.join(d + '/' for d in analysis['dirs']) or 'flat'}.",
             f"Dependencies: {analysis['package_manager'] or 'no manifest found'}.",
             f"Tests: {'yes (' + str(analysis['test_files']) + ' files)' if analysis['has_tests'] else 'none found'}."]
    text = " ".join(lines)
    if claude_md:
        text += "\n\nFrom CLAUDE.md:\n" + claude_md
    return text[:MAX_CONTEXT]


def describe(role):
    return (f"  {role.name}: {role.description}\n"
            f"    preset {role.preset} ({'can edit' if role.preset == 'edit' else 'read-only'}), "
            f"paths {' '.join(role.paths)}, output {role.output}, max_turns {role.max_turns}")


def review_roles(proposed, ask, say):
    """Show each role; the user accepts it, edits its paths or removes it."""
    kept = []
    for role in proposed:
        say(describe(role))
        while True:
            answer = ask(f"    [a]ccept, [e]dit paths or [r]emove {role.name}?", "a").strip().lower()[:1]
            if answer == "r":
                break
            if answer == "e":
                raw = ask("    paths (space-separated globs)", " ".join(role.paths)).split()
                try:
                    role = Role(**{**role.__dict__, "paths": raw})
                except ValueError as exc:
                    say(f"    {exc}")
                    continue
                say(f"    paths now {' '.join(role.paths)}")
            kept.append(role)
            break
    return kept


def ensure_gitignore(repo):
    """Append the run-state entries that are missing. Returns True if the file changed."""
    path = Path(repo) / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {line.strip() for line in existing.splitlines()}
    missing = [line for line in GITIGNORE_BLOCK[1:] if line not in have]
    if not missing:
        return False
    block = "\n".join([GITIGNORE_BLOCK[0], *missing]) + "\n"
    path.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + block, encoding="utf-8")
    return True


def git_root(path):
    r = worktree.git(path, "rev-parse", "--show-toplevel", check=False)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else None


def yes(answer, default=True):
    a = answer.strip().lower()
    return default if not a else a in ("y", "yes", "s", "si", "sí")


def migrate_legacy_dir(repo, ask, say):
    """Rename a pre-0.1.0 .orchestra/ to .uno/ after confirmation. Returns True if renamed."""
    if not roles_mod.has_legacy_dir(repo):
        return False
    if not yes(ask(f"Found {roles_mod.LEGACY_DIR}/ (the name before 0.1.0). Rename it to {roles_mod.DIR}/?", "y")):
        say(f"Left as is. Roles are read from {roles_mod.DIR}/ only; {roles_mod.LEGACY_DIR}/ stays protected.")
        return False
    old, new = Path(repo) / roles_mod.LEGACY_DIR, Path(repo) / roles_mod.DIR
    tracked = worktree.git(repo, "ls-files", "--error-unmatch", "--", roles_mod.LEGACY_DIR, check=False).returncode == 0
    if tracked:
        worktree.git(repo, "mv", roles_mod.LEGACY_DIR, roles_mod.DIR)
    else:
        old.rename(new)
    say(f"Renamed {roles_mod.LEGACY_DIR}/ to {roles_mod.DIR}/" + (" (git mv; commit it)." if tracked else "."))
    return True


def run(path, state_dir, ask=None, say=print, force=False):
    """The whole `uno init`. `ask(question, default) -> str` lets tests drive it; the default
    reads stdin. Returns 0 on success, 1 when the user declined something required."""
    ask = ask or (lambda q, d: input(f"{q} [{d}] ") or d)
    path = Path(path).resolve()
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    repo = git_root(path)
    if repo is None:
        if not yes(ask(f"{path} is not a git repository. Run `git init` and make an initial commit?", "y")):
            say("Uno needs a git repository (agents work in worktrees). Nothing was changed.")
            return 1
        worktree.git(path, "init", "-q")
        worktree.git(path, "add", "-A")
        if worktree.git(path, "status", "--porcelain").stdout.strip():
            ident = [] if worktree.git(path, "config", "user.email", check=False).stdout.strip() else worktree.IDENT
            worktree.git(path, *ident, "commit", "-q", "-m", "Initial commit")
        repo = path
        say(f"Initialised a git repository in {repo}.")
    elif repo != path:
        say(f"Using the repository root {repo}.")

    migrate_legacy_dir(repo, ask, say)
    uno_dir = repo / roles_mod.DIR
    if uno_dir.is_dir() and any(uno_dir.rglob("*.json")):
        try:
            existing = roles_mod.load_roles(repo)
            problem = None
        except ValueError as exc:
            existing, problem = {}, str(exc)
        trusted = roles_mod.trusted(repo, state_dir)
        say(f"{roles_mod.DIR}/ already exists with roles: {', '.join(sorted(existing)) or 'none'}"
            + (f" (invalid: {problem})" if problem else "") + ("" if trusted else " (not yet confirmed by you)"))
        if not force:
            changed = ensure_gitignore(repo)
            say("Nothing to do. Use --force to regenerate the proposed roles."
                + (" .gitignore updated." if changed else ""))
            if not trusted and not problem:
                say("To confirm these roles: `uno ui` -> Roles & project -> confirm, or `uno init --force`.")
            return 0
        if not yes(ask("Regenerate the proposed roles and project context (other roles are kept)?", "n"), default=False):
            say("Nothing was changed.")
            return 1

    analysis = analyze(repo)
    say(f"Detected: {project_context(analysis)}")
    say("Proposed roles:")
    roles = review_roles(propose_roles(analysis), ask, say)
    if not roles:
        say("No roles kept; nothing was written.")
        return 1
    claude_md = None
    if analysis["claude_md"]:
        if yes(ask("CLAUDE.md found. Use it as the project context for the planner and the roles?", "y")):
            claude_md = (repo / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    for role in roles:
        roles_mod.save_role(repo, role)
    roles_mod.save_project_context(repo, project_context(analysis, claude_md))
    roles_mod.trust(repo, state_dir)   # written by the user, through this command: confirmed
    changed = ensure_gitignore(repo)
    say(f"Wrote {roles_mod.DIR}/project.json and {roles_mod.DIR}/roles/{{{','.join(r.name for r in roles)}}}.json"
        + (", updated .gitignore" if changed else "") + ".")
    say(f"Next: git add {roles_mod.DIR} .gitignore && git commit -m 'Add Uno roles'   (runs need a clean tree)")
    say("Then `uno` (or `uno ui`) to open the control page.")
    return 0
