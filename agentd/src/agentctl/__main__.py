"""agentctl version | status | sessions | logs | quarantine-deferred."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

from agentd.config import load_config
from agentd.db import Store
from agentd.docker_wait import docker_socket_ready
from agentd.paths import agentd_root

log = logging.getLogger("agentctl")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentctl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="Print installed agentd distribution version")
    p_status = sub.add_parser("status", help="Queue depth and host readiness snapshot")
    p_status.add_argument(
        "--json",
        action="store_true",
        help="Print as JSON (default output is already JSON; flag kept for scripts)",
    )
    sub.add_parser("sessions", help="List sessions")
    p_logs = sub.add_parser("logs", help="Tail rotating app log (~/.agentd/logs/agentd.log)")
    p_logs.add_argument("-n", type=int, default=50)
    p_q = sub.add_parser(
        "quarantine-deferred",
        help="Move deferred deliveries to dropped (one-shot historical backlog purge)",
    )
    p_q.add_argument(
        "--before",
        type=int,
        default=None,
        metavar="UNIX_TS",
        help="Only quarantine deliveries with received_at < this timestamp (default: all)",
    )
    p_q.add_argument(
        "--dry-run",
        action="store_true",
        help="Print how many rows would be updated without changing the DB",
    )
    args = parser.parse_args(argv)

    # ADR-13: version needs no host config — dispatch before load_config().
    if args.cmd == "version":
        try:
            print(pkg_version("agentd"))
        except PackageNotFoundError:
            print(
                "agentd distribution metadata not found "
                "(install the package, e.g. pip install -e .)",
                file=sys.stderr,
            )
            sys.exit(1)
        return

    config = load_config()
    if args.cmd == "status":
        from agentd.disk import disk_free_gb

        store = Store(config.state_db)
        snap = store.status_snapshot()
        snap["owner"] = config.owner
        snap["docker"] = docker_socket_ready(config.docker_socket)
        snap["root"] = str(config.root)
        snap["disk_free_gb"] = disk_free_gb(config.root)
        snap["disk_floor_gb"] = config.disk_floor_gb
        snap["disk_resume_gb"] = config.disk_resume_gb
        snap["max_hot_containers"] = config.max_hot_containers
        snap["hot_sessions"] = store.count_hot_sessions()
        snap["intake"] = {
            "mode": config.intake_mode,
            "label": config.intake_label,
            "actors": config.intake_actors,
        }
        print(json.dumps(snap, indent=2))
        store.close()
        return

    if args.cmd == "sessions":
        store = Store(config.state_db)
        rows = store.list_sessions()
        enriched = []
        for r in rows:
            full = store.get_session(str(r["session_key"])) or r
            full.pop("runner_token", None)
            full.pop("token", None)
            enriched.append(full)
        print(json.dumps({"sessions": enriched}, indent=2))
        store.close()
        return

    if args.cmd == "logs":
        log_path = agentd_root() / "logs" / "agentd.log"
        if not log_path.exists():
            print(f"no log at {log_path}", file=sys.stderr)
            sys.exit(1)
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-args.n :]:
            print(line)
        return

    if args.cmd == "quarantine-deferred":
        store = Store(config.state_db)
        pending = store.count_by_status().get("deferred", 0)
        if args.dry_run:
            would = store.count_deferred(before_received_at=args.before)
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "deferred": pending,
                        "before_received_at": args.before,
                        "would_quarantine": would,
                    },
                    indent=2,
                )
            )
            store.close()
            return
        n = store.quarantine_deferred(before_received_at=args.before)
        after = store.count_by_status()
        msg = (
            f"quarantine-deferred: moved {n} deferred → dropped "
            f"(reason=historical-backlog-pre-m3; before={args.before})"
        )
        print(msg, file=sys.stderr)
        print(
            json.dumps(
                {
                    "quarantined": n,
                    "before_received_at": args.before,
                    "deliveries_by_status": after,
                    "at": int(time.time()),
                },
                indent=2,
            )
        )
        # Also append to agentd.log if present so host verification is greppable
        log_path = agentd_root() / "logs" / "agentd.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} INFO agentctl {msg}\n")
        except OSError:
            pass
        store.close()
        return

    parser.error(f"unknown {args.cmd}")


if __name__ == "__main__":
    main()
