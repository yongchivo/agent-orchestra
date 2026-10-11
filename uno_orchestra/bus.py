"""Append-only JSONL event bus.

Every state change (agent started, tool used, progress, merge result...) is one JSON
line in events.jsonl. The dashboard only needs to tail this file.

Agent output is untrusted, so before a line is written: long messages are cut, obvious
secrets are redacted, the file is kept 0600, and it rotates by size.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

MAX_MESSAGE = 500
MAX_BYTES = 10 * 1024 * 1024
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}"                       # Anthropic / OpenAI style keys
    r"|gh[pousr]_[A-Za-z0-9]{20,}"                 # GitHub tokens
    r"|AKIA[0-9A-Z]{16}"                           # AWS access key ids
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
)


def redact(value):
    if isinstance(value, str):
        return SECRET_RE.sub("[REDACTED]", value)
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    return value


class EventBus:
    def __init__(self, path, max_bytes=MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, source, kind, message="", target=None, **extra):
        event = {
            "ts": round(time.time(), 3),
            "source": source,
            "target": target,
            "kind": kind,
            "message": redact(str(message)[:MAX_MESSAGE]),
            **redact(extra),
        }
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self._rotate()
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.fchmod(fd, 0o600)  # also fixes a pre-existing file created with a wider mode
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return event

    def _rotate(self):
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size >= self.max_bytes:
            # ponytail: one backup generation; add numbered rotation if logs must be kept longer
            self.path.replace(self.path.with_name(self.path.name + ".1"))
