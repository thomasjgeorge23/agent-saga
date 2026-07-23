"""agent-saga command line.

Stdlib argparse, no click/typer -- the tool installs and runs with nothing
beyond the standard library, matching the rest of the package's dependency
discipline.

    agent-saga ui --wal-path ./agent-saga.wal --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from ._version import __version__
from .integrity import verify, export_worm


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
    import sys
    from .encryption import EncryptedRecordError, decode_line

    # Archived segments may be gzip-compressed (FileWAL compress_archives=True);
    # read them transparently so `verify`/`export` work on an archive as-is.
    if str(path).endswith(".gz"):
        import gzip
        opener = lambda: gzip.open(path, "rt", encoding="utf-8")
    else:
        opener = lambda: open(path, encoding="utf-8")

    records = []
    with opener() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(decode_line(line, None))
            except EncryptedRecordError as exc:
                print(f"Error: {path} contains encrypted records but no key was provided. "
                      f"Set AGENT_SAGA_WAL_KEY environment variable. ({exc})", file=sys.stderr)
                sys.exit(1)
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
    """Export WAL records for auditors.

    Two shapes, one command:

    * ``--out <dir>`` writes a verified, self-describing WORM *bundle* (records
      + manifest + chain head) for write-once/object-lock storage. This is the
      auditor-grade artifact and stays the default when a directory is given.
    * ``--format csv|json`` (optionally ``--output <file>``) writes a flat,
      analysis-friendly dump to a single file or stdout, for teams that just
      need the rows in a spreadsheet or a JSON pipeline.
    """
    try:
        records = _read_wal(args.wal_path)
    except OSError as exc:
        print(f"cannot read {args.wal_path}: {exc}", file=sys.stderr)
        return 2

    report = verify(records)
    if not report.intact and not args.allow_broken:
        print(f"refusing to export: {report.summary()}", file=sys.stderr)
        print("Exporting a broken chain would launder it into an artifact that "
              "looks authoritative. Pass --allow-broken to export it anyway, "
              "labelled as broken.", file=sys.stderr)
        return 1

    # WORM bundle path -- auditor grade, unchanged behaviour.
    if args.out:
        manifest = export_worm(records, args.out, source=args.wal_path, report=report)
        print(f"exported {manifest['records']} record(s) to {args.out}")
        print(f"  chain head : {manifest['chain_head']}")
        print(f"  bundle sha : {manifest['bundle_sha256']}")
        print(f"  intact     : {manifest['intact']}")
        print("\nStore under an object-lock/WORM policy. Re-verify any time with:")
        print(f"  agent-saga verify --wal-path {args.out}/records.jsonl")
        return 0 if manifest["intact"] else 1

    # Flat structured export path -- CSV or JSON, to a file or stdout.
    fmt = (args.format or "json").lower()
    output_path = Path(args.output) if args.output else None

    if fmt == "csv":
        import csv, io

        keys = sorted({k for r in records for k in r.keys()}) if records else []
        buf = io.StringIO()
        # lineterminator="\n" keeps the buffer newline-clean; we then write with
        # newline="" so Windows does not turn each "\n" into a blank-line "\r\n".
        writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for r in records:
            writer.writerow({k: _csv_cell(r.get(k, "")) for k in keys})
        text = buf.getvalue()
    else:  # json
        text = json.dumps(records, indent=2, default=str)

    if output_path:
        with open(output_path, "w", newline="", encoding="utf-8") as fh:
            fh.write(text)
        print(f"exported {len(records)} record(s) as {fmt} to {output_path}", file=sys.stderr)
    else:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    return 0 if report.intact else 1


def _csv_cell(value: object) -> str:
    """Flatten a WAL value into a single CSV cell. Nested dicts/lists become
    compact JSON so a row snapshot or kwargs blob survives the round-trip
    instead of rendering as a bare ``{...}`` repr."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, separators=(",", ":"))
    return "" if value is None else str(value)


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


def _approval_store(args: argparse.Namespace):
    from .approvals import FileApprovalStore

    if getattr(args, "redis", None):
        from .approvals import RedisApprovalStore

        return RedisApprovalStore(args.redis)
    return FileApprovalStore(args.dir)


def _format_local_time(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_utc_time(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_timestamps(ts: float) -> str:
    """UTC and the operator's local time on one line, so nobody has to convert in
    their head to know when an approval was requested."""
    return f"{_format_utc_time(ts)}  /  {_format_local_time(ts)} (local)"


def _approvals_by_status(store, status: str):
    """Return the requests matching `status` ('pending'|'granted'|'denied'|'all'),
    or None if the store cannot serve non-pending history. 'pending' works on
    every store; the rest need list_all()."""
    from .approvals import GRANTED, DENIED, PENDING

    if status == "pending":
        return list(store.pending())
    list_all = getattr(store, "list_all", None)
    if list_all is None:
        return None
    records = list(list_all())
    if status == "all":
        return records
    wanted = {"granted": GRANTED, "denied": DENIED}[status]
    return [r for r in records if r.status == wanted]


def _cmd_approvals(args: argparse.Namespace) -> int:
    store = _approval_store(args)

    if args.action == "list":
        status = getattr(args, "status", "pending")
        requests = _approvals_by_status(store, status)
        if requests is None:
            print(f"this approval store does not support listing by status "
                  f"'{status}' (only --status pending is available for it)")
            return 2
        if not requests:
            print(f"no {status} approvals" if status != "all" else "no approvals")
            return 0
        for request in requests:
            ts = getattr(request, "requested_at", time.time())
            print(request.summary())
            print(f"      requested: {_format_timestamps(ts)}")
            for key, value in request.context.items():
                print(f"      {key}: {value}")
        return 0

    if not args.approver:
        # An approval with no named approver is an audit trail that proves
        # nothing, which is the only thing this record exists for.
        print("--approver is required: an anonymous approval is not an approval")
        return 2

    granted = args.action == "approve"
    if args.break_glass and not granted:
        print("--break-glass only applies to approve")
        return 2

    request = store.decide(args.id, granted=granted, approver=args.approver,
                           note=args.note or "", break_glass=args.break_glass)
    if request is None:
        print(f"no such approval: {args.id}")
        return 2
    if request.decided_at and request.approver != args.approver:
        print(f"already {request.status} by {request.approver} -- first decision wins")
        return 1
    print(f"{request.status} by {request.approver}"
          + (" (BREAK-GLASS: requires post-hoc review)" if request.break_glass else ""))
    return 0


def _tail_wal(wal_path: str, stop: "threading.Event") -> None:
    """Follow a WAL file and print each new event as it lands -- a `tail -f` for
    saga activity. Deliberately tolerant of a truncated final line and of the
    file not existing yet (studio may start before the first saga runs)."""
    from .encryption import decode_line

    pos = 0            # byte offset of the last complete line consumed
    printed_wait = False
    while not stop.is_set():
        try:
            if not os.path.exists(wal_path):
                if not printed_wait:
                    print(f"  [tail] waiting for {wal_path} ...", flush=True)
                    printed_wait = True
                stop.wait(0.5)
                continue
            # Binary + byte offsets: text-mode tell() during iteration raises,
            # and only complete (newline-terminated) lines are safe to decode.
            with open(wal_path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
            last_nl = data.rfind(b"\n")
            if last_nl != -1:
                complete, pos = data[:last_nl + 1], pos + last_nl + 1
                for raw in complete.split(b"\n"):
                    if not raw.strip():
                        continue
                    try:
                        rec = decode_line(raw.decode("utf-8", errors="ignore"), None)
                    except Exception:
                        continue
                    ev = rec.get("event", "?")
                    sid = rec.get("name") or rec.get("saga_id", "-")
                    extra = rec.get("tool") or rec.get("step_id") or ""
                    print(f"  [tail] {ev:<18} {sid} {extra}".rstrip(), flush=True)
        except OSError:
            pass
        stop.wait(0.4)


def _cmd_studio(args: argparse.Namespace) -> int:
    import threading
    from .ui.server import make_server

    stop = threading.Event()
    workers: list[threading.Thread] = []

    token = args.token or os.environ.get("AGENT_SAGA_UI_TOKEN")
    if args.auth and not token:
        token = secrets.token_urlsafe(24)

    # Recovery daemon on its own thread + event loop, sweeping continuously.
    if args.recover:
        def _run_recovery() -> None:
            from .recovery import RecoveryDaemon
            daemon = RecoveryDaemon(args.wal_path, dry_run=args.dry_run)
            try:
                asyncio.run(daemon.watch(interval=args.recover_interval))
            except Exception:
                logging.getLogger("agent_saga.cli").exception("recovery daemon stopped")
        t = threading.Thread(target=_run_recovery, name="saga-recoveryd", daemon=True)
        t.start()
        workers.append(t)

    if args.tail:
        t = threading.Thread(target=_tail_wal, args=(args.wal_path, stop),
                             name="saga-tail", daemon=True)
        t.start()
        workers.append(t)

    services = ["dashboard"]
    if args.recover:
        services.append(f"recovery daemon ({'dry-run' if args.dry_run else 'active'})")
    if args.tail:
        services.append("WAL tail")
    print(f"agent-saga Studio  (WAL: {args.wal_path})")
    print(f"  running: {', '.join(services)}")
    print(f"  dashboard: http://{args.host}:{args.port}"
          + (f"/?token={token}" if token else ""))
    print("  Ctrl-C to stop.\n", flush=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        httpd = make_server(args.wal_path, args.host, args.port, token=token)
    except OSError as exc:
        print(f"error: could not bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        stop.set()
        return 1
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopping studio ...", flush=True)
    finally:
        stop.set()
        httpd.server_close()
    return 0


def _switch(args: argparse.Namespace):
    from .killswitch import FileSwitchStore, KillSwitch

    if getattr(args, "redis", None):
        from .killswitch import RedisSwitchStore

        return KillSwitch(RedisSwitchStore(args.redis))
    return KillSwitch(FileSwitchStore(args.file))


def _cmd_halt(args: argparse.Namespace) -> int:
    if not args.by or not args.reason:
        print("--by and --reason are required: an anonymous halt with no stated "
              "cause is the hardest thing to safely lift later")
        return 2
    switch = _switch(args)
    result = switch.halt(scope=args.scope, reason=args.reason, by=args.by,
                         ttl=args.ttl, drain=args.drain)
    print(result.summary())
    if not getattr(switch.store, "distributed", False):
        print("\nWARNING: this store is not distributed. Other processes will "
              "keep running. Use --redis for a fleet.")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    if _switch(args).resume(scope=args.scope, by=args.by):
        print(f"resumed {args.scope}")
        return 0
    print(f"nothing halted at scope {args.scope}")
    return 1


def _cmd_status(args: argparse.Namespace) -> int:
    status = _switch(args).status()
    print(f"store       : {status['store']} "
          f"({'distributed' if status['distributed'] else 'LOCAL ONLY'})")
    print(f"reachable   : {status['reachable']}"
          + (f" (degraded {status['degraded_for']}s)" if status["degraded_for"] else ""))
    if not status["switches"] and not status["quarantined"]:
        print("state       : RUNNING (nothing halted)")
        return 0
    for line in status["switches"]:
        print(f"halt        : {line}")
    for saga_id, info in status["quarantined"].items():
        print(f"quarantined : {saga_id} by {info.get('by', '-')}: {info.get('reason', '')}")
    return 0


def _cmd_quarantine(args: argparse.Namespace) -> int:
    switch = _switch(args)
    if args.release:
        if switch.release(args.saga_id, by=args.by):
            print(f"released {args.saga_id}")
            return 0
        print(f"{args.saga_id} was not quarantined")
        return 1
    if not args.by or not args.reason:
        print("--by and --reason are required")
        return 2
    switch.quarantine(args.saga_id, reason=args.reason, by=args.by)
    print(f"quarantined {args.saga_id}. It will make no further calls and the "
          f"recovery daemon will not touch it.")
    print("This is a freeze, not a rollback -- nothing has been undone.")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    """Ask the external systems whether the log is telling the truth.

    Exit codes are the product: 0 clean, 1 drift, 3 nothing could be verified.
    Drift and "we could not check" are different problems needing different
    people, so they do not share an exit code.
    """
    import asyncio

    from .reconcile import Reconciliation

    for module in args.imports or []:
        __import__(module)          # registers @reconciler handlers

    try:
        records = _read_wal(args.wal_path)
    except OSError as exc:
        print(f"cannot read {args.wal_path}: {exc}")
        return 2

    report = asyncio.run(Reconciliation().run(records))
    print(f"{args.wal_path}: {report.summary()}")

    if report.drift:
        print("\nDRIFT -- the system disagrees with the log:\n")
        for finding in report.drift:
            print(f"  - {finding}")
    if report.unverifiable and args.verbose:
        print("\nUnverifiable:\n")
        for finding in report.unverifiable:
            print(f"  - {finding}")
    elif report.unverifiable:
        handlers = sorted({f.handler for f in report.unverifiable})
        print(f"\n{len(report.unverifiable)} effect(s) could not be verified "
              f"(handlers: {', '.join(handlers)}). Register a @reconciler for "
              f"them, or pass --verbose. These are asserted by the log alone.")

    if report.drift:
        return 1
    if report.checked and not report.confirmed:
        return 3
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    from .recovery import RecoveryDaemon
    daemon = RecoveryDaemon(args.wal_path, dry_run=args.dry_run)
    outcomes = asyncio.run(daemon.recover_all())
    print(f"Recovery Sweep Completed. {len(outcomes)} saga(s) processed. (Dry-Run: {args.dry_run})")
    for o in outcomes:
        print(f"  - Saga '{o.saga_id}': {o.resolution.value} ({o.reason})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent-saga",
        description="Inspect and operate agent-saga state: human approvals, "
                    "kill-switches, audit logs, and the time-travel debugger.",
    )
    p.add_argument("--version", action="version", version=f"agent-saga {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    recov = sub.add_parser("recover", help="run recovery daemon sweep over orphaned sagas")
    recov.add_argument("--wal-path", "--wal", default="./agent-saga.wal", help="path to WAL file")
    recov.add_argument("--dry-run", action="store_true", help="run in observation-only mode without executing compensations")
    recov.set_defaults(func=_cmd_recover)

    ui = sub.add_parser("ui", help="launch the time-travel debugger over a WAL file")
    ui.add_argument("--wal-path", "--wal", default="./agent-saga.wal",
                    help="path to the WAL file (default: ./agent-saga.wal)")
    ui.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    ui.add_argument("--host", default="127.0.0.1",
                    help="bind host (default: 127.0.0.1; use 0.0.0.0 to expose)")
    ui.add_argument("--token", default=None,
                    help="require this bearer token (or set AGENT_SAGA_UI_TOKEN)")
    ui.add_argument("--auth", action="store_true",
                    help="mint a random bearer token and print the URL to open")
    ui.set_defaults(func=_cmd_ui)

    studio = sub.add_parser(
        "studio",
        help="one-command local dev: dashboard + recovery daemon + WAL tail in one process")
    studio.add_argument("--wal-path", "--wal", default="./agent-saga.wal", help="path to WAL file")
    studio.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    studio.add_argument("--host", default="127.0.0.1", help="bind host")
    studio.add_argument("--recover", action="store_true",
                        help="also run the recovery daemon, sweeping for orphaned sagas")
    studio.add_argument("--recover-interval", type=float, default=5.0,
                        help="seconds between recovery sweeps (default: 5)")
    studio.add_argument("--dry-run", action="store_true",
                        help="recovery daemon observes only, runs no compensations")
    studio.add_argument("--tail", action="store_true",
                        help="stream new WAL events to the console as they land")
    studio.add_argument("--token", default=None, help="bearer token to protect the dashboard")
    studio.add_argument("--auth", action="store_true",
                        help="mint a random bearer token if none is given")
    studio.set_defaults(func=_cmd_studio)

    verify = sub.add_parser(
        "verify", help="check the WAL hash chain for tampering")
    verify.add_argument("--wal-path", "--wal", default="./agent-saga.wal",
                        help="path to the WAL file (default: ./agent-saga.wal)")
    verify.add_argument("--strict", action="store_true",
                        help="also fail on records written before chaining was "
                             "enabled, instead of reporting them as unchained")
    verify.set_defaults(func=_cmd_verify)

    export = sub.add_parser(
        "export",
        help="export the WAL: a verified WORM bundle (--out) or flat CSV/JSON (--format)")
    export.add_argument("--wal-path", "--wal", default="./agent-saga.wal",
                        help="path to the WAL file (default: ./agent-saga.wal)")
    export.add_argument("--out", default=None,
                        help="output DIRECTORY for a verified WORM bundle (auditor grade)")
    export.add_argument("--format", default="json", choices=["json", "csv"],
                        help="flat export format when --out is not used (default: json)")
    export.add_argument("--output", "-o", default=None,
                        help="output FILE for the flat CSV/JSON export (default: stdout)")
    export.add_argument("--allow-broken", action="store_true",
                        help="export even if hash-chain verification fails (labelled broken)")
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
    mcp.add_argument("--wal-path", "--wal", default="./agent-saga.wal",
                     help="path to the WAL file (default: ./agent-saga.wal)")
    mcp.add_argument("server", nargs=argparse.REMAINDER,
                     help="the upstream server command, after --")
    mcp.set_defaults(func=_cmd_mcp)

    appr = sub.add_parser(
        "approvals", help="list and answer pending human approvals")
    appr.add_argument("action", choices=["list", "approve", "deny"])
    appr.add_argument("id", nargs="?", default="",
                      help="approval id (for approve/deny)")
    appr.add_argument("--status", default="pending",
                      choices=["pending", "granted", "denied", "all"],
                      help="which approvals to list (default: pending)")
    appr.add_argument("--approver", default="",
                      help="who is deciding; required, and recorded")
    appr.add_argument("--note", default="", help="reason, recorded with the decision")
    appr.add_argument("--break-glass", action="store_true",
                      help="emergency override; recorded distinctly and flagged "
                           "for post-hoc review")
    appr.add_argument("--dir", default="./.agent-saga-approvals",
                      help="approval directory (default: ./.agent-saga-approvals)")
    appr.add_argument("--redis", default=None,
                      help="use a Redis store instead, e.g. redis://localhost:6379/0")
    appr.set_defaults(func=_cmd_approvals)

    def _switch_args(sp):
        sp.add_argument("--file", default="./.agent-saga-switch.json",
                        help="switch file (default: ./.agent-saga-switch.json)")
        sp.add_argument("--redis", default=None,
                        help="use a Redis store instead -- required for a fleet")
        return sp

    halt = _switch_args(sub.add_parser(
        "halt", help="stop agents performing side effects, now"))
    halt.add_argument("--scope", default="*",
                      help="'*', 'tool:<name>', 'tool:<prefix>.*' or 'tag:<name>' "
                           "(default: everything)")
    halt.add_argument("--reason", default="", help="why; recorded and shown")
    halt.add_argument("--by", default="", help="who; recorded and shown")
    halt.add_argument("--ttl", type=float, default=0.0,
                      help="auto-lift after N seconds (a halt nobody lifts is "
                           "its own outage)")
    halt.add_argument("--drain", action="store_true",
                      help="let running sagas finish, start no new ones")
    halt.set_defaults(func=_cmd_halt)

    resume = _switch_args(sub.add_parser("resume", help="lift a halt"))
    resume.add_argument("--scope", default="*")
    resume.add_argument("--by", default="")
    resume.set_defaults(func=_cmd_resume)

    status = _switch_args(sub.add_parser("status", help="what is halted right now"))
    status.set_defaults(func=_cmd_status)
    quar = _switch_args(sub.add_parser(
        "quarantine", help="freeze one saga for investigation (not a rollback)"))
    quar.add_argument("saga_id")
    quar.add_argument("--reason", default="")
    quar.add_argument("--by", default="")
    quar.add_argument("--release", action="store_true", help="un-quarantine it")
    quar.set_defaults(func=_cmd_quarantine)

    rpy = sub.add_parser("replay", help="simulate a historical saga failure locally for debugging")
    rpy.add_argument("saga_id", help="the saga_id to debug and replay")
    rpy.add_argument("--wal-path", "--wal", default="./agent-saga.wal", help="path to the WAL file (default: ./agent-saga.wal)")
    rpy.set_defaults(func=_cmd_replay)

    rec = sub.add_parser(
        "reconcile",
        help="ask the external systems whether the log is telling the truth")
    rec.add_argument("--wal-path", "--wal", default="./agent-saga.wal",
                     help="path to the WAL file (default: ./agent-saga.wal)")
    rec.add_argument("--import", dest="imports", action="append", default=[],
                     metavar="MODULE",
                     help="import a module so its @reconciler handlers register; "
                          "repeatable. Without these, every effect is "
                          "unverifiable by definition.")
    rec.add_argument("--verbose", action="store_true",
                     help="list every unverifiable effect, not just a count")
    rec.set_defaults(func=_cmd_reconcile)
    return p


def _cmd_replay(args: argparse.Namespace) -> int:
    from pathlib import Path
    wal_path = Path(args.wal_path)
    saga_target = args.saga_id

    if not wal_path.exists():
        print(f"Error: WAL file not found at {wal_path}", file=sys.stderr)
        return 1

    records = _read_wal(wal_path)
    saga_records = [r for r in records if r.get("saga_id") == saga_target]

    if not saga_records:
        print(f"No records found for saga_id '{saga_target}' in {wal_path}")
        return 1

    print(f"\n=======================================================")
    print(f"  TIME-TRAVEL DEBUGGER: Replaying Saga '{saga_target}'")
    print(f"=======================================================")
    print(f"Found {len(saga_records)} records in {wal_path}\n")

    for idx, rec in enumerate(saga_records, 1):
        event = rec.get("event", "UNKNOWN")
        seq = rec.get("seq", idx)
        tool = rec.get("tool", "-")
        payload = rec.get("payload", {})
        print(f" [{seq:03d}] {event:<22} tool={tool:<20} payload={json.dumps(payload, default=str)[:80]}")

    print("\nSimulated Replay Completed Cleanly (Dry-Run Mode).")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
