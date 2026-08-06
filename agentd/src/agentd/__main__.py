"""CLI entry: agentd init-layout | serve."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn

from agentd.config import load_config
from agentd.db import Store
from agentd.docker_wait import wait_for_docker
from agentd.keychain import webhook_secret
from agentd.paths import agentd_root, ensure_layout
from agentd.server import create_app

# App-owned log: rotates in-process (launchd StandardOutPath fds cannot).
LOG_MAX_BYTES = 1 << 20  # 1 MiB
LOG_BACKUP_COUNT = 5


def configure_logging(level: str, log_dir: Path | None = None) -> Path:
    """Configure root logging.

    - Primary: RotatingFileHandler → ``{log_dir}/agentd.log`` (owns its fd;
      caps scanner/access volume when uvicorn uses log_config=None).
    - stderr WARNING+: LaunchAgent ``gateway.err.log`` crash/traceback sink only.
      No stdout INFO mirror — that would re-unbound ``gateway.log``.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, level))
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    log_dir = Path(log_dir) if log_dir is not None else agentd_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_path = log_dir / "agentd.log"
    file_h = RotatingFileHandler(
        file_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(fmt)
    root.addHandler(err)
    return file_path


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
        # log_config=None: do not install uvicorn's own stdout/stderr handlers
        # (propagate=False). Access/error then reach root RotatingFileHandler.
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=args.log_level.lower(),
            log_config=None,
        )
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
