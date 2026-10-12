"""User settings in <state-dir>/config.json (0600). Keys:

  planner_model, planner_effort: what the orchestrator's planner runs with (allowlist in
                  models.json; the UI can override them for one request).

  auth = account  (default) the Claude CLI's own login; ANTHROPIC_API_KEY in the environment is
                  NOT passed to the agents or the planner.
       = api-key  ANTHROPIC_API_KEY (and ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL) are passed and
                  the agents run with --bare.
"""
import json
import os
from pathlib import Path

from . import models

FILE = "config.json"
CHOICES = {"auth": ("account", "api-key"), "planner_model": tuple(models.MODELS), "planner_effort": tuple(models.EFFORTS)}
DEFAULTS = {"auth": "account", "planner_model": models.default_for("planner")[0],
            "planner_effort": models.default_for("planner")[1]}


def load(state_dir):
    """Validated settings; anything missing or invalid falls back to the default."""
    path = Path(state_dir) / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: (data[k] if data.get(k) in CHOICES[k] else d) for k, d in DEFAULTS.items()}


def set_value(state_dir, key, value):
    if key not in CHOICES:
        raise ValueError(f"unknown setting {key!r}: use one of {sorted(CHOICES)}")
    if value not in CHOICES[key]:
        raise ValueError(f"{key} must be one of {list(CHOICES[key])}")
    data = load(state_dir)
    data[key] = value
    d = Path(state_dir)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    fd = os.open(d / FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def auth_mode(state_dir):
    return load(state_dir)["auth"]
