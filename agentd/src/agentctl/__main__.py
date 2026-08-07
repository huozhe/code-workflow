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
    p_logs = sub.add_parser("logs", help="Tail rotating app log (~/.agentd/logs/agentd.log)")
    p_logs.add_argument("-n", type=int, default=50)
    args = parser.parse_args(argv)

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
            # Never print runner bearer tokens
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

    parser.error(f"unknown {args.cmd}")


if __name__ == "__main__":
    main()
