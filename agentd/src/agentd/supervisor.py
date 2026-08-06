"""Session Supervisor — docker lifecycle, tokens, RPC attach (§6.5, §7.2)."""

from __future__ import annotations

import json
import logging
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentd.config import Config
from agentd.db import Store
from agentd.gitops import (
    ensure_shared_clone,
    session_dir_name,
    worktree_add,
)
from agentd.keychain import get_password
from agentd.rpc_client import RunnerClient

log = logging.getLogger("agentd.supervisor")

IMAGE = "agentd/session-runner:1.0.0"
RPC_CONTAINER_PORT = 7000


@dataclass
class SessionHandle:
    session_key: str
    container_id: str
    host_port: int
    bearer: str
    endpoint: str  # 127.0.0.1:port
    tier: str


def container_name(session_key: str) -> str:
    # docker name-safe
    return "agentd-" + session_key.replace("/", "-").replace("#", "-")


def generate_bearer() -> str:
    """≥ 256 bits from CSPRNG (§14.1)."""
    return secrets.token_urlsafe(32)  # 256 bits


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def image_present(image: str = IMAGE) -> bool:
    r = _docker("image", "inspect", image, check=False)
    return r.returncode == 0


def assert_no_docker_sock_mount(container_id: str) -> None:
    """§7.2: never mount docker.sock. Fail hard if present."""
    r = _docker("inspect", container_id, "--format", "{{json .Mounts}}")
    mounts = json.loads(r.stdout or "[]")
    for m in mounts:
        src = str(m.get("Source") or "")
        dest = str(m.get("Destination") or "")
        if "docker.sock" in src or "docker.sock" in dest:
            raise RuntimeError(
                f"FORBIDDEN: docker.sock mount on container {container_id}: {m}"
            )


def _host_port_from_inspect(container_id: str) -> int:
    r = _docker(
        "inspect",
        container_id,
        "--format",
        f'{{{{(index (index .NetworkSettings.Ports "{RPC_CONTAINER_PORT}/tcp") 0).HostPort}}}}',
    )
    port_s = (r.stdout or "").strip()
    if not port_s:
        raise RuntimeError(f"no host port published for {container_id}")
    return int(port_s)


class SessionSupervisor:
    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config

    def ensure_session(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        architect_login: str | None = None,
        developer_login: str | None = None,
        skip_preflight: bool = False,
        clone_url: str | None = None,
    ) -> SessionHandle:
        """Create or resume HOT session: clone, worktree, container, init RPC."""
        existing = self.store.get_session(session_key)
        if existing and existing.get("container_id"):
            cid = str(existing["container_id"])
            # Try re-attach
            try:
                handle = self._handle_from_row(existing)
                with RunnerClient("127.0.0.1", handle.host_port, handle.bearer) as cli:
                    cli.call("health.ping")
                self.store.upsert_runner(
                    session_key,
                    container_id=cid,
                    endpoint=handle.endpoint,
                    token=handle.bearer,
                    tier="hot",
                )
                return handle
            except Exception:
                log.warning("existing session unreachable; recreating %s", session_key)

        architect = architect_login or self.config.agent_login("claude") or "huozheclaude"
        developer = developer_login or self.config.agent_login("grok") or "huozhegrok"

        # Layout
        sess_host = self.config.root / "sessions" / session_dir_name(session_key)
        for role in ("architect", "developer"):
            for sub in ("home", "tmp", "xdg", "worktrees", "context", "scratch"):
                (sess_host / role / sub).mkdir(parents=True, exist_ok=True)
            (sess_host / role / "transcript.jsonl").touch(exist_ok=True)

        clone = ensure_shared_clone(self.config.root, repo, clone_url=clone_url)
        # One worktree per role for the issue (shared object store)
        wt_arch = sess_host / "architect" / "worktrees" / f"issue-{issue_num}"
        wt_dev = sess_host / "developer" / "worktrees" / f"issue-{issue_num}"
        sn = session_dir_name(session_key)
        worktree_add(clone, wt_arch, f"agentd/{sn}/architect")
        worktree_add(clone, wt_dev, f"agentd/{sn}/developer")

        bearer = generate_bearer()
        name = container_name(session_key)
        # Remove any prior container with same name
        _docker("rm", "-f", name, check=False)

        env = {
            "AGENTD_RUNNER_BEARER": bearer,
            "AGENTD_RPC_HOST": "0.0.0.0",
            "AGENTD_RPC_PORT": str(RPC_CONTAINER_PORT),
        }
        if skip_preflight:
            env["AGENTD_SKIP_PREFLIGHT"] = "1"

        run_args = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "agentd.managed=true",
            "--label",
            f"agentd.session={session_key}",
            "--restart",
            "unless-stopped",
            "--memory",
            "3g",
            "--memory-swap",
            "3g",
            "--cpus",
            "2",
            "--pids-limit",
            "1024",
            # Drop everything, then re-add the minimum for §5.2/§7.3:
            # CHOWN/FOWNER — write 0400 role-owned tokens on tmpfs as PID1 root
            # SETUID/SETGID — fork → setuid(role) before exec (turn path)
            # (pure --cap-drop ALL makes chown/setuid EPERM under OrbStack.)
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--cap-add",
            "FOWNER",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/run/agent:rw,noexec,nosuid,size=1m,mode=0711",
            "-v",
            f"{clone}:/srv/repo",
            "-v",
            f"{sess_host}:/srv/session",
            "-p",
            f"127.0.0.1:0:{RPC_CONTAINER_PORT}",
        ]
        for k, v in env.items():
            run_args.extend(["-e", f"{k}={v}"])
        run_args.append(IMAGE)

        # Never pass docker.sock
        assert not any("docker.sock" in a for a in run_args)

        r = _docker(*run_args)
        cid = r.stdout.strip()
        assert_no_docker_sock_mount(cid)
        host_port = _host_port_from_inspect(cid)
        endpoint = f"127.0.0.1:{host_port}"

        # Wait for RPC
        self._wait_rpc(host_port, bearer, timeout_s=30)

        tokens = self._load_tokens()
        with RunnerClient("127.0.0.1", host_port, bearer) as cli:
            cli.call(
                "session.init",
                {
                    "session_key": session_key,
                    "roles": {"architect": architect, "developer": developer},
                    "tokens": tokens,
                },
            )
            ping = cli.call("health.ping")
            log.info("health.ping ok session=%s rss=%s", session_key, ping.get("rss_bytes"))

        now = int(time.time())
        self.store.upsert_session(
            session_key=session_key,
            repo=repo,
            issue_num=issue_num,
            state="INTAKE",
            architect=architect,
            developer=developer,
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_runner(
            session_key,
            container_id=cid,
            endpoint=endpoint,
            token=bearer,
            tier="hot",
        )
        return SessionHandle(
            session_key=session_key,
            container_id=cid,
            host_port=host_port,
            bearer=bearer,
            endpoint=endpoint,
            tier="hot",
        )

    def demote_cold(self, session_key: str) -> None:
        """COLD = docker stop only (§6.5). Never docker pause as memory save."""
        row = self.store.get_runner(session_key)
        if not row or not row.get("container_id"):
            return
        cid = str(row["container_id"])
        _docker("stop", cid, check=False)
        self.store.upsert_runner(
            session_key,
            container_id=cid,
            endpoint=str(row.get("endpoint") or ""),
            token=str(row.get("token") or ""),
            tier="cold",
        )
        log.info("session %s → COLD (docker stop)", session_key)

    def promote_hot(self, session_key: str) -> SessionHandle:
        row = self.store.get_session(session_key)
        runner = self.store.get_runner(session_key)
        if not row or not runner:
            raise RuntimeError(f"unknown session {session_key}")
        cid = str(runner["container_id"])
        bearer = str(runner["token"])
        _docker("start", cid, check=True)
        host_port = _host_port_from_inspect(cid)
        self._wait_rpc(host_port, bearer, timeout_s=30)
        tokens = self._load_tokens()
        with RunnerClient("127.0.0.1", host_port, bearer) as cli:
            cli.call(
                "session.resume",
                {
                    "session_key": session_key,
                    "roles": {
                        "architect": row["architect"],
                        "developer": row["developer"],
                    },
                    "tokens": tokens,
                },
            )
            cli.call("health.ping")
        endpoint = f"127.0.0.1:{host_port}"
        self.store.upsert_runner(
            session_key,
            container_id=cid,
            endpoint=endpoint,
            token=bearer,
            tier="hot",
        )
        return SessionHandle(
            session_key=session_key,
            container_id=cid,
            host_port=host_port,
            bearer=bearer,
            endpoint=endpoint,
            tier="hot",
        )

    def adversarial_token_check(self, handle: SessionHandle) -> dict[str, Any]:
        """As uid_developer, attempt to read architect token — must fail."""
        # Run inside container: su to developer and cat architect token
        script = (
            "set -e; "
            "if [ -r /run/agent/architect/token ]; then echo READABLE; cat /run/agent/architect/token; exit 0; "
            "else echo DENIED; exit 1; fi"
        )
        r = _docker(
            "exec",
            "-u",
            "1002:1002",
            handle.container_id,
            "bash",
            "-c",
            script,
            check=False,
        )
        readable = "READABLE" in (r.stdout or "") or r.returncode == 0
        return {
            "developer_can_read_architect_token": readable,
            "returncode": r.returncode,
            "stdout": (r.stdout or "").strip()[:200],
            "stderr": (r.stderr or "").strip()[:200],
        }

    def _load_tokens(self) -> dict[str, str]:
        # Keychain accounts from config credentials or defaults
        claude = get_password("claude-bot") or "test-claude-pat"
        grok = get_password("grok-bot") or "test-grok-pat"
        return {"architect": claude, "developer": grok}

    def _wait_rpc(self, port: int, bearer: str, timeout_s: float) -> None:
        deadline = time.time() + timeout_s
        last: Exception | None = None
        while time.time() < deadline:
            try:
                with RunnerClient("127.0.0.1", port, bearer, timeout_s=2) as cli:
                    cli.call("health.ping")
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.3)
        raise RuntimeError(f"RPC not ready on 127.0.0.1:{port}: {last}")

    def _handle_from_row(self, row: dict[str, Any]) -> SessionHandle:
        runner = self.store.get_runner(str(row["session_key"]))
        if not runner:
            raise RuntimeError("no runner row")
        endpoint = str(runner["endpoint"])
        host, _, port_s = endpoint.partition(":")
        return SessionHandle(
            session_key=str(row["session_key"]),
            container_id=str(runner["container_id"]),
            host_port=int(port_s),
            bearer=str(runner["token"]),
            endpoint=endpoint,
            tier=str(runner.get("tier") or "hot"),
        )
