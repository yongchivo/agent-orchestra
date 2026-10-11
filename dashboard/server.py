#!/usr/bin/env python3
"""Dashboard server: serves index.html and streams events.jsonl over Server-Sent Events.

Standard library only. Usage:
    python dashboard/server.py --events events.jsonl --port 8765
then open http://127.0.0.1:8765

Only the files in FILES are served (nothing else from this directory), SSE clients are
capped, and the page gets a CSP that forbids loading or sending anything off-origin.
"""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
FILES = {"/": "index.html", "/index.html": "index.html"}  # allowlist: nothing else is served
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
EVENTS = Path("events.jsonl")
SSE_SLOTS = threading.BoundedSemaphore(8)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the terminal quiet
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/events":
            return self.stream_events()
        name = FILES.get(path)
        if name is None:
            return self.send_error(404)
        body = (HERE / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        if not SSE_SLOTS.acquire(blocking=False):
            return self.send_error(503, "too many event-stream clients")
        try:
            self._tail()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            SSE_SLOTS.release()

    def _tail(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        pos = 0
        while True:
            if EVENTS.exists():
                if EVENTS.stat().st_size < pos:
                    pos = 0  # the log was rotated: start again from the new file
                with EVENTS.open("r", encoding="utf-8") as f:
                    f.seek(pos)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            json.loads(line)  # only forward valid JSON lines
                        except json.JSONDecodeError:
                            continue
                        self.wfile.write(f"data: {line}\n\n".encode("utf-8"))
                    pos = f.tell()
                self.wfile.flush()
            else:
                self.wfile.write(b": waiting for events file\n\n")
                self.wfile.flush()
            time.sleep(0.25)


def main():
    global EVENTS, SSE_SLOTS
    p = argparse.ArgumentParser()
    p.add_argument("--events", default="events.jsonl")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep it local)")
    p.add_argument("--max-clients", type=int, default=8, help="simultaneous SSE connections")
    args = p.parse_args()
    EVENTS = Path(args.events)
    SSE_SLOTS = threading.BoundedSemaphore(args.max_clients)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}  (tailing {EVENTS})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
