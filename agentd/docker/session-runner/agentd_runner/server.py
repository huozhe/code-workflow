"""JSON-RPC 2.0 NDJSON runner — §14.1 / §14.2 (M2 subset)."""

from __future__ import annotations

import hmac
import json
import logging
import os
import resource
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("agentd_runner")

UID_ARCHITECT = 1001
UID_DEVELOPER = 1002
ROLE_UIDS = {"architect": UID_ARCHITECT, "developer": UID_DEVELOPER}
TOKEN_ROOT = Path("/run/agent")


class RunnerState:
    def __init__(self) -> None:
        self.bearer: bytes | None = None
        self.attached = False
        self.session_key: str | None = None
        self.project_key: str | None = None
        self.roles: dict[str, str] = {}  # role -> github login
        self.initialized = False
        self.missed: dict[str, Any] = {}
        self._lock = threading.Lock()
        # One CLI conversation per role — serialize turns project-wide (#20).
        self._role_locks: dict[str, threading.Lock] = {
            "architect": threading.Lock(),
            "developer": threading.Lock(),
        }

    def set_bearer(self, token: str) -> None:
        self.bearer = token.encode("utf-8")

    def check_bearer(self, token: str | None) -> bool:
        if not self.bearer or token is None:
            return False
        return hmac.compare_digest(self.bearer, token.encode("utf-8"))


STATE = RunnerState()


def write_role_secret(role: str, name: str, value: str) -> Path:
    """Write a secret file under /run/agent/<role>/ at 0400 owned by role UID.

    Order matters under ``--cap-drop ALL`` (no CAP_DAC_OVERRIDE): the role
    dir must be root-owned and writable when creating the file. If a prior
    secret already set the dir to role 0700, re-open it as root first.
    """
    if role not in ROLE_UIDS:
        raise ValueError(f"unknown role {role}")
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"invalid secret name {name!r}")
    uid = ROLE_UIDS[role]
    role_dir = TOKEN_ROOT / role
    role_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Reclaim dir so a second secret can be written after the first chown.
        os.chown(role_dir, 0, 0)
        os.chmod(role_dir, 0o755)
    except OSError:
        pass
    path = role_dir / name
    path.write_text(value, encoding="utf-8")
    try:
        os.chown(path, uid, uid)
        os.chmod(path, 0o400)
        os.chown(role_dir, uid, uid)
        os.chmod(role_dir, 0o700)
    except OSError as exc:
        if os.geteuid() == 0:
            raise
        # Unprivileged test host: leave file owned by current euid.
        log.warning("chown secret %s for %s failed on euid=%s: %s", name, role, os.geteuid(), exc)
        try:
            os.chmod(path, 0o600)
            os.chmod(role_dir, 0o700)
        except OSError:
            pass
    return path

def write_role_token(role: str, token: str) -> Path:
    """Write GitHub PAT to container-internal tmpfs at 0400 owned by role UID."""
    return write_role_secret(role, "token", token)


def _run_as_role(uid: int, fn_name: str, paths: list[str]) -> int:
    """Fork, setuid(role), create 0700 dirs. Returns child exit code."""
    pid = os.fork()
    if pid == 0:
        try:
            os.setgid(uid)
            os.setuid(uid)
            for p in paths:
                Path(p).mkdir(parents=True, exist_ok=True)
                os.chmod(p, 0o700)
            # write probe in tmp (last path is tmp)
            probe = Path(paths[-1]) / f".agentd-write-probe-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            os._exit(0)
        except Exception as exc:  # noqa: BLE001
            # os.write is async-signal/fork-safe; sys.stderr can deadlock if
            # another thread holds the stream lock at fork time.
            try:
                os.write(2, f"{fn_name}: {exc}\n".encode())
            except OSError:
                pass
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 1


def project_root() -> Path:
    """Project tree mount point (#20) — durable homes live here."""
    return Path(
        os.environ.get("AGENTD_PROJECT_ROOT")
        or os.environ.get("AGENTD_HOST_ROOT")
        or "/srv/agentd"
    )


def session_base(issue_num: int | None = None) -> Path:
    """Session directory inside the container.

    When *issue_num* is set, derive ``<project>/sessions/<issue>`` so one
    project container cannot pin writes to whichever issue created it (#76).
    AGENTD_SESSION_DIR is only the fallback for callers that have no issue
    (session.init / resume). Legacy /srv/session is last.
    """
    if issue_num is not None:
        return project_root() / "sessions" / str(int(issue_num))
    override = os.environ.get("AGENTD_SESSION_DIR")
    if override:
        return Path(override)
    return Path("/srv/session")


def ensure_role_layout(role: str, issue_num: int | None = None) -> dict[str, str]:
    """Create role runtime dirs as the role UID (0700) and, when known, the issue tree.

    TMPDIR / XDG live under the project mount, not under any issue (#76 B1).
    Issue-scoped ``context`` / ``scratch`` / ``worktrees`` are created only when
    *issue_num* is set — init has no issue and must not recreate AGENTD_SESSION_DIR.
    §7.3: fail if the role cannot write its TMPDIR.
    """
    from agentd_runner.turn import role_paths

    paths_map = role_paths(role, issue_num)
    home = paths_map["home"]
    tmp = paths_map["tmp"]
    xdg = paths_map["xdg"]
    # Root creates project-level parents so the role can mkdir children.
    tmp.parent.mkdir(parents=True, exist_ok=True)
    xdg.parent.mkdir(parents=True, exist_ok=True)
    home.parent.mkdir(parents=True, exist_ok=True)
    if issue_num is not None:
        base = paths_map["base"]
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o755)
        except OSError as exc:
            log.warning("chmod base %s: %s", base, exc)
        for p in (paths_map["worktrees"], paths_map["context"], paths_map["scratch"]):
            p.mkdir(parents=True, exist_ok=True)

    uid = ROLE_UIDS[role]
    # Durable HOME is host-created (755). Do not chmod it as the role.
    paths = [str(tmp), str(xdg), str(xdg / "cache"), str(xdg / "config"), str(xdg / "data")]
    rc = _run_as_role(uid, f"ensure_role_layout({role})", paths)
    if rc != 0:
        # Unit tests on macOS cannot setuid(1001); create as current euid with warning.
        # Production containers run as root with CAP_SETUID — setuid path must work there.
        if os.geteuid() != 0:
            for p in paths:
                Path(p).mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
            log.warning(
                "setuid role layout failed for %s on euid=%s; using unprivileged fallback (test host)",
                role,
                os.geteuid(),
            )
        else:
            st = tmp.stat() if tmp.exists() else None
            raise RuntimeError(
                f"role {role} cannot establish TMPDIR {tmp} "
                f"(stat={None if st is None else (st.st_uid, oct(st.st_mode))}); "
                "§7.3 requires a writable 0700 per-role temp path"
            )
    log.info(
        "role layout ok role=%s tmp=%s uid=%s mode=%s",
        role,
        tmp,
        tmp.stat().st_uid,
        oct(tmp.stat().st_mode),
    )

    return {
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
    }


def rss_bytes() -> int:
    try:
        # ru_maxrss is KB on Linux
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return 0


def identity_preflight(tokens: dict[str, str], expected: dict[str, str]) -> list[str]:
    """GET /user per token; return list of mismatch descriptions (empty = ok).

    Always hits the API (or AGENTD_GITHUB_API_BASE for integration tests against
    a local stub). No production-token shape skips the check — ADR-11 fail-closed.
    """
    errors: list[str] = []
    try:
        import urllib.request
    except ImportError:
        return ["urllib unavailable"]

    base = os.environ.get("AGENTD_GITHUB_API_BASE", "https://api.github.com").rstrip("/")
    user_url = f"{base}/user"

    for role, pat in tokens.items():
        want = expected.get(role)
        if not want:
            errors.append(f"{role}: no configured login")
            continue
        if not pat:
            errors.append(f"{role}: empty token")
            continue
        req = urllib.request.Request(
            user_url,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "agentd-runner",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            login = data.get("login")
            if login != want:
                errors.append(f"{role}: token login={login!r} != configured {want!r}")
            else:
                log.info("identity preflight ok role=%s login=%s", role, login)
        except Exception as exc:  # noqa: BLE001 — surface as preflight failure
            errors.append(f"{role}: GET /user failed: {exc}")
    return errors


def handle_request(req: dict[str, Any], authed: bool) -> dict[str, Any]:
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if method == "session.attach":
        token = params.get("bearer") or params.get("token")
        if not STATE.check_bearer(str(token) if token is not None else None):
            return err(-32001, "invalid bearer")
        STATE.attached = True
        return ok({"attached": True, "session_key": STATE.session_key})

    if not authed and method != "session.attach":
        return err(-32001, "not attached")

    if method == "health.ping":
        from agentd_runner import cli_session as _cli

        return ok(
            {
                "ok": True,
                "rss_bytes": rss_bytes(),
                "cli_rss_kb": _cli.rss_snapshot(),
                "session_key": STATE.session_key,
                "initialized": STATE.initialized,
            }
        )

    if method in ("session.init", "session.resume"):
        from agentd_runner.turn import load_rehydration

        session_key = str(params.get("session_key") or STATE.session_key or "")
        roles = params.get("roles") or {}
        tokens = params.get("tokens") or {}
        model_creds = params.get("model_credentials") or {}
        if not isinstance(roles, dict) or not isinstance(tokens, dict):
            return err(-32602, "roles and tokens must be objects")
        if not isinstance(model_creds, dict):
            return err(-32602, "model_credentials must be an object")
        STATE.session_key = session_key
        STATE.roles = {str(k): str(v) for k, v in roles.items()}

        # Project-scoped TMPDIR/XDG as the role UID (§7.3). No issue in
        # scope — do not mkdir AGENTD_SESSION_DIR (#76 B1/B2).
        for role in ROLE_UIDS:
            ensure_role_layout(role)
        for role, pat in tokens.items():
            if role in ROLE_UIDS and pat:
                write_role_token(str(role), str(pat))
        # Claude oauth is account-wide and may be used by either role when
        # §5.3 swaps adapter (role/architect:grok etc.). Deliberately write a
        # copy into *both* role tmpfs dirs — not a bug; select by adapter at
        # turn time, not by which role "owns" the provider.
        claude_oauth = str(model_creds.get("claude_oauth_token") or "").strip()
        if claude_oauth:
            for role in ROLE_UIDS:
                write_role_secret(role, "claude_oauth_token", claude_oauth)

        # ADR-11 identity preflight before any turn (GitHub PATs only)
        mismatches = identity_preflight(
            {str(k): str(v) for k, v in tokens.items()},
            STATE.roles,
        )
        if mismatches:
            STATE.initialized = False
            return err(-32010, "identity preflight failed: " + "; ".join(mismatches))

        STATE.initialized = True
        rehydrated = {}
        if method == "session.resume":
            missed = params.get("missed")
            if isinstance(missed, dict):
                STATE.missed = missed
            # §6.3 continuity by persistence — load transcript/summary for both roles
            rehydrated = {
                role: load_rehydration(role) for role in ROLE_UIDS
            }
            log.info(
                "session.resume rehydrated transcripts arch=%s dev=%s",
                rehydrated.get("architect", {}).get("transcript_lines"),
                rehydrated.get("developer", {}).get("transcript_lines"),
            )
        # #25: spawn one long-lived CLI per role (setuid in child). resume uses -c.
        from agentd_runner.turn import ensure_role_cli_spawned

        continue_sess = method == "session.resume"
        cli_spawn = {
            role: ensure_role_cli_spawned(role, continue_session=continue_sess)
            for role in ROLE_UIDS
        }
        return ok(
            {
                "session_key": session_key,
                "initialized": True,
                "method": method,
                "roles": STATE.roles,
                "rehydrated": rehydrated,
                "cli_spawn": cli_spawn,
            }
        )

    if method == "session.teardown":
        from agentd_runner import cli_session as _cli

        _cli.shutdown_all()
        # Best-effort: wipe secrets from tmpfs
        for role in ROLE_UIDS:
            role_dir = TOKEN_ROOT / role
            if not role_dir.is_dir():
                continue
            for p in role_dir.iterdir():
                try:
                    p.unlink()
                except OSError:
                    pass
        STATE.initialized = False
        return ok({"torn_down": True})

    if method in ("turn.dispatch", "turn.resume"):
        from agentd_runner.turn import exec_turn_as_role

        if not STATE.initialized:
            return err(-32002, "session not initialized")
        role = str(params.get("role") or "")
        if role not in ROLE_UIDS:
            return err(-32602, f"unknown role {role}")
        # turn.resume: runner re-derives from workspace; same exec path with flag
        if method == "turn.resume":
            params = dict(params)
            params["resuming"] = True
        # Serialize per role so concurrent issues cannot interleave one conversation.
        # Progress notifications (no id) may be written mid-turn via the active handler.
        progress = params.pop("_progress_cb", None) if isinstance(params, dict) else None
        with STATE._role_locks[role]:
            result = exec_turn_as_role(params, progress=progress)
        return ok(result)

    if method == "escalate.human":
        # Runner-side record; gateway pauses session on seeing needs_human / escalate
        reason = str(params.get("reason") or "unspecified")
        question = str(params.get("question") or "")
        log.warning("escalate.human reason=%s question=%s", reason, question)
        return ok(
            {
                "status": "needs_human",
                "reason": reason,
                "question": question,
            }
        )

    # artifact.register is Runner → Gateway (notification). The gateway client
    # drains no-id frames; if a misconfigured peer calls it as a request, accept.
    if method == "artifact.register":
        return ok(
            {
                "accepted": True,
                "kind": params.get("kind"),
                "ref": params.get("ref"),
                "role": params.get("role"),
            }
        )

    return err(-32601, f"method not found: {method}")


class _RPCHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        authed = False
        first = True
        peer = self.client_address
        log.info("connection from %s", peer)
        while True:
            line = self.rfile.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                self._write(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
                )
                continue
            method = req.get("method")
            if first and method != "session.attach":
                log.warning("first frame was %s, expected session.attach — closing", method)
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "error": {"code": -32001, "message": "first frame must be session.attach"},
                    }
                )
                break
            first = False
            if method == "session.attach":
                resp = handle_request(req, authed=False)
                if "result" in resp:
                    authed = True
                self._write(resp)
                if not authed:
                    break
                continue
            if not authed:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "error": {"code": -32001, "message": "not attached"},
                    }
                )
                break
            try:
                # Inject notify.progress writer for long-lived turn streams (§14.2 / #25).
                if method in ("turn.dispatch", "turn.resume"):
                    params = req.get("params")
                    if not isinstance(params, dict):
                        params = {}
                        req = dict(req)
                        req["params"] = params
                    turn_id = str(params.get("turn_id") or "")
                    role = str(params.get("role") or "")

                    def _progress(chunk_info: dict) -> None:
                        self._write(
                            {
                                "jsonrpc": "2.0",
                                "method": "notify.progress",
                                "params": {
                                    "turn_id": turn_id,
                                    "role": role,
                                    **chunk_info,
                                },
                            }
                        )

                    params["_progress_cb"] = _progress
                self._write(handle_request(req, authed=True))
            except Exception as exc:  # noqa: BLE001
                log.exception("handler error method=%s", method)
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": req.get("id"),
                        "error": {"code": -32000, "message": f"internal error: {exc}"},
                    }
                )

    def _write(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# Host injects via: docker create → docker cp → docker start.
# Container-local rootfs (NOT bind mount — roles can read bind mounts, §5.2).
BEARER_PATH = Path("/etc/agentd/rpc.bearer")


def load_bearer() -> str:
    """Load RPC bearer from container-local rootfs. Fail closed if missing."""
    if BEARER_PATH.is_file():
        raw = BEARER_PATH.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    env = os.environ.get("AGENTD_RUNNER_BEARER")
    if env:
        # Dev-only fallback (unit tests). Supervisor never sets this in production
        # and asserts its absence from inspect Env.
        log.warning(
            "bearer loaded from env (dev only; production uses docker cp to %s)",
            BEARER_PATH,
        )
        return env
    raise SystemExit(
        f"RPC bearer missing at {BEARER_PATH}; refusing to bind "
        "(host: docker create → docker cp → docker start — fail closed, B1 shape)"
    )


def serve(host: str, port: int) -> None:
    bearer = load_bearer()
    STATE.set_bearer(bearer)
    # Ensure token root exists (tmpfs mount)
    TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(TOKEN_ROOT, 0o711)

    with _ThreadedTCPServer((host, port), _RPCHandler) as server:
        log.info("agentd-runner listening on %s:%s", host, port)
        server.serve_forever()
