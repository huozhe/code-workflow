"""Host-side JSON-RPC NDJSON client for agentd-runner (§14.1)."""

from __future__ import annotations

import itertools
import json
import logging
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any, Self

# ADR-29 (c): process-unique request ids. Per-instance counters reset on every
# `with RunnerClient` so turn.dispatch was always id=2. next() on a C iterator
# is atomic under the GIL.
_req_ids = itertools.count(1)

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
        self._rfile: IO[bytes] | None = None
        self._wfile: IO[bytes] | None = None

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
        req_id = next(_req_ids)
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
                params_n = (
                    resp.get("params") if isinstance(resp.get("params"), dict) else {}
                )
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


@dataclass(frozen=True)
class ProbeResult:
    """One probe, three answers: serviceable, why not, and the reply (ADR-34)."""

    serviceable: bool
    reason: str
    payload: dict[str, Any] | None = None


def probe_runner(
    runner: Mapping[str, Any],
    *,
    timeout_s: float = 2.0,
    client_cls: type[RunnerClient] | None = None,
) -> ProbeResult:
    """Serviceable ping — answering is not enough (ADR-25), shared by both callers.

    The reason is derived from *where* the probe failed, not from a flag, because
    a stale ``runners.endpoint`` (which does not survive stop/start — ADR-25) is a
    running, initialised runner that fails at connect and would otherwise log
    identically to one that answered ``initialized: false``:

    ``attached``      serviceable.
    ``no_endpoint``   the row cannot address a runner. No connect attempted; an
                      unparseable row must not read as a container in trouble.
    ``unreachable``   the transport failed — stale endpoint, or a dead process
                      behind a live port mapping.
    ``uninitialised`` it answered but cannot serve a turn (the post-reboot case).

    ``stopped`` is the caller's to report and must be reached without a socket;
    see ``Reconciler._probe_attachments`` (ADR-34 acceptance (11)).

    ``client_cls`` is the transport seam. Callers pass their own module's
    ``RunnerClient`` so a test that patches it there keeps intercepting this
    probe as well as that module's other RPC — the predicate moved modules, the
    seams did not.
    """
    endpoint = str(runner.get("endpoint") or "")
    host, _, port_s = endpoint.partition(":")
    token = str(runner.get("token") or runner.get("runner_token") or "")
    if not host or not port_s or not token:
        return ProbeResult(False, "no_endpoint")
    try:
        port = int(port_s)
    except ValueError:
        return ProbeResult(False, "no_endpoint")
    try:
        cls = client_cls or RunnerClient
        with cls(host, port, token, timeout_s=timeout_s) as cli:
            ping = cli.call("health.ping")
    except Exception:  # noqa: BLE001 — ping probe
        return ProbeResult(False, "unreachable")
    if isinstance(ping, dict) and ping.get("initialized") is True:
        return ProbeResult(True, "attached", ping)
    return ProbeResult(False, "uninitialised", ping if isinstance(ping, dict) else None)
