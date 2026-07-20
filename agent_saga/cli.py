"""agent-saga command line.

Stdlib argparse, no click/typer -- the tool installs and runs with nothing
beyond the standard library, matching the rest of the package's dependency
discipline.

    agent-saga ui --wal-path ./agent-saga.wal --port 8080
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def _cmd_ui(args: argparse.Namespace) -> int:
    from .ui.server import make_server

    wal = Path(args.wal_path)
    host, port = args.host, args.port

    # ASCII only: this prints to consoles whose encoding is not UTF-8 (Windows
    # cp1252), where box-drawing characters or emoji raise UnicodeEncodeError.
    wal_line = str(wal.resolve()) if wal.exists() else f"{wal}  (waiting - file not found yet)"
    banner = [
        "",
        "  ===============================================",
        "   AgentSaga - Time-Travel Debugger",
        "  ===============================================",
        f"    WAL     : {wal_line}",
        f"    Serving : http://{host}:{port}",
        "",
    ]
    if host not in ("127.0.0.1", "localhost", "::1"):
        banner.append(
            f"    !  Bound to {host}, not localhost. A WAL can contain business\n"
            f"       data (ids, amounts). Expose beyond this machine deliberately.\n"
        )
    banner.append("    Ctrl-C to stop.\n")
    print("\n".join(banner), flush=True)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        httpd = make_server(str(wal), host=host, port=port)
    except OSError as exc:
        print(f"error: could not bind {host}:{port} — {exc}", file=sys.stderr)
        return 1
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.", flush=True)
    finally:
        httpd.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-saga", description="agent-saga tooling")
    sub = p.add_subparsers(dest="command", required=True)

    ui = sub.add_parser("ui", help="launch the time-travel debugger over a WAL file")
    ui.add_argument("--wal-path", default="./agent-saga.wal",
                    help="path to the WAL file (default: ./agent-saga.wal)")
    ui.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    ui.add_argument("--host", default="127.0.0.1",
                    help="bind host (default: 127.0.0.1; use 0.0.0.0 to expose)")
    ui.set_defaults(func=_cmd_ui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
