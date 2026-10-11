"""Append-only JSONL event bus.

Every state change (agent started, tool used, progress, merge result...) is one JSON
line in events.jsonl. The dashboard only needs to tail this file.
"""
import json
import threading
import time
from pathlib import Path


class EventBus:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, source, kind, message="", target=None, **extra):
        event = {
            "ts": round(time.time(), 3),
            "source": source,
            "target": target,
            "kind": kind,
            "message": message,
            **extra,
        }
        line = json.dumps(event, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return event
