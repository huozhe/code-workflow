"""Session Supervisor — docker lifecycle, tokens, RPC attach (§6.5, §7.2)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentd.config import Config
from agentd.db import Store
from agentd.gitops import (
    ensure_shared_clone,
    issue_session_rel,
    project_dir_name,
    project_key_from_repo,
    project_path,
    worktree_add,
)
from agentd.keychain import get_password
from agentd.refusals import CapacityRefusal, StructuralRefusal
from agentd.rpc_client import RunnerClient

log = logging.getLogger("agentd.supervisor")

IMAGE = "agentd/session-runner:1.1.0"
RPC_CONTAINER_PORT = 7000
# Container-local rootfs path (docker cp after create). Not bind mount, not Env.
BEARER_IN_CONTAINER = "/etc/agentd/rpc.bearer"


@dataclass
class SessionHandle:
    session_key: str
    project_key: str
    container_id: str
    host_port: int
    bearer: str
    endpoint: str  # 127.0.0.1:port
    tier: str


def container_name(project_key: str) -> str:
    """Docker name for a **project** runner (§6.2 / #20)."""
    return "agentd-" + project_key.replace("/", "-")


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
            raise StructuralRefusal(
                f"FORBIDDEN: docker.sock mount on container {container_id}: {m}"
            )


# Env keys allowed on the container (allowlist — never put secrets here).
_INSPECT_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOSTNAME",
        "HOME",
        "TERM",
        "LANG",
        "LC_ALL",
        "DEBIAN_FRONTEND",
        "AGENTD_RPC_HOST",
        "AGENTD_RPC_PORT",
        "AGENTD_HOST_ROOT",
        "AGENTD_PROJECT_ROOT",
        "AGENTD_SESSION_DIR",
        "AGENTD_GITHUB_API_BASE",  # test-only stub
        "AGENTD_ADAPTER",  # test/mock adapter name
    }
)


def assert_no_secrets_in_inspect_env(container_id: str) -> None:
    """§5.2 / #11 R1 / #21 R1: no credential material in docker inspect Env.

    Allowlist of expected non-secret keys. Denylist-by-one-name is how the
    Claude oauth token slipped past the old bearer-only check.
    """
    r = _docker("inspect", container_id, "--format", "{{json .Config.Env}}")
    env_list = json.loads(r.stdout or "[]")
    forbidden: list[str] = []
    for entry in env_list:
        s = str(entry)
        key = s.split("=", 1)[0] if "=" in s else s
        # Docker / image noise: skip empty and well-known non-secret prefixes
        if key in _INSPECT_ENV_ALLOWLIST:
            continue
        if key.startswith("GPG_") or key in ("PWD", "SHLVL", "_"):
            continue
        # Anything else is unexpected — secrets must never appear as keys either.
        lower = key.lower()
        # Substring match — avoid short stems like "pat" (false-positives PATH).
        if any(
            part in lower
            for part in (
                "token",
                "secret",
                "password",
                "bearer",
                "credential",
                "api_key",
                "apikey",
                "oauth",
            )
        ):
            forbidden.append(key)
            continue
        if key in (
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "XAI_API_KEY",
            "AGENTD_RUNNER_BEARER",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ):
            forbidden.append(key)
        # Image may inject non-secret keys (PYTHONPATH, etc.) — leave those alone.
    if forbidden:
        raise StructuralRefusal(
            f"FORBIDDEN: secret or credential-shaped env in docker inspect on "
            f"{container_id}: {sorted(set(forbidden))}"
        )


# Back-compat name used by tests
assert_bearer_not_in_inspect_env = assert_no_secrets_in_inspect_env

def _host_port_from_inspect(container_id: str) -> int:
    """Resolve published host port (retry — OrbStack can lag right after start)."""
    last = ""
    for _ in range(30):
        r = _docker(
            "port",
            container_id,
            f"{RPC_CONTAINER_PORT}/tcp",
            check=False,
        )
        last = (r.stdout or "").strip()
        # e.g. "127.0.0.1:32784"
        if r.returncode == 0 and ":" in last:
            return int(last.rsplit(":", 1)[-1])
        time.sleep(0.1)
    raise RuntimeError(f"no host port published for {container_id}: {last!r}")


def assert_bearer_not_readable_by_roles(container_id: str) -> None:
    """Bearer must not be readable as either role UID (same shape as token check)."""
    for uid, label in (("1001:1001", "architect"), ("1002:1002", "developer")):
        r = _docker(
            "exec",
            "-u",
            uid,
            container_id,
            "bash",
            "-c",
            f"if [ -r {BEARER_IN_CONTAINER} ]; then echo READABLE; exit 0; "
            f"else echo DENIED; exit 1; fi",
            check=False,
        )
        if "READABLE" in (r.stdout or "") or r.returncode == 0:
            raise StructuralRefusal(
                f"FORBIDDEN: RPC bearer readable as {label} ({uid}) on {container_id}"
            )


def assert_worktree_usable(
    container_id: str,
    *,
    issue_num: int,
    role: str = "architect",
    uid: str = "1001:1001",
) -> None:
    """§6.4: relative gitdir must resolve inside the container for the role UID.

    Project layout: worktree is under sessions/<issue>/<role>/worktrees/…
    Five levels up is the project root (mounted at /srv/agentd), then repo/.git.
    """
    wt = f"/srv/agentd/sessions/{int(issue_num)}/{role}/worktrees/issue-{int(issue_num)}"
    r = _docker(
        "exec",
        "-u",
        uid,
        "-w",
        wt,
        container_id,
        "git",
        "rev-parse",
        "--git-dir",
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"worktree unusable in container for {role} at {wt}: "
            f"stdout={r.stdout!r} stderr={r.stderr!r}"
        )
    gitdir = (r.stdout or "").strip()
    log.info("worktree ok role=%s gitdir=%s", role, gitdir)


def assert_host_secrets_not_mounted(container_id: str) -> None:
    """W2: state.db / config.yaml must not be visible inside the container.

    Project container mounts exactly one project tree at /srv/agentd
    (allowlist: repo, sessions, home) — never the host agentd root.
    """
    r = _docker(
        "exec",
        "-u",
        "1001:1001",
        container_id,
        "bash",
        "-c",
        "for p in /srv/agentd/state.db /srv/agentd/config.yaml; do "
        "if [ -e \"$p\" ]; then echo VISIBLE:$p; fi; done; "
        "ls -1 /srv/agentd 2>/dev/null || true",
        check=False,
    )
    out = (r.stdout or "") + (r.stderr or "")
    if "VISIBLE:" in out:
        raise StructuralRefusal(
            f"FORBIDDEN: host secrets visible in container: {out!r}"
        )
    listed = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    allowed = {"repo", "sessions", "home"}
    for name in listed:
        if name not in allowed:
            raise StructuralRefusal(
                f"FORBIDDEN: unexpected path under /srv/agentd: {name!r} "
                f"(full={out!r}). Remove stray paths under the project tree so "
                f"only repo/, sessions/, home/ remain; then reply on the issue."
            )


class SessionSupervisor:
    def __init__(self, store: Store, config: Config) -> None:
        self.store = store
        self.config = config
        # ADR-23 / #123: one lock for admission and git gc. Created by M6-2.
        self.admit_lock = threading.Lock()

    def ensure_session(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        architect_login: str | None = None,
        developer_login: str | None = None,
        clone_url: str | None = None,
        github_api_base: str | None = None,
    ) -> SessionHandle:
        """Ensure project runner + attach this issue session (#20).

        Creates/resumes the **project** container (not per-issue), adds issue
        worktrees under the project tree, and upserts the issue session row.

        ``github_api_base`` is for integration tests only (stub GitHub API).
        """
        project_key = project_key_from_repo(repo)
        architect = architect_login or self.config.agent_login("claude") or "huozheclaude"
        developer = developer_login or self.config.agent_login("grok") or "huozhegrok"

        # #35: open project block short-circuits before clone/worktree churn.
        block = self.store.get_open_project_block(project_key)
        if block:
            raise StructuralRefusal(
                str(block.get("reason") or f"project {project_key} blocked")
            )

        existing_runner = self.store.get_runner(project_key)

        with self.admit_lock:
            return self._ensure_session_locked(
                session_key=session_key,
                repo=repo,
                issue_num=issue_num,
                project_key=project_key,
                architect=architect,
                developer=developer,
                clone_url=clone_url,
                github_api_base=github_api_base,
                existing_runner=existing_runner,
            )

    def _ensure_session_locked(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
        project_key: str,
        architect: str,
        developer: str,
        clone_url: str | None,
        github_api_base: str | None,
        existing_runner: dict[str, Any] | None,
    ) -> SessionHandle:
        if existing_runner and existing_runner.get("container_id"):
            try:
                # Ensure worktrees exist on host before ping/assert.
                self._prepare_project_issue_layout(
                    repo=repo,
                    issue_num=issue_num,
                    clone_url=clone_url,
                )
                handle = self._handle_from_runner(
                    session_key=session_key,
                    project_key=project_key,
                    runner=existing_runner,
                )
                with RunnerClient("127.0.0.1", handle.host_port, handle.bearer) as cli:
                    cli.call("health.ping")
                assert_worktree_usable(
                    handle.container_id, issue_num=issue_num, role="architect", uid="1001:1001"
                )
                assert_worktree_usable(
                    handle.container_id, issue_num=issue_num, role="developer", uid="1002:1002"
                )
                now = int(time.time())
                self.store.upsert_session(
                    session_key=session_key,
                    project_key=project_key,
                    repo=repo,
                    issue_num=issue_num,
                    state="INTAKE",
                    architect=architect,
                    developer=developer,
                    created_at=now,
                    updated_at=now,
                )
                self._register_session_layout_artifacts(
                    session_key=session_key,
                    repo=repo,
                    issue_num=issue_num,
                )
                self.store.upsert_runner(
                    project_key,
                    container_id=handle.container_id,
                    endpoint=handle.endpoint,
                    token=handle.bearer,
                    tier="hot",
                )
                return handle
            except Exception:
                log.warning(
                    "existing project runner unreachable; recreating project=%s", project_key
                )

        # §6.6 admission: HOT unit is the project container (before clone/create).
        hot = self.store.count_hot_sessions()
        cap = self.config.max_hot_containers
        if hot >= cap and not (existing_runner and existing_runner.get("tier") == "hot"):
            raise CapacityRefusal(
                f"max_hot_containers={cap} reached (hot={hot}); refusing new project "
                f"{project_key} for session {session_key}"
            )

        if not image_present():
            raise StructuralRefusal(
                f"session-runner image missing: {IMAGE}. Build/load the image on "
                f"this host, then reply on the issue to retry."
            )

        self._prepare_project_issue_layout(
            repo=repo,
            issue_num=issue_num,
            clone_url=clone_url,
        )
        proj = project_path(self.config.root, repo)

        tokens = self._load_tokens()
        bearer = generate_bearer()
        name = container_name(project_key)
        _docker("rm", "-f", name, check=False)

        env: dict[str, str] = {
            "AGENTD_RPC_HOST": "0.0.0.0",
            "AGENTD_RPC_PORT": str(RPC_CONTAINER_PORT),
            "AGENTD_HOST_ROOT": "/srv/agentd",
            "AGENTD_PROJECT_ROOT": "/srv/agentd",
            # Issue-scoped role dirs still used for context/scratch; HOME is durable.
            "AGENTD_SESSION_DIR": f"/srv/agentd/sessions/{int(issue_num)}",
        }
        # Model credentials go over session.init → tmpfs (#21 R1), never -e.
        if github_api_base:
            env["AGENTD_GITHUB_API_BASE"] = github_api_base

        # Single project mount → /srv/agentd (repo + sessions + home). W2: not host root.
        create_args = [
            "create",
            "--name",
            name,
            "--label",
            "agentd.managed=true",
            "--label",
            f"agentd.project={project_key}",
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
            f"{proj}:/srv/agentd",
            "-p",
            f"127.0.0.1:0:{RPC_CONTAINER_PORT}",
        ]
        for k, v in env.items():
            create_args.extend(["-e", f"{k}={v}"])
        create_args.append(IMAGE)

        if any("docker.sock" in a for a in create_args):
            raise StructuralRefusal(
                "FORBIDDEN: docker.sock must not appear in create args"
            )

        r = _docker(*create_args)
        cid = r.stdout.strip()

        with tempfile.TemporaryDirectory(prefix="agentd-bearer-") as td:
            host_bearer = Path(td) / "rpc.bearer"
            host_bearer.write_text(bearer, encoding="utf-8")
            os.chmod(host_bearer, stat.S_IRUSR)
            _docker("cp", str(host_bearer), f"{cid}:{BEARER_IN_CONTAINER}")

        _docker("start", cid)
        assert_no_docker_sock_mount(cid)
        assert_bearer_not_in_inspect_env(cid)
        assert_bearer_not_readable_by_roles(cid)
        assert_host_secrets_not_mounted(cid)
        assert_worktree_usable(
            cid, issue_num=issue_num, role="architect", uid="1001:1001"
        )
        assert_worktree_usable(
            cid, issue_num=issue_num, role="developer", uid="1002:1002"
        )
        host_port = _host_port_from_inspect(cid)
        endpoint = f"127.0.0.1:{host_port}"

        self._wait_rpc(host_port, bearer, timeout_s=30)

        with RunnerClient("127.0.0.1", host_port, bearer) as cli:
            cli.call(
                "session.init",
                {
                    "session_key": session_key,
                    "project_key": project_key,
                    "roles": {"architect": architect, "developer": developer},
                    "tokens": tokens,
                    # Model creds over control channel → tmpfs (#21 R1), never Env.
                    "model_credentials": self._load_model_credentials(),
                },
            )
            ping = cli.call("health.ping")
            log.info(
                "health.ping ok project=%s session=%s rss=%s",
                project_key,
                session_key,
                ping.get("rss_bytes"),
            )

        now = int(time.time())
        self.store.upsert_session(
            session_key=session_key,
            project_key=project_key,
            repo=repo,
            issue_num=issue_num,
            state="INTAKE",
            architect=architect,
            developer=developer,
            created_at=now,
            updated_at=now,
        )
        self._register_session_layout_artifacts(
            session_key=session_key,
            repo=repo,
            issue_num=issue_num,
        )
        self.store.upsert_runner(
            project_key,
            container_id=cid,
            endpoint=endpoint,
            token=bearer,
            tier="hot",
        )
        return SessionHandle(
            session_key=session_key,
            project_key=project_key,
            container_id=cid,
            host_port=host_port,
            bearer=bearer,
            endpoint=endpoint,
            tier="hot",
        )

    def demote_cold(self, project_key: str) -> None:
        """COLD = docker stop for a **project** runner (§6.5)."""
        row = self.store.get_runner(project_key)
        if not row or not row.get("container_id"):
            return
        cid = str(row["container_id"])
        _docker("stop", cid, check=False)
        self.store.upsert_runner(
            project_key,
            container_id=cid,
            endpoint=str(row.get("endpoint") or ""),
            token=str(row.get("token") or ""),
            tier="cold",
        )
        log.info("project %s → COLD (docker stop)", project_key)

    def promote_hot(self, session_key: str) -> SessionHandle:
        """Promote project runner to HOT (§6.5/§6.6 admission on promote path)."""
        row = self.store.get_session(session_key)
        if not row:
            raise RuntimeError(f"unknown session {session_key}")
        project_key = str(row.get("project_key") or row.get("repo") or "")
        runner = self.store.get_runner(project_key)
        if not runner:
            raise RuntimeError(f"unknown project runner {project_key}")

        if str(runner.get("tier") or "") != "hot":
            hot = self.store.count_hot_sessions()
            cap = self.config.max_hot_containers
            if hot >= cap:
                raise CapacityRefusal(
                    f"max_hot_containers={cap} reached (hot={hot}); refusing promote "
                    f"of project {project_key}"
                )

        cid = str(runner["container_id"])
        bearer = str(runner["token"])
        _docker("start", cid, check=True)
        host_port = _host_port_from_inspect(cid)
        self._wait_rpc(host_port, bearer, timeout_s=30)
        tokens = self._load_tokens()
        retired = [
            {"turn_id": t.get("turn_id"), "reason": t.get("summary")}
            for t in self.store.list_turns(session_key)
            if t.get("status") == "interrupted"
        ]
        with RunnerClient("127.0.0.1", host_port, bearer) as cli:
            cli.call(
                "session.resume",
                {
                    "session_key": session_key,
                    "project_key": project_key,
                    "roles": {
                        "architect": row["architect"],
                        "developer": row["developer"],
                    },
                    "tokens": tokens,
                    "model_credentials": self._load_model_credentials(),
                    "missed": {"retired_turns": retired[-10:]},
                },
            )
            cli.call("health.ping")
        endpoint = f"127.0.0.1:{host_port}"
        self.store.upsert_runner(
            project_key,
            container_id=cid,
            endpoint=endpoint,
            token=bearer,
            tier="hot",
        )
        return SessionHandle(
            session_key=session_key,
            project_key=project_key,
            container_id=cid,
            host_port=host_port,
            bearer=bearer,
            endpoint=endpoint,
            tier="hot",
        )

    def adversarial_token_check(self, handle: SessionHandle) -> dict[str, Any]:
        """Cross-role token reads must fail both directions (§5.2)."""

        def _try(uid: str, path: str) -> bool:
            script = (
                f"if [ -r {path} ]; then echo READABLE; exit 0; "
                f"else echo DENIED; exit 1; fi"
            )
            r = _docker(
                "exec",
                "-u",
                uid,
                handle.container_id,
                "bash",
                "-c",
                script,
                check=False,
            )
            return "READABLE" in (r.stdout or "") or r.returncode == 0

        dev_reads_arch = _try("1002:1002", "/run/agent/architect/token")
        arch_reads_dev = _try("1001:1001", "/run/agent/developer/token")
        return {
            "developer_can_read_architect_token": dev_reads_arch,
            "architect_can_read_developer_token": arch_reads_dev,
        }

    def _prepare_project_issue_layout(
        self,
        *,
        repo: str,
        issue_num: int,
        clone_url: str | None,
    ) -> Path:
        """Create project dirs + issue worktrees. Returns project host path."""
        proj = project_path(self.config.root, repo)
        for role in ("architect", "developer"):
            (proj / "home" / role).mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(proj / "home" / role, 0o755)
            except OSError:
                pass
        issue_host = proj / issue_session_rel(issue_num)
        for role in ("architect", "developer"):
            for sub in ("worktrees", "context", "scratch"):
                (issue_host / role / sub).mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(issue_host / role, 0o755)
            except OSError:
                pass
            (issue_host / role / "transcript.jsonl").touch(exist_ok=True)

        clone = ensure_shared_clone(self.config.root, repo, clone_url=clone_url)
        wt_arch = issue_host / "architect" / "worktrees" / f"issue-{issue_num}"
        wt_dev = issue_host / "developer" / "worktrees" / f"issue-{issue_num}"
        branch_prefix = f"agentd/{project_dir_name(repo)}/{issue_num}"
        worktree_add(clone, wt_arch, f"{branch_prefix}/architect")
        worktree_add(clone, wt_dev, f"{branch_prefix}/developer")
        return proj

    def _register_session_layout_artifacts(
        self,
        *,
        session_key: str,
        repo: str,
        issue_num: int,
    ) -> list[dict[str, str]]:
        """Ledger what ensure_session *created* — not model-reported (M3-C / §10.5).

        Registration is driven by supervisor-observed worktrees, branches, and
        scratch dirs so M5 teardown has a non-empty ledger even when adapters
        return ``artifacts: []``.
        """
        proj = project_path(self.config.root, repo)
        issue_host = proj / issue_session_rel(issue_num)
        branch_prefix = f"agentd/{project_dir_name(repo)}/{issue_num}"
        registered: list[dict[str, str]] = []
        for role in ("architect", "developer"):
            wt = issue_host / role / "worktrees" / f"issue-{issue_num}"
            branch = f"{branch_prefix}/{role}"
            scratch = issue_host / role / "scratch"
            for kind, ref in (
                ("worktree", str(wt)),
                ("branch", branch),
                ("scratch", str(scratch)),
            ):
                self.store.register_artifact(
                    session_key=session_key,
                    role=role,
                    kind=kind,
                    ref=ref,
                )
                registered.append({"role": role, "kind": kind, "ref": ref})
        log.info(
            "artifact ledger session=%s registered=%s (supervisor-observed)",
            session_key,
            len(registered),
        )
        return registered

    def _load_tokens(self) -> dict[str, str]:
        """Keychain PATs only — fail closed on miss (ADR-11 / B1 shape)."""
        claude = get_password("claude-bot")
        grok = get_password("grok-bot")
        missing = []
        if not claude:
            missing.append("claude-bot")
        if not grok:
            missing.append("grok-bot")
        if missing:
            raise RuntimeError(
                "PAT(s) missing from Keychain/env: "
                + ", ".join(missing)
                + "; refusing session start (ADR-11 fail-closed)"
            )
        return {"architect": claude, "developer": grok}

    def _load_model_credentials(self) -> dict[str, str]:
        """Subscription model credentials (optional at create; empty if unset).

        Delivered via session.init → container tmpfs, never docker Env (§5.2).
        """
        out: dict[str, str] = {}
        oauth = get_password("claude-oauth-token")
        if oauth:
            out["claude_oauth_token"] = oauth
        return out
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

    def _handle_from_runner(
        self,
        *,
        session_key: str,
        project_key: str,
        runner: dict[str, Any],
    ) -> SessionHandle:
        endpoint = str(runner["endpoint"])
        _, _, port_s = endpoint.partition(":")
        return SessionHandle(
            session_key=session_key,
            project_key=project_key,
            container_id=str(runner["container_id"]),
            host_port=int(port_s),
            bearer=str(runner["token"]),
            endpoint=endpoint,
            tier=str(runner.get("tier") or "hot"),
        )
