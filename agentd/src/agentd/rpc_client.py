"""Host-side JSON-RPC NDJSON client for agentd-runner (§14.1)."""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Self

log = logging.getLogger("agentd.rpc_client")


class RpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


class RunnerClient:
    def __init__(
        self,
        host: str,
        port: int,
        bearer: str,
        *,
        timeout_s: float = 30.0,
        on_notification: Any | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.bearer = bearer
        self.timeout_s = timeout_s
        # Runner → gateway notifications (notify.progress, artifact.register).
        self.on_notification = on_notification
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._id = 0

    def connect(self) -> None:
        self.close()
        try:
            # Connect/handshake stay short; long timeout is for turn.dispatch
            # reads (deadline_s + grace). A dead runner must fail fast.
            connect_s = min(float(self.timeout_s), 30.0)
            s = socket.create_connection((self.host, self.port), timeout=connect_s)
            s.settimeout(self.timeout_s)
            self._sock = s
            self._rfile = s.makefile("rb")
            self._wfile = s.makefile("wb")
            # First frame must be session.attach
            self.call("session.attach", {"bearer": self.bearer})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        for f in (self._rfile, self._wfile):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = self._rfile = self._wfile = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._wfile is None or self._rfile is None:
            raise RuntimeError("not connected")
        self._id += 1
        req_id = self._id
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        line = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
        self._wfile.write(line)
        self._wfile.flush()
        # Drain notify.* frames (no id) until the matching response (#25 / §14.2).
        while True:
            raw = self._rfile.readline()
            if not raw:
                raise RuntimeError("RPC connection closed")
            resp = json.loads(raw.decode("utf-8"))
            if resp.get("id") is None and resp.get("method"):
                method_n = str(resp.get("method") or "")
                params_n = resp.get("params") if isinstance(resp.get("params"), dict) else {}
                log.debug("rpc notify %s params=%s", method_n, params_n)
                if self.on_notification is not None:
                    try:
                        self.on_notification(method_n, params_n)
                    except Exception:
                        log.exception("on_notification failed method=%s", method_n)
                continue
            if resp.get("id") != req_id and resp.get("id") is not None:
                log.warning(
                    "rpc unexpected id %s (want %s); discarding", resp.get("id"), req_id
                )
                continue
            if resp.get("error"):
                err = resp["error"]
                raise RpcError(int(err.get("code", -1)), str(err.get("message", "")))
            return resp.get("result")
