"""agentctl version | status | sessions | logs | quarantine-deferred | write-verification | review-stats | reconcile."""

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
    p_v = sub.add_parser(
        "write-verification",
        help="Upsert §10.1 verification block on a session issue body (M5-0 / #50)",
    )
    p_v.add_argument(
        "session",
        help="Session key (repo#issue) or bare issue number for the only open session repo",
    )
    p_v.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the new body without PATCHing GitHub",
    )
    p_rs = sub.add_parser(
        "review-stats",
        help="Turns-per-review measurement (ADR-14 / #49)",
    )
    p_rs.add_argument(
        "--session",
        default=None,
        metavar="KEY",
        help="Session key (repo#issue) or bare issue number (default: all sessions)",
    )
    p_rec = sub.add_parser(
        "reconcile",
        help="M6-1a local report (ADR-20). --once waits for the GitHub half.",
    )
    p_rec.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only inventory: orphans, runners, open turns, closed-live",
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

    if args.cmd == "reconcile":
        from agentd.reconciler import Reconciler, pragma_integrity_check

        if not args.dry_run:
            print(
                "agentctl reconcile --once waits for the GitHub half (M6-1b). "
                "This half is --dry-run only.",
                file=sys.stderr,
            )
            sys.exit(2)
        db_path = config.state_db
        integrity = pragma_integrity_check(db_path)
        if integrity != "ok":
            print(json.dumps({"integrity": integrity}, indent=2))
            sys.exit(1)
        store = Store(db_path)
        report = Reconciler(store).reconcile_once(dry_run=True)
        report["integrity"] = integrity
        print(json.dumps(report, indent=2, default=str))
        store.close()
        return

    if args.cmd == "write-verification":
        from agentd.github_write import get_issue_body, patch_issue_body
        from agentd.keychain import get_password
        from agentd.verification import (
            default_steps_for_session,
            extract_verification_block,
            upsert_verification_block,
        )

        store = Store(config.state_db)
        sess = _resolve_session(store, args.session)
        if not sess:
            print(f"no session for {args.session!r}", file=sys.stderr)
            store.close()
            sys.exit(1)
        repo = str(sess["repo"])
        issue_num = int(sess["issue_num"])
        design_pr = sess.get("design_pr")
        feature_pr = sess.get("feature_pr")
        token = get_password("gateway")
        if not token and not args.dry_run:
            print("no gateway token in keychain", file=sys.stderr)
            store.close()
            sys.exit(1)
        current = ""
        if token:
            current = get_issue_body(repo=repo, issue_num=issue_num, token=token)
        prs = [n for n in (design_pr, feature_pr) if n is not None and n != ""]
        new_body = upsert_verification_block(
            current,
            steps=default_steps_for_session(
                design_pr=design_pr, feature_pr=feature_pr
            ),
            merged_prs=prs,
            preserve_checkbox=True,
        )
        had = extract_verification_block(current) is not None
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "session_key": sess["session_key"],
                        "repo": repo,
                        "issue_num": issue_num,
                        "had_block": had,
                        "body_changed": new_body != current,
                        "block": extract_verification_block(new_body),
                    },
                    indent=2,
                )
            )
            store.close()
            return
        if new_body == current and had:
            print(
                json.dumps(
                    {
                        "session_key": sess["session_key"],
                        "issue_num": issue_num,
                        "status": "unchanged",
                    },
                    indent=2,
                )
            )
            store.close()
            return
        patch_issue_body(
            repo=repo, issue_num=issue_num, body=new_body, token=token
        )
        print(
            json.dumps(
                {
                    "session_key": sess["session_key"],
                    "issue_num": issue_num,
                    "status": "written",
                    "had_block": had,
                    "design_pr": design_pr,
                    "feature_pr": feature_pr,
                },
                indent=2,
            )
        )
        store.close()
        return

    if args.cmd == "review-stats":
        from agentd.review_stats import collect_review_stats

        store = Store(config.state_db)
        if args.session is not None:
            sess = _resolve_session(store, args.session)
            if not sess:
                print(f"no session for {args.session!r}", file=sys.stderr)
                store.close()
                sys.exit(1)
            targets = [sess]
        else:
            targets = []
            for row in store.list_sessions():
                full = store.get_session(str(row["session_key"])) or row
                targets.append(full)
        print(json.dumps(collect_review_stats(store, sessions=targets), indent=2))
        store.close()
        return

    parser.error(f"unknown {args.cmd}")


def _resolve_session(store: Store, key: str) -> dict | None:
    """Accept full session_key or bare issue number."""
    key = (key or "").strip()
    if not key:
        return None
    if "#" in key:
        return store.get_session(key)
    # Bare issue number → first matching open session.
    try:
        issue = int(key)
    except ValueError:
        return store.get_session(key)
    for row in store.list_sessions():
        full = store.get_session(str(row["session_key"])) or row
        if int(full.get("issue_num") or 0) == issue:
            return full
    return None


if __name__ == "__main__":
    main()
