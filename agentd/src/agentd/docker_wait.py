"""Wait for Docker/OrbStack socket with backoff (§11.1)."""

from __future__ import annotations

import logging
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("agentd.docker_wait")


def socket_path_from_url(docker_socket: str) -> Path:
    # unix:///var/run/docker.sock or plain path
    if docker_socket.startswith("unix://"):
        return Path(urlparse(docker_socket).path or "/var/run/docker.sock")
    return Path(docker_socket)


def docker_socket_ready(docker_socket: str) -> bool:
    path = socket_path_from_url(docker_socket)
    if not path.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.0)
        s.connect(str(path))
        s.close()
        return True
    except OSError:
        return False


def wait_for_docker(
    docker_socket: str,
    *,
    timeout_s: float = 300,
    initial_delay: float = 0.5,
    max_delay: float = 10.0,
) -> bool:
    """Retry until Docker socket accepts connections or timeout."""
    deadline = time.monotonic() + timeout_s
    delay = initial_delay
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        if docker_socket_ready(docker_socket):
            log.info("docker socket ready after %s attempt(s)", attempt)
            return True
        log.warning(
            "docker socket not ready (attempt %s); retry in %.1fs", attempt, delay
        )
        time.sleep(delay)
        delay = min(max_delay, delay * 1.5)
    log.error("docker socket not ready within %.0fs", timeout_s)
    return False
