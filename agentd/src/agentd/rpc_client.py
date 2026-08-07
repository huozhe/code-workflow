"""Host-side JSON-RPC NDJSON client for agentd-runner (§14.1)."""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

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
    ) -> None:
        self.host = host
        self.port = port
        self.bearer = bearer
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._rfile = None
        self._wfile = None
        self._id = 0

    def connect(self) -> None:
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
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

    def __enter__(self) -> RunnerClient:
        self.connect()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._wfile is None or self._rfile is None:
            raise RuntimeError("not connected")
        self._id += 1
        req = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": method,
            "params": params or {},
        }
        line = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
        self._wfile.write(line)
        self._wfile.flush()
        raw = self._rfile.readline()
        if not raw:
            raise RuntimeError("RPC connection closed")
        resp = json.loads(raw.decode("utf-8"))
        if "error" in resp and resp["error"]:
            err = resp["error"]
            raise RpcError(int(err.get("code", -1)), str(err.get("message", "")))
        return resp.get("result")
