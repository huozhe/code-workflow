"""Load ~/.agentd/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentd.paths import agentd_root, ensure_layout


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    root: Path = field(default_factory=agentd_root)

    @property
    def owner(self) -> str:
        return str(self.raw.get("host", {}).get("owner", "huozhe"))

    @property
    def listen(self) -> str:
        return str(self.raw.get("host", {}).get("listen", "127.0.0.1:8787"))

    @property
    def disk_floor_gb(self) -> float:
        return float(self.raw.get("host", {}).get("disk_floor_gb", 15))

    @property
    def disk_resume_gb(self) -> float:
        return float(self.raw.get("host", {}).get("disk_resume_gb", 20))

    @property
    def docker_socket(self) -> str:
        return str(
            self.raw.get("gateway", {}).get("docker_socket", "unix:///var/run/docker.sock")
        )

    @property
    def docker_wait_timeout_s(self) -> int:
        return int(self.raw.get("gateway", {}).get("docker_wait_timeout_s", 300))

    @property
    def turn_deadline_s(self) -> int:
        """Per-turn deadline sent to the runner (§14.2)."""
        return int(self.raw.get("gateway", {}).get("turn_deadline_s", 900))

    @property
    def rpc_timeout_grace_s(self) -> int:
        """Seconds beyond turn_deadline_s for the gateway RPC read (#34)."""
        return int(self.raw.get("gateway", {}).get("rpc_timeout_grace_s", 60))

    @property
    def rpc_timeout_s(self) -> float:
        """Socket timeout for turn.dispatch — must exceed deadline_s (#34)."""
        return float(self.turn_deadline_s + self.rpc_timeout_grace_s)

    @property
    def intake_mode(self) -> str:
        return str(self.raw.get("intake", {}).get("mode", "label"))

    @property
    def intake_label(self) -> str:
        return str(self.raw.get("intake", {}).get("label", "agentd"))

    @property
    def intake_actors(self) -> str:
        return str(self.raw.get("intake", {}).get("actors", "collaborators"))

    @property
    def governor_interval_s(self) -> float:
        return float(self.raw.get("gateway", {}).get("governor_interval_s", 30))

    @property
    def max_hot_containers(self) -> int:
        return int(self.raw.get("host", {}).get("max_hot_containers", 4))

    @property
    def silent_turn_limit(self) -> int:
        """Consecutive silent agent turns before escalate (#39 / §9.3)."""
        budgets = self.raw.get("budgets") or {}
        if "silent_turn_limit" in budgets:
            return int(budgets["silent_turn_limit"])
        return int(self.raw.get("gateway", {}).get("silent_turn_limit", 3))

    @property
    def state_db(self) -> Path:
        return self.root / "state.db"

    @property
    def gateway_login(self) -> str | None:
        """GitHub login for gateway-authored comments (§8.5 / §5.1 third identity)."""
        gw = self.raw.get("gateway") or {}
        login = gw.get("login")
        return str(login) if login else None

    def agent_login(self, agent_id: str) -> str | None:
        agents = self.raw.get("agents") or {}
        entry = agents.get(agent_id) or {}
        return entry.get("login")

    def all_bot_logins(self) -> set[str]:
        """Agent logins + gateway login (must not be classified as human §9.1)."""
        agents = self.raw.get("agents") or {}
        logins = {
            str(v["login"])
            for v in agents.values()
            if isinstance(v, dict) and "login" in v
        }
        gw = self.gateway_login
        if gw:
            logins.add(gw)
        return logins


def load_config(root: Path | None = None) -> Config:
    root = ensure_layout(root)
    path = root / "config.yaml"
    data: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = loaded
    return Config(raw=data, root=root)
