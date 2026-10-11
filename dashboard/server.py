#!/usr/bin/env python3
"""Dashboard server: serves index.html and streams events.jsonl over Server-Sent Events.

Standard library only. Usage:
    python dashboard/server.py --events events.jsonl --port 8765
then open http://127.0.0.1:8765
"""
import argparse
import json
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVENTS = Path("events.jsonl")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def log_message(self, *a):  # keep the terminal quiet
        pass

    def do_GET(self):
        if self.path.startswith("/events"):
            return self.stream_events()
        if self.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        pos = 0
        try:
            while True:
                if EVENTS.exists():
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
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    global EVENTS
    p = argparse.ArgumentParser()
    p.add_argument("--events", default="events.jsonl")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="bind address (keep it local)")
    args = p.parse_args()
    EVENTS = Path(args.events)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"dashboard: http://{args.host}:{args.port}  (tailing {EVENTS})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
