#!/usr/bin/env python3
"""Start the local control UI and open it in the browser with this start's token.

    python start.py [--port 8766] [--state-dir ~/.agent-orchestra] [--allowed-root DIR ...]
                    [--idle-timeout 7200] [--no-browser]

The token travels in the URL fragment (never sent to the server by the browser), is kept in
memory by the page, and is also written to <state-dir>/token with mode 0600.
"""
import argparse
import secrets
import sys
import webbrowser
from pathlib import Path

from ui.server import UIServer, write_token


def main():
    p = argparse.ArgumentParser(description="Local web UI for agent-orchestra (loopback only).")
    p.add_argument("--host", default="127.0.0.1", help="must be a loopback address")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--state-dir", default=str(Path.home() / ".agent-orchestra"))
    p.add_argument("--allowed-root", action="append", default=None,
                   help="directory under which repositories may live (repeatable; default: your home)")
    p.add_argument("--idle-timeout", type=float, default=7200, help="seconds without requests before exit")
    p.add_argument("--agent-timeout", type=float, default=900, help="wall-clock seconds per agent")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()

    token = secrets.token_urlsafe(32)
    write_token(args.state_dir, token)
    try:
        srv = UIServer(args.host, args.port, token, args.state_dir,
                       args.allowed_root or [Path.home()], args.idle_timeout, args.agent_timeout)
    except (ValueError, OSError) as exc:
        sys.exit(f"error: {exc}")
    url = f"http://{args.host}:{srv.server_port}/#token={token}"
    print(f"agent-orchestra UI: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("UI stopped", flush=True)


if __name__ == "__main__":
    main()
