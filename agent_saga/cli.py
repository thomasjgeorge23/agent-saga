"""agent-saga command line.

Stdlib argparse, no click/typer -- the tool installs and runs with nothing
beyond the standard library, matching the rest of the package's dependency
discipline.

    agent-saga ui --wal-path ./agent-saga.wal --port 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path


def _cmd_ui(args: argparse.Namespace) -> int:
    from .ui.server import make_server

    wal = Path(args.wal_path)
    host, port = args.host, args.port

    # Token auth for shared/team environments. --token takes a literal; --auth
    # mints one; AGENT_SAGA_UI_TOKEN is the env fallback. None -> open (local).
    token = args.token or os.environ.get("AGENT_SAGA_UI_TOKEN")
    if args.auth and not token:
        token = secrets.token_urlsafe(24)

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
    if token:
        banner.append(f"    Auth    : bearer token required")
        banner.append(f"    Open    : http://{host}:{port}/?token={token}\n")
    if host not in ("127.0.0.1", "localhost", "::1"):
        banner.append(
            f"    !  Bound to {host}, not localhost. A WAL can contain business\n"
            f"       data (ids, amounts). Expose beyond this machine deliberately.\n"
        )
        if not token:
            banner.append(
                "    !  No --token set. Anyone who can reach this port can read the\n"
                "       WAL. Use --auth (or --token) on a shared network.\n"
            )
    banner.append("    Ctrl-C to stop.\n")
    print("\n".join(banner), flush=True)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        httpd = make_server(str(wal), host=host, port=port, token=token)
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


def _read_wal(path: str) -> list:
    from .encryption import decode_line

    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(decode_line(line, None))
            except Exception:
                # A truncated final line is the normal state of a log whose
                # process was killed -- the reader is deliberately tolerant.
                continue
    return records


def _cmd_verify(args: argparse.Namespace) -> int:
    """Exit 0 only if the chain is intact. Designed to be a CI gate and a cron
    job, so the exit code is the product; the report is for the human reading
    the failure."""
    from .integrity import verify

    try:
        records = _read_wal(args.wal_path)
    except OSError as exc:
        print(f"cannot read {args.wal_path}: {exc}")
        return 2

    report = verify(records, strict=args.strict)
    print(f"{args.wal_path}: {report.summary()}")
    if not report.intact:
        print("\nThe log does not add up. Each finding is the first record whose")
        print("hash stops following from the one before it:\n")
        for brk in report.breaks[:20]:
            print(f"  - {brk}")
        if len(report.breaks) > 20:
            print(f"  ... and {len(report.breaks) - 20} more")
        return 1
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Write a verified, self-describing bundle for write-once storage."""
    from .integrity import export_worm, verify

    try:
        records = _read_wal(args.wal_path)
    except OSError as exc:
        print(f"cannot read {args.wal_path}: {exc}")
        return 2

    report = verify(records)
    if not report.intact and not args.allow_broken:
        print(f"refusing to export: {report.summary()}")
        print("Exporting a broken chain would launder it into an artifact that "
              "looks authoritative. Pass --allow-broken to export it anyway, "
              "labelled as broken.")
        return 1

    manifest = export_worm(records, args.out, source=args.wal_path, report=report)
    print(f"exported {manifest['records']} record(s) to {args.out}")
    print(f"  chain head : {manifest['chain_head']}")
    print(f"  bundle sha : {manifest['bundle_sha256']}")
    print(f"  intact     : {manifest['intact']}")
    print("\nStore under an object-lock/WORM policy. Re-verify any time with:")
    print(f"  agent-saga verify --wal-path {args.out}/records.jsonl")
    return 0 if manifest["intact"] else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Run the saga proxy in front of an MCP server, over stdio."""
    import asyncio

    from .mcp import SagaMCPProxy, load_policy_file
    from .mcp.policy import PolicyError, ProxyPolicy
    from .mcp.stdio import UpstreamServer, serve_stdio
    from .wal import FileWAL

    if not args.server:
        print("nothing to proxy: pass the upstream server command after --")
        return 2
    try:
        policy = (ProxyPolicy(mode="observe") if args.observe
                  else load_policy_file(args.policy))
    except (PolicyError, OSError) as exc:
        print(f"policy error: {exc}")
        return 2

    async def run() -> int:
        upstream = UpstreamServer(list(args.server))
        await upstream.start()
        wal = FileWAL(args.wal_path)
        await wal.start()
        proxy = SagaMCPProxy(policy, upstream.call_tool, wal=wal,
                             boundary=args.boundary,
                             server_name=args.server[0])
        try:
            return await serve_stdio(proxy, upstream)
        finally:
            if args.observe and args.emit_policy:
                with open(args.emit_policy, "w", encoding="utf-8") as fh:
                    json.dump(proxy.policy_skeleton(), fh, indent=2, sort_keys=True)
            await wal.close()
            await upstream.close()

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-saga", description="agent-saga tooling")
    sub = p.add_subparsers(dest="command", required=True)

    ui = sub.add_parser("ui", help="launch the time-travel debugger over a WAL file")
    ui.add_argument("--wal-path", default="./agent-saga.wal",
                    help="path to the WAL file (default: ./agent-saga.wal)")
    ui.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    ui.add_argument("--host", default="127.0.0.1",
                    help="bind host (default: 127.0.0.1; use 0.0.0.0 to expose)")
    ui.add_argument("--token", default=None,
                    help="require this bearer token (or set AGENT_SAGA_UI_TOKEN)")
    ui.add_argument("--auth", action="store_true",
                    help="mint a random bearer token and print the URL to open")
    ui.set_defaults(func=_cmd_ui)

    verify = sub.add_parser(
        "verify", help="check the WAL hash chain for tampering")
    verify.add_argument("--wal-path", default="./agent-saga.wal",
                        help="path to the WAL file (default: ./agent-saga.wal)")
    verify.add_argument("--strict", action="store_true",
                        help="also fail on records written before chaining was "
                             "enabled, instead of reporting them as unchained")
    verify.set_defaults(func=_cmd_verify)

    export = sub.add_parser(
        "export", help="write a verified WORM bundle for an auditor")
    export.add_argument("--wal-path", default="./agent-saga.wal",
                        help="path to the WAL file (default: ./agent-saga.wal)")
    export.add_argument("--out", required=True,
                        help="output directory for the bundle")
    export.add_argument("--allow-broken", action="store_true",
                        help="export even if verification fails (labelled broken)")
    export.set_defaults(func=_cmd_export)

    mcp = sub.add_parser(
        "mcp", help="run the saga proxy in front of an MCP server")
    mcp.add_argument("--policy", default="./saga-policy.json",
                     help="tool policy file (default: ./saga-policy.json)")
    mcp.add_argument("--observe", action="store_true",
                     help="forward everything and classify nothing, recording "
                          "which tools are actually called; use with "
                          "--emit-policy to generate a skeleton to review")
    mcp.add_argument("--emit-policy", default=None,
                     help="with --observe, write a policy skeleton here on exit")
    mcp.add_argument("--boundary", default="session",
                     choices=["session", "explicit", "none"],
                     help="what a transaction is (default: session)")
    mcp.add_argument("--wal-path", default="./agent-saga.wal",
                     help="path to the WAL file (default: ./agent-saga.wal)")
    mcp.add_argument("server", nargs=argparse.REMAINDER,
                     help="the upstream server command, after --")
    mcp.set_defaults(func=_cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
