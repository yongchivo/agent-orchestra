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

SKIP_DIRS = {"node_modules", "venv", "__pycache__", "dist", "build", "target", "vendor", "coverage",
             "Pods", "DerivedData", "out", "bin", "obj"}
LANGUAGES = {
    ".py": "Python", ".pyi": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".dart": "Dart", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".c": "C", ".h": "C", ".cpp": "C++", ".cc": "C++", ".hpp": "C++", ".m": "Objective-C",
    ".swift": "Swift", ".scala": "Scala", ".sh": "Shell", ".sql": "SQL", ".vue": "Vue", ".svelte": "Svelte",
    ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".md": "Markdown", ".yml": "YAML", ".yaml": "YAML",
}
DOC_LANGUAGES = {"Markdown", "YAML", "HTML", "CSS"}  # never the main language of a project
# (manifest at the top level, dependency manager). Every match is reported, in this order.
MANIFESTS = [("pubspec.yaml", "pub"), ("package.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
             ("pyproject.toml", "pip"), ("setup.py", "pip"), ("requirements.txt", "pip"), ("Pipfile", "pipenv"),
             ("poetry.lock", "poetry"), ("uv.lock", "uv"), ("Cargo.toml", "cargo"), ("go.mod", "go modules"),
             ("Gemfile", "bundler"), ("pom.xml", "maven"), ("build.gradle", "gradle"), ("build.gradle.kts", "gradle"),
             ("composer.json", "composer"), ("Podfile", "cocoapods"), ("*.csproj", "dotnet"), ("*.sln", "dotnet"),
             ("Package.swift", "swiftpm")]
LOCKFILES = ["pubspec.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock",
             "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "Podfile.lock"]
# Frameworks by structure: (name, top-level file or directory present, text that must appear in it or "")
FRAMEWORKS = [("Flutter", "pubspec.yaml", "sdk: flutter"), ("Next.js", "package.json", '"next"'),
              ("React", "package.json", '"react"'), ("Vue", "package.json", '"vue"'),
              ("Angular", "angular.json", ""), ("Svelte", "package.json", '"svelte"'),
              ("Express", "package.json", '"express"'), ("Django", "manage.py", ""),
              ("Flask", "requirements.txt", "flask"), ("FastAPI", "requirements.txt", "fastapi"),
              ("Flask", "pyproject.toml", "flask"), ("FastAPI", "pyproject.toml", "fastapi"),
              ("Rails", "Gemfile", "rails"), ("Laravel", "artisan", ""), ("Spring", "pom.xml", "spring"),
              ("Spring", "build.gradle", "spring"), ("Spring", "build.gradle.kts", "spring")]
SERVICES = [("Firebase", ("firebase.json", "firestore.rules", "storage.rules", ".firebaserc")),
            ("Supabase", ("supabase",)), ("Docker", ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml")),
            ("Terraform", ("main.tf",))]
FIREBASE_FILES = ["firebase.json", "firestore.rules", "storage.rules", "firestore.indexes.json", ".firebaserc",
                  "database.rules.json"]
FIREBASE_CONFIG = {"google-services.json", "GoogleService-Info.plist"}   # per-app Firebase project config
PLATFORM_DIRS = {"Flutter": ("ios", "android", "linux", "windows", "macos", "web")}  # generated, not the app's code
INSTRUCTION_WORDS = ("claude", "agent", "prompt", "scaffold")
TYPICAL_DIRS = ("src", "app", "lib", "pkg", "cmd", "tests", "test", "spec", "__tests__", "integration_test",
                "docs", ".github", "scripts", "android", "ios")
SOURCE_DIRS = ("src", "app", "lib", "pkg", "cmd")
TEST_DIRS = ("tests", "test", "spec", "__tests__", "integration_test")
TEST_GLOBS = {"Python": ["**/test_*.py", "**/*_test.py", "**/conftest.py"],
              "JavaScript": ["**/*.test.js", "**/*.spec.js"], "TypeScript": ["**/*.test.ts", "**/*.spec.ts", "**/*.test.tsx"],
              "Dart": ["**/*_test.dart"], "Go": ["**/*_test.go"], "Rust": ["tests/**"], "Java": ["src/test/**"],
              "Kotlin": ["src/test/**"], "Ruby": ["spec/**", "test/**"], "PHP": ["tests/**"], "C#": ["**/*Tests/**"],
              "Swift": ["Tests/**"]}
MAX_FILES = 50_000
MIN_SHELL_FILES = 3      # Shell is part of the stack only with this many .sh files outside scripts/
MAX_TITLE = 120
GITIGNORE_BLOCK = ["# Uno: run state (the roles in .uno/ are meant to be committed)", "events.jsonl", "events.jsonl.1"]


def _is_test(rel_dir, name):
    parts = Path(rel_dir).parts
    return (any(p in TEST_DIRS or "test" in p.lower() for p in parts[:1]) or name.startswith("test_")
            or ".test." in name or ".spec." in name or name.endswith(("_test.dart", "_test.go", "_test.py")))


def _contains(repo, name, text):
    path = Path(repo) / name
    if not path.is_file():
        return False
    if not text:
        return True
    try:
        return text in path.read_text(encoding="utf-8", errors="replace")[:200_000].lower()
    except OSError:
        return False


def _top_level(repo):
    """Manifests, frameworks and services from the top level only (a few small reads)."""
    top = {p.name for p in Path(repo).iterdir()}
    managers = []
    for pattern, manager in MANIFESTS:
        if ("*" in pattern and any(Path(n).match(pattern) for n in top)) or pattern in top:
            if manager not in managers:
                managers.append(manager)
    frameworks = list(dict.fromkeys(fw for fw, name, text in FRAMEWORKS if _contains(repo, name, text.lower())))
    services = [svc for svc, names in SERVICES if any(n in top for n in names)]
    return top, managers, frameworks, services


def analyze(repo):
    """Read-only facts about the repository. Hidden directories, dependency and build output
    directories and, for Flutter, the generated platform directories are left out of the
    language count (.github at the top level is still reported)."""
    repo = Path(repo)
    top, managers, frameworks, services = _top_level(repo)
    skip_top = {d for fw in frameworks for d in PLATFORM_DIRS.get(fw, ())}
    counts, files, test_files = collections.Counter(), 0, 0
    in_scripts = collections.Counter()   # files per language under the top-level scripts/
    logs, instructions, firebase_config = [], [], []
    for root, dirs, names in os.walk(repo):
        rel = Path(root).relative_to(repo)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                         and not (rel == Path(".") and d in skip_top))
        for name in names:
            files += 1
            relname = str(rel / name) if rel != Path(".") else name
            lang = LANGUAGES.get(Path(name).suffix.lower())
            if lang:
                counts[lang] += 1
                if rel.parts[:1] == ("scripts",):
                    in_scripts[lang] += 1
            if lang and lang not in DOC_LANGUAGES and _is_test(rel, name):
                test_files += 1
            low = name.lower()
            if low.endswith(".log"):
                logs.append(relname)
            elif low.endswith(".md") and name not in ("CLAUDE.md", "AGENTS.md") and any(w in low for w in INSTRUCTION_WORDS):
                instructions.append(relname)
            elif name in FIREBASE_CONFIG:
                firebase_config.append(relname)
        if files > MAX_FILES:
            break
    for d in skip_top:   # platform dirs are not counted, but their Firebase config files matter
        for name in FIREBASE_CONFIG:
            firebase_config += [str(p.relative_to(repo)) for p in (repo / d).rglob(name)]
    languages = [lang for lang, n in counts.most_common() if lang not in DOC_LANGUAGES
                 and not (lang == "Shell" and n - in_scripts[lang] < MIN_SHELL_FILES)]
    return {
        "languages": languages, "counts": dict(counts), "files": files,
        "dirs": [d for d in TYPICAL_DIRS if (repo / d).is_dir()],
        "test_dirs": sorted(d for d in top if (repo / d).is_dir() and not d.startswith(".")
                            and (d in TEST_DIRS or "test" in d.lower())),
        "package_managers": managers, "package_manager": managers[0] if managers else None,
        "frameworks": frameworks, "services": services, "platform_dirs": sorted(d for d in skip_top if (repo / d).is_dir()),
        "has_tests": test_files > 0, "test_files": test_files,
        "readme": next((f for f in sorted(top) if f.lower().startswith("readme")), None),
        "claude_md": "CLAUDE.md" in top, "agents_md": "AGENTS.md" in top,
        "instruction_files": sorted(instructions), "log_files": sorted(logs),
        "firebase_config_files": sorted(set(firebase_config)),
    }


def unignored_logs(repo, analysis):
    """*.log files git does not ignore (tracked or untracked-and-not-ignored)."""
    if not analysis["log_files"]:
        return []
    out = worktree.git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                       *analysis["log_files"], check=False).stdout
    return sorted(p for p in out.split("\0") if p)


def propose_protected(repo, analysis):
    """Files of this project that always need review: service rules and config, lockfiles and
    manifests of the detected managers, and (Flutter) the platform directories."""
    repo = Path(repo)
    out = []
    if "Firebase" in analysis["services"]:
        out += [f for f in FIREBASE_FILES if (repo / f).is_file()]
    out += analysis["firebase_config_files"]
    if "Flutter" in analysis["frameworks"]:
        out += [f"{d}/**" for d in ("android", "ios", "macos", "linux", "windows") if (repo / d).is_dir()]
        out += [f for f in ("pubspec.yaml", "pubspec.lock") if (repo / f).is_file()]
    out += [f for f in LOCKFILES if (repo / f).is_file() and f not in out]
    return list(dict.fromkeys(out))


def warnings(repo, analysis):
    out = []
    logs = unignored_logs(repo, analysis)
    if logs:
        out.append(f"{len(logs)} .log file(s) are not ignored by git ({', '.join(logs[:5])}"
                   + (", ..." if len(logs) > 5 else "") + "): add `*.log` to .gitignore so a run does not see a dirty tree")
    if analysis["firebase_config_files"]:
        out.append("Firebase app configuration found (" + ", ".join(analysis["firebase_config_files"])
                   + "): it identifies your Firebase project; it is proposed as protected and should stay out of the agents' paths")
    return out


def _ext_globs(languages):
    return sorted({f"**/*{ext}" for ext, lang in LANGUAGES.items() if lang in languages})


def _security_focus(analysis):
    """Extra, project-specific instructions for the security analyst."""
    focus = []
    if "Firebase" in analysis["services"]:
        focus.append("Firebase: review firestore.rules and storage.rules for access without authentication, "
                     "`allow ... if true`, missing data validation, and reads or writes across users "
                     "(a user reaching another user's documents or files); check that no service account "
                     "or API key is committed.")
    if "Flutter" in analysis["frameworks"] or any(d in analysis["dirs"] for d in ("android", "ios")):
        focus.append("Mobile: review the permissions declared in AndroidManifest.xml and Info.plist and "
                     "whether the code needs them; flag secrets, signing material or identifiers in the "
                     "platform directories.")
    if "Docker" in analysis["services"]:
        focus.append("Containers: review Dockerfiles and compose files for secrets, root users and exposed ports.")
    return " Project-specific focus: " + " ".join(focus) if focus else ""


def propose_roles(analysis):
    """Four roles whose paths and presets follow what was detected. Reviewers are read-only."""
    langs = analysis["languages"][:3]
    src_dirs = [d for d in analysis["dirs"] if d in SOURCE_DIRS]
    dev_paths = [f"{d}/**" for d in src_dirs] or _ext_globs(langs) or ["**"]
    test_paths = [f"{d}/**" for d in analysis["test_dirs"]] + [g for lang in langs for g in TEST_GLOBS.get(lang, [])]
    test_paths = list(dict.fromkeys(test_paths)) or ["tests/**"]
    templates = {t.name: t for t in roles_mod.TEMPLATES}
    stack = ", ".join(analysis["frameworks"][:2] + langs) or "the project's language"
    manager = analysis["package_manager"]
    tester_hint = ("Extend the existing test suite" if analysis["has_tests"]
                   else "The project has no tests yet: create them under " + test_paths[0].split("/")[0] + "/")
    sec = templates["security-analyst"]
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
        Role(**{**sec.__dict__, "instructions": (sec.instructions + _security_focus(analysis))[:roles_mod.MAX_TEXT["instructions"]]}),
        templates["architect"],
    ]


def doc_title(content):
    """The first markdown heading, else the first non-empty line: what the document is about."""
    for line in content.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip().rstrip(".")[:MAX_TITLE]
    return next((line.strip().rstrip(".")[:MAX_TITLE] for line in content.splitlines() if line.strip()), "")


def _cut_at_section(content, limit):
    """Cut before `limit` at the last markdown heading, else at the last line break."""
    head = content[:limit]
    at = head.rfind("\n#")
    if at <= 0:
        at = head.rfind("\n")
    return head[:at] if at > 0 else head


def project_context(analysis, extra_docs=()):
    """Deterministic short summary from the analysis, plus the instruction files the user chose:
    referenced by path (`Read X when you need: <title>`) or embedded. Returns (text, truncated):
    an embedded document that does not fit is cut at a section boundary, a marker says how much
    was left out, and `truncated` maps the document to the characters omitted."""
    langs = ", ".join(f"{lang} ({analysis['counts'][lang]} files)" for lang in analysis["languages"][:5]) or "none detected"
    lines = [f"Languages: {langs}."]
    if analysis["frameworks"] or analysis["services"]:
        lines.append("Stack: " + ", ".join(analysis["frameworks"] + analysis["services"]) + ".")
    lines += [f"Layout: {', '.join(d + '/' for d in analysis['dirs']) or 'flat'}"
              + (f" (platform directories {', '.join(analysis['platform_dirs'])} are generated)" if analysis["platform_dirs"] else "") + ".",
              f"Dependencies: {', '.join(analysis['package_managers']) or 'no manifest found'}.",
              f"Tests: {'yes (' + str(analysis['test_files']) + ' files)' if analysis['has_tests'] else 'none found'}."]
    text = " ".join(lines)
    truncated = {}
    for name, content, mode in extra_docs:
        if mode == "reference":
            text += f"\nRead {name} when you need: {doc_title(content) or 'its contents'}."
            continue
        header = f"\n\nFrom {name}:\n"
        marker = f"\n[... {{n}} characters of {name} omitted: Read {name} for the rest]"
        room = MAX_CONTEXT - len(text) - len(header)
        if len(content) <= room:
            text += header + content
            continue
        kept = _cut_at_section(content, max(0, room - len(marker) - 12))
        omitted = len(content) - len(kept)
        truncated[name] = omitted
        if kept:
            text += header + kept + marker.format(n=omitted)
        else:
            text += f"\n[{name} not embedded: no room left ({omitted} characters); Read {name} when you need: {doc_title(content)}]"
    assert len(text) <= MAX_CONTEXT, len(text)
    return text, truncated


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
    say(f"Detected: {project_context(analysis)[0]}")
    for w in warnings(repo, analysis):
        say(f"warning: {w}")
    say("Proposed roles:")
    roles = review_roles(propose_roles(analysis), ask, say)
    if not roles:
        say("No roles kept; nothing was written.")
        return 1
    protected = propose_protected(repo, analysis)
    if protected:
        say("Protected paths of this project (a change to them always needs your review, even inside a role's paths):")
        say("  " + " ".join(protected))
        protected = roles_mod.check_protected_paths(ask("    protected paths (space-separated; empty for none)", " ".join(protected)).split())
    docs = []
    for name in ([n for n in ("CLAUDE.md", "AGENTS.md") if analysis[n[:-3].lower() + "_md"]] + analysis["instruction_files"]):
        try:
            roles_mod.check_references(repo, [name], protected)
        except ValueError as exc:
            say(f"warning: {name} skipped: {exc}")
            continue
        content = (repo / name).read_text(encoding="utf-8", errors="replace")
        room = MAX_CONTEXT - len(project_context(analysis, docs)[0]) - len(name) - 12
        fits = len(content) <= room
        answer = ask(f"{name} looks like instructions for agents ({len(content)} characters"
                     + ("" if fits else f", more than the {room} left in the project context") + "). "
                     "[r]eference it by path (agents can Read it), [e]mbed it in the context, or [s]kip?",
                     "e" if fits else "r").strip().lower()[:1]
        if answer in ("r", "e"):
            docs.append((name, content, "reference" if answer == "r" else "embed"))
    text, truncated = project_context(analysis, docs)
    for name, n in truncated.items():
        say(f"warning: {name} did not fit: cut at a section boundary, {n} characters omitted "
            f"(the context says so; agents can Read the file). Prefer referencing it by path.")
    for role in roles:
        roles_mod.save_role(repo, role)
    roles_mod.save_project_context(repo, text)
    roles_mod.save_protected_paths(repo, protected, truncated,
                                   references=[name for name, _, mode in docs if mode == "reference"])
    roles_mod.trust(repo, state_dir)   # written by the user, through this command: confirmed
    changed = ensure_gitignore(repo)
    say(f"Wrote {roles_mod.DIR}/project.json and {roles_mod.DIR}/roles/{{{','.join(r.name for r in roles)}}}.json"
        + (", updated .gitignore" if changed else "") + ".")
    say(f"Next: git add {roles_mod.DIR} .gitignore && git commit -m 'Add Uno roles'   (runs need a clean tree)")
    say("Then `uno` (or `uno ui`) to open the control page.")
    return 0
