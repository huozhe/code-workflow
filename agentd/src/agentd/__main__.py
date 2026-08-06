"""CLI entry: agentd init-layout | serve."""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from agentd.config import load_config
from agentd.db import Store
from agentd.docker_wait import wait_for_docker
from agentd.keychain import webhook_secret
from agentd.paths import ensure_layout
from agentd.server import create_app


class _MaxLevelFilter(logging.Filter):
    """Pass records at or below max_level (so INFO stays off stderr)."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def configure_logging(level: str) -> None:
    """Route DEBUG/INFO → stdout, WARNING+ → stderr (LaunchAgent log split)."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.DEBUG)
    out.addFilter(_MaxLevelFilter(logging.INFO))
    out.setFormatter(fmt)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)

    root.addHandler(out)
    root.addHandler(err)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentd")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-layout", help="Create ~/.agentd layout and default config")

    p_serve = sub.add_parser("serve", help="Run webhook gateway")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument(
        "--skip-docker-wait",
        action="store_true",
        help="Do not block on Docker socket (dev/test)",
    )
    p_serve.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)
    if args.cmd == "init-layout":
        root = ensure_layout()
        print(f"layout ready: {root}")
        return

    if args.cmd == "serve":
        configure_logging(args.log_level)
        log = logging.getLogger("agentd")
        # B1: refuse to listen if we cannot verify signatures.
        secret = webhook_secret()
        if not secret:
            log.error(
                "webhook secret not found (Keychain agentd/webhook-secret "
                "or AGENTD_SECRET_WEBHOOK_SECRET); refusing to start"
            )
            sys.exit(1)

        config = load_config()
        if not args.skip_docker_wait:
            ok = wait_for_docker(
                config.docker_socket,
                timeout_s=config.docker_wait_timeout_s,
            )
            if not ok:
                log.warning(
                    "continuing without Docker socket; /readyz will report not ready"
                )
        store = Store(config.state_db)
        app = create_app(config, store, secret)
        host, port = _listen(config.listen, args.host, args.port)
        log.info("listening on %s:%s db=%s", host, port, config.state_db)
        uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())
        return

    parser.error(f"unknown command {args.cmd}")
    sys.exit(2)


def _listen(
    listen: str, host_override: str | None, port_override: int | None
) -> tuple[str, int]:
    host, _, port_s = listen.partition(":")
    host = host_override or host or "127.0.0.1"
    port = port_override if port_override is not None else int(port_s or "8787")
    return host, port


if __name__ == "__main__":
    main()
