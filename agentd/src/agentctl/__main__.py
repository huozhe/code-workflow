"""agentctl status | sessions | logs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agentd.config import load_config
from agentd.db import Store
from agentd.docker_wait import docker_socket_ready
from agentd.paths import agentd_root


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentctl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Queue depth and host readiness snapshot")
    sub.add_parser("sessions", help="List sessions (M0: empty until M2)")
    p_logs = sub.add_parser("logs", help="Tail gateway log file if present")
    p_logs.add_argument("-n", type=int, default=50)
    args = parser.parse_args(argv)

    config = load_config()
    if args.cmd == "status":
        store = Store(config.state_db)
        snap = store.status_snapshot()
        snap["owner"] = config.owner
        snap["docker"] = docker_socket_ready(config.docker_socket)
        snap["root"] = str(config.root)
        print(json.dumps(snap, indent=2))
        store.close()
        return

    if args.cmd == "sessions":
        # M0: session table not yet provisioned
        print(json.dumps({"sessions": [], "note": "session table arrives in M2"}, indent=2))
        return

    if args.cmd == "logs":
        log_path = agentd_root() / "logs" / "gateway.log"
        if not log_path.exists():
            print(f"no log at {log_path}", file=sys.stderr)
            sys.exit(1)
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-args.n :]:
            print(line)
        return

    parser.error(f"unknown {args.cmd}")


if __name__ == "__main__":
    main()
