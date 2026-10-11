"""Deterministic merge gate: what an agent changed versus what its task allows.

This does not depend on the model behaving: it looks at the git diff only.
"""
import re

from .bus import SECRET_RE

# Never merged automatically, even when a task's "paths" cover them.
PROTECTED = [".github/", ".claude/", ".uno/", ".orchestra/", "CLAUDE.md", ".git*", ".gitattributes", ".gitmodules",
             "Makefile", "*.sh", "setup.py", "pyproject.toml", "package.json",
             "requirements*.txt", "Dockerfile"]
MAX_NEW_BINARY_BYTES = 1 << 20
MAX_SCAN_BYTES = 1 << 20
SYMLINK_MODE = "120000"
ENV_FILE_RE = re.compile(r"^\.env(\..*)?$")


def _glob_re(pattern):
    """gitignore-like glob: `**` crosses directories, `*` and `?` stay inside one segment."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def in_scope(path, globs):
    return any(_glob_re(g).match(path) for g in globs)


def is_protected(path):
    parts = path.split("/")
    for pat in PROTECTED:
        if pat.endswith("/"):
            if pat[:-1] in parts[:-1]:
                return True
        elif _glob_re(pat).match(parts[-1]):
            return True
    return False


def secret_lines(text):
    """1-based line numbers that look like they hold a secret. The secret itself is not returned."""
    return [i for i, line in enumerate(text.splitlines(), 1) if SECRET_RE.search(line)]


def check(changes, globs, read=None):
    """Return (verdict, detail): verdict is "ok", "out_of_scope" or "needs_review".

    `read(path)` returns the file's new content as bytes; when given, text files are
    scanned for secrets with the same patterns the event log redacts.
    """
    outside = [c["path"] for c in changes if not in_scope(c["path"], globs)]
    if outside:
        return "out_of_scope", "outside task paths: " + ", ".join(sorted(outside))
    review = []
    for c in changes:
        name = c["path"].rsplit("/", 1)[-1]
        if is_protected(c["path"]):
            review.append(f"{c['path']} (protected)")
        elif c["mode"] == SYMLINK_MODE:
            review.append(f"{c['path']} (symlink)")
        elif c["status"] == "A" and c["binary"] and c["size"] > MAX_NEW_BINARY_BYTES:
            review.append(f"{c['path']} (new binary, {c['size'] // 1024} KiB)")
        elif ENV_FILE_RE.match(name) and c["status"] != "D":
            review.append(f"{c['path']} (.env file)")
        elif read and c["status"] != "D" and not c["binary"] and c["size"] <= MAX_SCAN_BYTES:
            for line in secret_lines(read(c["path"]).decode("utf-8", errors="replace")):
                review.append(f"{c['path']}:{line} (possible secret)")
    if review:
        return "needs_review", "manual review required: " + ", ".join(sorted(review))
    return "ok", ""
