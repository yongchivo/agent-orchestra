"""Local state directory: ~/.uno-orchestra (0700; token, trust.json, recent repos, worktrees).

Before 0.1.0 it was ~/.agent-orchestra. The first `uno` start migrates it: copy, verify every
byte, make everything private, and only then remove the old directory.
"""
import os
import shutil
from pathlib import Path

NAME = ".uno-orchestra"
LEGACY = ".agent-orchestra"


def default_dir(home=None):
    return Path(home or Path.home()) / NAME


def make_private(root):
    os.chmod(root, 0o700)
    for p in Path(root).rglob("*"):
        if not p.is_symlink():
            os.chmod(p, 0o700 if p.is_dir() else 0o600)


def migrate_legacy(home=None):
    """Return the new directory if ~/.agent-orchestra was migrated now, else None.

    Nothing happens when the old directory is absent or the new one already exists. The copy
    is verified file by file before the old directory goes; a failed copy leaves it intact."""
    home = Path(home or Path.home())
    old, new = home / LEGACY, home / NAME
    if not old.is_dir() or new.exists():
        return None
    tmp = home / (NAME + ".migrating")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(old, tmp, symlinks=True)
    make_private(tmp)
    for src in old.rglob("*"):
        dst = tmp / src.relative_to(old)
        if src.is_file() and not src.is_symlink() and (not dst.is_file() or src.read_bytes() != dst.read_bytes()):
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"copy of {old} to {new} failed verification at {src.name}; nothing was removed")
    tmp.rename(new)
    shutil.rmtree(old)
    return new
