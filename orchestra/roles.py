"""Roles: per-project agent profiles stored in <repo>/.orchestra/roles/<name>.json.

A role fixes what an agent is (instructions), what it may do (one of the core presets,
never free-text tools), where (default paths) and what it must return (text or JSON
findings). .orchestra/ also holds project.json with the project context. The directory is
protected: the merge gate never lets an agent's change to it through, and the agent's
settings deny Edit/Write on it as well.
"""
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .worker import MAX_TURNS, NAME_RE, PRESETS

DIR = ".orchestra"
OUTPUTS = ("text", "findings")
ROLE_FIELDS = {"name", "description", "instructions", "preset", "paths", "output",
               "output_format", "max_turns"}
MAX_TEXT = {"description": 300, "instructions": 4000, "output_format": 1000}
MAX_PATHS = 20
MAX_CONTEXT = 4000


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
            if p.startswith("/") or ".." in p.split("/") or p.split("/")[0] == DIR:
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


def roles_dir(repo):
    return Path(repo) / DIR / "roles"


def load_roles(repo):
    """{name: Role} from .orchestra/roles/*.json. Any invalid file is an error naming it."""
    out = {}
    for path in sorted(roles_dir(repo).glob("*.json")):
        try:
            role = role_from_json(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            raise ValueError(f"{DIR}/roles/{path.name}: {exc}") from exc
        if role.name != path.stem:
            raise ValueError(f"{DIR}/roles/{path.name}: file name and role name differ")
        out[role.name] = role
    return out


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


def project_context(repo):
    path = Path(repo) / DIR / "project.json"
    if not path.is_file():
        return ""
    try:
        ctx = json.loads(path.read_text(encoding="utf-8")).get("context", "")
    except (ValueError, AttributeError, OSError) as exc:
        raise ValueError(f"{DIR}/project.json: {exc}") from exc
    if not isinstance(ctx, str):
        raise ValueError(f"{DIR}/project.json: 'context' must be a string")
    return ctx[:MAX_CONTEXT]


def save_project_context(repo, text):
    if not isinstance(text, str) or len(text) > MAX_CONTEXT:
        raise ValueError(f"project context must be a string of at most {MAX_CONTEXT} characters")
    d = Path(repo) / DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(json.dumps({"context": text}, indent=2) + "\n", encoding="utf-8")


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
            "paths; never touch .orchestra/, CI, build or dependency files, and do not commit. "
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
    """Paths under .orchestra/ that are not committed (the run preflight needs a clean tree)."""
    out = git(repo, "status", "--porcelain", "--", DIR).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


