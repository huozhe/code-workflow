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
        self.roles: dict[str, str] = {}  # role -> github login
        self.initialized = False
        self._lock = threading.Lock()

    def set_bearer(self, token: str) -> None:
        self.bearer = token.encode("utf-8")

    def check_bearer(self, token: str | None) -> bool:
        if not self.bearer or token is None:
            return False
        return hmac.compare_digest(self.bearer, token.encode("utf-8"))


STATE = RunnerState()


def write_role_token(role: str, token: str) -> Path:
    """Write token to container-internal tmpfs at 0400 owned by role UID.

    Order matters under ``--cap-drop ALL`` (no CAP_DAC_OVERRIDE): write while
    the directory is still root-owned, then chown/chmod. Chowning the dir to
    0700 first makes root unable to create the file.
    """
    if role not in ROLE_UIDS:
        raise ValueError(f"unknown role {role}")
    uid = ROLE_UIDS[role]
    role_dir = TOKEN_ROOT / role
    role_dir.mkdir(parents=True, exist_ok=True)
    path = role_dir / "token"
    path.write_text(token, encoding="utf-8")
    os.chown(path, uid, uid)
    os.chmod(path, 0o400)
    os.chown(role_dir, uid, uid)
    os.chmod(role_dir, 0o700)
    return path


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
            sys.stderr.write(f"{fn_name}: {exc}\n")
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 1


def ensure_role_layout(role: str) -> dict[str, str]:
    """HOME / TMPDIR / XDG under /srv/session/<role>/ at 0700.

    Directories are created *as the role UID* so ownership is real on the
    container filesystem view. Host must leave base role dir traversable (0755).
    §7.3: fail if the role cannot write its TMPDIR.
    """
    base = Path("/srv/session") / role
    home = base / "home"
    tmp = base / "tmp"
    xdg = base / "xdg"
    # Root ensures parent exists and is traversable so the role can mkdir children.
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o755)
    except OSError as exc:
        log.warning("chmod base %s: %s", base, exc)
    for p in (base / "worktrees", base / "context", base / "scratch"):
        p.mkdir(parents=True, exist_ok=True)

    uid = ROLE_UIDS[role]
    rc = _run_as_role(
        uid,
        f"ensure_role_layout({role})",
        [str(home), str(tmp), str(xdg), str(xdg / "cache"), str(xdg / "config"), str(xdg / "data")],
    )
    if rc != 0:
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
        return ok(
            {
                "ok": True,
                "rss_bytes": rss_bytes(),
                "session_key": STATE.session_key,
                "initialized": STATE.initialized,
            }
        )

    if method in ("session.init", "session.resume"):
        session_key = str(params.get("session_key") or STATE.session_key or "")
        roles = params.get("roles") or {}
        tokens = params.get("tokens") or {}
        if not isinstance(roles, dict) or not isinstance(tokens, dict):
            return err(-32602, "roles and tokens must be objects")
        STATE.session_key = session_key
        STATE.roles = {str(k): str(v) for k, v in roles.items()}

        # Layout + tokens on tmpfs only
        for role in ROLE_UIDS:
            ensure_role_layout(role)
        for role, pat in tokens.items():
            if role in ROLE_UIDS and pat:
                write_role_token(str(role), str(pat))

        # ADR-11 identity preflight before any turn
        mismatches = identity_preflight(
            {str(k): str(v) for k, v in tokens.items()},
            STATE.roles,
        )
        if mismatches:
            STATE.initialized = False
            return err(-32010, "identity preflight failed: " + "; ".join(mismatches))

        STATE.initialized = True
        return ok(
            {
                "session_key": session_key,
                "initialized": True,
                "method": method,
                "roles": STATE.roles,
            }
        )

    if method == "session.teardown":
        # Best-effort: wipe tokens from tmpfs
        for role in ROLE_UIDS:
            p = TOKEN_ROOT / role / "token"
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        STATE.initialized = False
        return ok({"torn_down": True})

    if method == "turn.dispatch":
        # M2: runner stands up; adapters arrive in M3. Refuse real work as root path.
        if not STATE.initialized:
            return err(-32002, "session not initialized")
        role = str(params.get("role") or "")
        if role not in ROLE_UIDS:
            return err(-32602, f"unknown role {role}")
        return ok(
            {
                "status": "not_implemented",
                "note": "turn.dispatch adapters are M3; M2 validates channel only",
                "role": role,
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


# Host writes bearer here (bind-mounted session volume) — not docker Env (§5.2).
BEARER_PATH = Path("/srv/session/.runner/bearer")


def load_bearer() -> str:
    """Load RPC bearer from the session mount. Fail closed if missing (B1 shape)."""
    if BEARER_PATH.is_file():
        raw = BEARER_PATH.read_text(encoding="utf-8").strip()
        if raw:
            return raw
    env = os.environ.get("AGENTD_RUNNER_BEARER")
    if env:
        # Legacy/dev only — production host path is the file. Never invent a token.
        log.warning("bearer loaded from env (prefer %s; env is visible in docker inspect)", BEARER_PATH)
        return env
    raise SystemExit(
        f"RPC bearer missing at {BEARER_PATH}; refusing to bind "
        "(host must place bearer before start — fail closed, same shape as B1)"
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
