"""Models, effort levels and simple-mode profiles, read from models.json (the only list).

Every model and effort that reaches the Claude CLI is validated here against that file and
passed as its own argv element (`--model`, `<id>`, `--effort`, `<level>`): never free text,
never a shell string. A model that the CLI rejects is reported as such, never swapped.
"""
import json
from importlib import resources

_DATA = json.loads(resources.files(__package__).joinpath("models.json").read_text(encoding="utf-8"))
CLI_VERIFIED = _DATA["cli_verified"]
MODELS = [m["id"] for m in _DATA["models"]]
FULL_NAMES = {m["id"]: m["full"] for m in _DATA["models"]}
EFFORTS = list(_DATA["efforts"])
PROFILES = {name: (p["model"], p["effort"]) for name, p in _DATA["profiles"].items()}
ROLE_DEFAULTS = dict(_DATA["role_defaults"])
for _name, (_m, _e) in PROFILES.items():
    assert _m in MODELS and _e in EFFORTS, f"models.json: profile {_name} uses an unlisted model or effort"


def check(model, effort):
    """Raise ValueError unless both are in the allowlist (an enum, not free text)."""
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}: use one of {MODELS}")
    if effort not in EFFORTS:
        raise ValueError(f"unknown effort {effort!r}: use one of {EFFORTS}")


def default_for(name):
    """(model, effort) of the profile models.json assigns to this role name, or to '*'."""
    return PROFILES[ROLE_DEFAULTS.get(name, ROLE_DEFAULTS["*"])]


def profile_of(model, effort):
    """The simple-mode profile name for this pair, or None when it is a custom combination."""
    return next((n for n, pair in PROFILES.items() if pair == (model, effort)), None)


def catalog():
    """What the page needs to build its selects; data only."""
    return {"cli_verified": CLI_VERIFIED, "models": _DATA["models"], "efforts": EFFORTS,
            "profiles": {n: {"model": m, "effort": e} for n, (m, e) in PROFILES.items()},
            "role_defaults": ROLE_DEFAULTS}
