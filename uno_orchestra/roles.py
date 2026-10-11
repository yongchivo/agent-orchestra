"""Roles: per-project agent profiles stored in <repo>/.uno/roles/<name>.json.

A role fixes what an agent is (instructions), what it may do (one of the core presets,
never free-text tools), where (default paths) and what it must return (text or JSON
findings). .uno/ also holds project.json with the project context. The directory is
protected: the merge gate never lets an agent's change to it through, and the agent's
settings deny Edit/Write on it as well.
"""
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .worker import MAX_TURNS, NAME_RE, PRESETS

DIR = ".uno"
LEGACY_DIR = ".orchestra"   # the name before 0.1.0; still protected, migrated by `uno init`
OUTPUTS = ("text", "findings")
ROLE_FIELDS = {"name", "description", "instructions", "preset", "paths", "output",
               "output_format", "max_turns"}
MAX_TEXT = {"description": 300, "instructions": 4000, "output_format": 1000}
MAX_PATHS = 20
MAX_CONTEXT = 4000
MAX_PROTECTED = 50


@dataclass
class Role:
    name: str
    description: str
    instructions: str
    preset: str
    paths: list
    output: str = "text"
    output_format: str = ""
    max_turns: int = 20

    def __post_init__(self):
        if not isinstance(self.name, str) or not NAME_RE.match(self.name):
            raise ValueError(f"invalid role name {self.name!r}: use a-z, 0-9 and '-'")
        for key, cap in MAX_TEXT.items():
            v = getattr(self, key)
            if not isinstance(v, str) or len(v) > cap:
                raise ValueError(f"role {self.name!r}: {key} must be a string of at most {cap} characters")
        if self.preset not in PRESETS:
            raise ValueError(f"role {self.name!r}: unknown preset {self.preset!r}: use one of {sorted(PRESETS)}")
        if self.output not in OUTPUTS:
            raise ValueError(f"role {self.name!r}: output must be one of {list(OUTPUTS)}")
        if (not isinstance(self.paths, list) or not 1 <= len(self.paths) <= MAX_PATHS
                or not all(isinstance(p, str) and p.strip() for p in self.paths)):
            raise ValueError(f"role {self.name!r}: paths must be 1..{MAX_PATHS} non-empty globs")
        for p in self.paths:
            if p.startswith("/") or ".." in p.split("/") or p.split("/")[0] in (DIR, LEGACY_DIR):
                raise ValueError(f"role {self.name!r}: path {p!r} must be relative, without '..' and outside {DIR}/")
        if (not isinstance(self.max_turns, int) or isinstance(self.max_turns, bool)
                or not 1 <= self.max_turns <= MAX_TURNS):
            raise ValueError(f"role {self.name!r}: max_turns must be 1..{MAX_TURNS}")


def role_from_json(data):
    """Strict: an object with the known fields only. A tampered file fails here."""
    if not isinstance(data, dict):
        raise ValueError("a role must be a JSON object")
    unknown = sorted(set(data) - ROLE_FIELDS)
    if unknown:
        raise ValueError(f"role: unknown fields {unknown}")
    for key in ("name", "description", "instructions", "preset", "paths"):
        if key not in data:
            raise ValueError(f"role: '{key}' is required")
    return Role(**data)


def has_legacy_dir(repo):
    """A pre-0.1.0 .orchestra/ that `uno init` can rename to .uno/."""
    return (Path(repo) / LEGACY_DIR).is_dir() and not (Path(repo) / DIR).exists()


def roles_dir(repo):
    return Path(repo) / DIR / "roles"


def snapshot(repo):
    """Every file under .orchestra/ read once: {relative path: bytes}. Roles, context and the
    fingerprint are all taken from this same dict, so what is hashed is what is used."""
    root = Path(repo) / DIR
    if not root.is_dir():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def roles_from(files):
    """{name: Role} from a snapshot. Any invalid file is an error naming it."""
    out = {}
    for rel, data in files.items():
        if not (rel.startswith("roles/") and rel.endswith(".json") and rel.count("/") == 1):
            continue
        try:
            role = role_from_json(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"{DIR}/{rel}: {exc}") from exc
        if role.name != rel[len("roles/"):-len(".json")]:
            raise ValueError(f"{DIR}/{rel}: file name and role name differ")
        out[role.name] = role
    return out


def load_roles(repo):
    return roles_from(snapshot(repo))


def save_role(repo, role):
    d = roles_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{role.name}.json").write_text(json.dumps(asdict(role), indent=2) + "\n", encoding="utf-8")


def delete_role(repo, name):
    if not NAME_RE.match(name):
        raise ValueError("invalid role name")
    path = roles_dir(repo) / f"{name}.json"
    if not path.is_file():
        raise ValueError("no such role")
    path.unlink()


def _project_from(files):
    data = files.get("project.json")
    if data is None:
        return {}
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{DIR}/project.json: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{DIR}/project.json: must be a JSON object")
    return obj


def project_context_from(files):
    ctx = _project_from(files).get("context", "")
    if not isinstance(ctx, str):
        raise ValueError(f"{DIR}/project.json: 'context' must be a string")
    return ctx[:MAX_CONTEXT]


def check_protected_paths(paths):
    """Globs of this project's files that always need review, on top of scope.PROTECTED.
    They can only add restrictions, so they are read even before the user confirms .uno/."""
    if (not isinstance(paths, list) or len(paths) > MAX_PROTECTED
            or not all(isinstance(p, str) and p.strip() and len(p) <= 300 for p in paths)):
        raise ValueError(f"protected_paths must be a list of at most {MAX_PROTECTED} globs")
    for p in paths:
        if p.startswith("/") or ".." in p.split("/"):
            raise ValueError(f"protected path {p!r} must be relative and without '..'")
    return list(dict.fromkeys(p.strip() for p in paths))


def project_protected_from(files):
    return check_protected_paths(_project_from(files).get("protected_paths", []))


def project_truncated_from(files):
    """{document: characters omitted} when `uno init` had to cut an embedded document."""
    data = _project_from(files).get("truncated", {})
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0
                                             for k, v in data.items()):
        raise ValueError(f"{DIR}/project.json: 'truncated' must map document names to character counts")
    return {k[:300]: v for k, v in list(data.items())[:MAX_PROTECTED]}


def project_context(repo):
    return project_context_from(snapshot(repo))


def project_protected(repo):
    try:
        return project_protected_from(snapshot(repo))
    except ValueError:
        return []   # a broken file protects nothing extra; load_confirmed() reports it


def _save_project(repo, **fields):
    """Update project.json field by field: the UI's context editor never drops protected_paths."""
    d = Path(repo) / DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / "project.json"
    try:
        data = _project_from({"project.json": path.read_bytes()}) if path.is_file() else {}
    except ValueError:
        data = {}
    data.update(fields)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def save_project_context(repo, text):
    if not isinstance(text, str) or len(text) > MAX_CONTEXT:
        raise ValueError(f"project context must be a string of at most {MAX_CONTEXT} characters")
    _save_project(repo, context=text)


def save_protected_paths(repo, paths, truncated=None):
    _save_project(repo, protected_paths=check_protected_paths(paths), truncated=dict(truncated or {}))


FINDINGS_FORMAT = ('JSON only: {"findings": [{"severity": "low|medium|high", "file": "path", '
                   '"line": 0, "description": "...", "recommendation": "..."}]}. '
                   "An empty list means nothing to report.")

TEMPLATES = [
    Role(
        name="developer",
        description="Implements the requested change inside its paths. Never commits.",
        instructions=(
            "Implement the task completely and minimally, following the project's existing style. "
            "Keep the public API unless the task says otherwise. Only change files inside your "
            "paths; never touch .uno/, CI, build or dependency files, and do not commit. "
            "When you receive review findings, fix every medium and high finding that is correct "
            "and say briefly why you skipped any that is not."),
        preset="edit", paths=["**"], output="text",
        output_format="A short summary: what you changed, file by file, and what reviewers should look at.",
        max_turns=25),
    Role(
        name="security-analyst",
        description="Reviews the change as an application security analyst. Read-only.",
        instructions=(
            "Review the diff you are given and the files it touches in the working tree. Look for "
            "injection, path traversal, command execution, unsafe deserialization, secrets, weak "
            "crypto, missing validation at trust boundaries, insecure defaults and dependency risks. "
            "Report only what is in the change or directly affected by it. Severity: high = "
            "exploitable or leaks a secret; medium = weakness to fix before merging; low = "
            "hardening advice. Do not modify any file."),
        preset="readonly", paths=["**"], output="findings", output_format=FINDINGS_FORMAT,
        max_turns=15),
    Role(
        name="architect",
        description="Reviews design and structure: boundaries, duplication, API consistency, tests. Read-only.",
        instructions=(
            "Review the diff you are given for design and structure: module boundaries, "
            "duplication, naming, error handling, API consistency, tests, and whether the change "
            "fits the project's architecture. Each observation needs a concrete recommendation. "
            "Severity: high = breaks a design rule or the public API; medium = should change "
            "before merging; low = suggestion. Do not modify any file."),
        preset="readonly", paths=["**"], output="findings", output_format=FINDINGS_FORMAT,
        max_turns=15),
]


def install_templates(repo):
    """Write the example roles that do not exist yet. Returns the names written."""
    existing = {p.stem for p in roles_dir(repo).glob("*.json")}
    written = []
    for role in TEMPLATES:
        if role.name not in existing:
            save_role(repo, role)
            written.append(role.name)
    return written


def uncommitted(repo, git):
    """Paths under .uno/ that are not committed (the run preflight needs a clean tree)."""
    out = git(repo, "status", "--porcelain", "--", DIR).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]




# --- trust: roles the user has seen and confirmed --------------------------------------------
# A repository can ship any .uno/ it likes, and its instructions reach the model. The UI
# only uses roles and a project context the user created there or explicitly confirmed; the
# fingerprint lives in the state directory, outside the repository.

def fingerprint_of(files):
    """sha256 over a snapshot (relative path and bytes), or "" when there is nothing."""
    if not files:
        return ""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + files[rel] + b"\0")
    return h.hexdigest()


def fingerprint(repo):
    return fingerprint_of(snapshot(repo))


def _trust_path(state_dir):
    return Path(state_dir) / "trust.json"


def _trust_load(state_dir):
    try:
        data = json.loads(_trust_path(state_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def trusted(repo, state_dir, files=None):
    """True when .uno/ is absent or matches what the user last confirmed."""
    fp = fingerprint_of(snapshot(repo) if files is None else files)
    return fp == "" or _trust_load(state_dir).get(str(Path(repo).resolve())) == fp


def load_confirmed(repo, state_dir):
    """(roles, project context, fingerprint) from ONE snapshot, only if that very snapshot is
    the one the user confirmed. Raises ValueError(UNCONFIRMED) otherwise."""
    files = snapshot(repo)
    if not trusted(repo, state_dir, files):
        raise ValueError(UNCONFIRMED)
    project_protected_from(files)   # validated here so a broken list is an error, not silence
    return roles_from(files), project_context_from(files), fingerprint_of(files)


def trust(repo, state_dir):
    """Record the current .uno/ as confirmed (0600, in the state directory)."""
    data = _trust_load(state_dir)
    data[str(Path(repo).resolve())] = fingerprint(repo)
    path = _trust_path(state_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


UNCONFIRMED = ("the roles and project context in .uno/ were not created from this UI, or changed "
               "since you confirmed them: review them in Roles & project and confirm before a run")
