"""JSON-RPC 2.0 NDJSON runner — §14.1 / §14.2 (M2 subset)."""

from __future__ import annotations

import hmac
import json
import logging
import os
import resource
import secrets
import socket
import socketserver
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


def ensure_role_layout(role: str) -> dict[str, str]:
    """HOME / TMPDIR / XDG under /srv/session/<role>/ at 0700."""
    base = Path("/srv/session") / role
    home = base / "home"
    tmp = base / "tmp"
    xdg = base / "xdg"
    for p in (home, tmp, xdg, base / "worktrees", base / "context", base / "scratch"):
        p.mkdir(parents=True, exist_ok=True)
    uid = ROLE_UIDS[role]
    for p in (base, home, tmp, xdg):
        try:
            os.chown(p, uid, uid)
            os.chmod(p, 0o700)
        except OSError:
            # Host bind mounts may not allow chown; still set mode best-effort.
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass
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

    expected: role -> github login
    tokens: role -> pat
    """
    errors: list[str] = []
    try:
        import urllib.error
        import urllib.request
    except ImportError:
        return ["urllib unavailable"]

    for role, pat in tokens.items():
        want = expected.get(role)
        if not want:
            errors.append(f"{role}: no configured login")
            continue
        if not pat:
            errors.append(f"{role}: empty token")
            continue
        # Skip real GitHub when using test placeholders
        if pat.startswith("test-") or os.environ.get("AGENTD_SKIP_PREFLIGHT") == "1":
            log.info("identity preflight skipped for %s (test mode)", role)
            continue
        req = urllib.request.Request(
            "https://api.github.com/user",
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


def serve(host: str, port: int) -> None:
    # Bearer from env at start (host injects via docker -e); never log the value.
    bearer = os.environ.get("AGENTD_RUNNER_BEARER")
    if not bearer:
        bearer = secrets.token_urlsafe(32)
        log.warning("AGENTD_RUNNER_BEARER unset; generated ephemeral bearer (host must know it)")
    STATE.set_bearer(bearer)
    # Ensure token root exists (tmpfs mount)
    TOKEN_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(TOKEN_ROOT, 0o711)

    with _ThreadedTCPServer((host, port), _RPCHandler) as server:
        log.info("agentd-runner listening on %s:%s", host, port)
        server.serve_forever()
