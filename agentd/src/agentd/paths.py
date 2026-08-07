"""Host filesystem layout (§6.2)."""

from __future__ import annotations

import os
from pathlib import Path


def agentd_root() -> Path:
    override = os.environ.get("AGENTD_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".agentd"


def ensure_layout(root: Path | None = None) -> Path:
    """Create repos/, sessions/, archive/ and a default config if missing."""
    root = root or agentd_root()
    for name in ("repos", "sessions", "archive", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    config = root / "config.yaml"
    if not config.exists():
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
    return root


DEFAULT_CONFIG = """\
# agentd host config — see docs/design/unified_design_spec.md §5.4
host:
  root: ~/.agentd
  listen: 127.0.0.1:8787
  disk_floor_gb: 15
  disk_resume_gb: 20
  max_hot_containers: 4
  owner: huozhe

gateway:
  # webhook HMAC secret keychain account: agentd / webhook-secret
  docker_socket: unix:///var/run/docker.sock
  docker_wait_timeout_s: 300

ingress:
  backend: funnel   # funnel | cloudflared | ngrok | smee | none

intake:
  mode: label
  label: agentd
  actors: collaborators

agents:
  claude:
    login: huozheclaude
    credential: keychain://agentd/claude-bot
  grok:
    login: huozhegrok
    credential: keychain://agentd/grok-bot
"""
