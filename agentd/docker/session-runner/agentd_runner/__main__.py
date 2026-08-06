"""Entrypoint: python -m agentd_runner."""

from __future__ import annotations

import logging
import os
import sys

from agentd_runner.server import serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("AGENTD_RUNNER_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    host = os.environ.get("AGENTD_RPC_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENTD_RPC_PORT", "7000"))
    serve(host, port)


if __name__ == "__main__":
    main()
