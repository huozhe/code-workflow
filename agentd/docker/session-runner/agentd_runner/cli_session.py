"""Long-lived per-role CLI sessions held by agentd-runner (§6.3 / #25).

Spawn once per role (after setuid in the child), multiplex turns over stdio.
One-shot ``-p`` adapters remain the recovery path (ADR-9 / §14.5).

Cwd decision (#25): spawn at project root ``/srv/agentd``; each turn's
digest/prompt names the issue worktree — cross-issue context is retained in
the vendor conversation without rebinding the process cwd.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, IO, TextIO

from agentd_runner.public_actions import (
    dedupe_actions,
    from_claude_stream_obj,
    from_grok_session_update,
)

log = logging.getLogger("agentd_runner.cli_session")

# Global registry: one live session per role (project container).
_SESSIONS: dict[str, "LiveCliSession"] = {}
_REGISTRY_LOCK = threading.Lock()

def project_root() -> Path:
    return Path(
        os.environ.get("AGENTD_PROJECT_ROOT")
        or os.environ.get("AGENTD_HOST_ROOT")
        or "/srv/agentd"
    )


def use_oneshot(adapter: str) -> bool:
    """True → disposable -p path (mock/script, or explicit recovery)."""
    if adapter in ("mock", "script"):
        return True
    mode = (os.environ.get("AGENTD_CLI_MODE") or "").strip().lower()
    return mode in ("oneshot", "one-shot", "p", "-p")


def _drop_privs(uid: int) -> None:
    try:
        os.setgroups([])
    except OSError:
        pass
    os.setgid(uid)
    os.setuid(uid)
    if os.geteuid() == 0:
        raise RuntimeError("setuid failed; still euid 0")


def _role_env(role: str, home: Path, tmp: Path, xdg: Path) -> dict[str, str]:
    """Non-secret env only.

    GH_TOKEN / CLAUDE_CODE_OAUTH_TOKEN live on tmpfs at 0400 role-owned.
    Under §7.2 (no CAP_DAC_OVERRIDE) container root cannot read them — B5.
    The spawn wrapper loads secrets *after* the privilege drop.
    """
    _ = role  # reserved for future non-secret role hints
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "XDG_CACHE_HOME": str(xdg / "cache"),
            "XDG_CONFIG_HOME": str(xdg / "config"),
            "XDG_DATA_HOME": str(xdg / "data"),
            "PATH": env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        }
    )
    # Drop any inherited secrets from the runner process environment.
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def _wrap_with_role_secrets(role: str, cmd: list[str]) -> list[str]:
    """bash wrapper: as role UID, cat secrets then exec vendor CLI (B5)."""
    if role not in ("architect", "developer"):
        raise ValueError(f"unknown role {role!r}")
    # $1 = role; remaining argv = vendor command.
    script = (
        'set -e\n'
        'ROLE="$1"; shift\n'
        'TOK="/run/agent/${ROLE}/token"\n'
        'if [ -r "$TOK" ]; then\n'
        '  export GH_TOKEN="$(cat "$TOK")"\n'
        '  export GITHUB_TOKEN="$GH_TOKEN"\n'
        'fi\n'
        'OAUTH="/run/agent/${ROLE}/claude_oauth_token"\n'
        'if [ -r "$OAUTH" ]; then\n'
        '  export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$OAUTH")"\n'
        'fi\n'
        'exec "$@"\n'
    )
    return ["bash", "-c", script, "role-secret-wrap", role, *cmd]


def _pick_permission_option(params: dict[str, Any]) -> str | None:
    """Choose an allow option from the request's offered list (B6).

    Edit tools offer ``allow-edits-session``; execute tools offer
    ``allow-once`` / ``reject-once``. Hardcoding either id fails the other.
    Preference: allow_always → allow-edits-session → allow-once → first allow* → first.
    """
    options = params.get("options") or []
    ids: list[str] = []
    for opt in options:
        if isinstance(opt, dict) and opt.get("optionId"):
            ids.append(str(opt["optionId"]))
    if not ids:
        return None
    for preferred in (
        "allow_always",
        "allow-always",
        "allow-edits-session",
        "allow_once",
        "allow-once",
    ):
        if preferred in ids:
            return preferred
    for oid in ids:
        low = oid.lower()
        if "allow" in low and "reject" not in low:
            return oid
    return ids[0]


def _is_jsonrpc_response(obj: dict[str, Any]) -> bool:
    """Response = has result/error and no method (B2: id spaces collide)."""
    if "method" in obj:
        return False
    return "result" in obj or "error" in obj


def _is_jsonrpc_request(obj: dict[str, Any]) -> bool:
    """Agent→client request: method + id (needs a reply)."""
    return bool(obj.get("method")) and "id" in obj and "result" not in obj and "error" not in obj


@dataclass
class LiveCliSession:
    """One long-lived vendor CLI process for a role."""

    role: str
    adapter: str
    uid: int
    home: Path
    tmp: Path
    xdg: Path
    spawn_cwd: Path
    proc: subprocess.Popen[str] | None = None
    claude_session_id: str | None = None
    acp_session_id: str | None = None
    # Client request ids start at 0; keep ours high to reduce confusion in logs.
    _rpc_id: int = 1000
    _lock: threading.Lock = field(default_factory=threading.Lock)
    last_rss_kb: int = 0
    _stderr_fh: IO[str] | None = field(default=None, repr=False)
    _stdout_q: queue.Queue[str | None] | None = field(default=None, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure_spawned(self, *, continue_session: bool = False) -> None:
        """Start the CLI if not running. continue_session → vendor -c / store re-entry."""
        with self._lock:
            if self.is_alive():
                return
            self._spawn_unlocked(continue_session=continue_session)

    def respawn(self, *, continue_session: bool = True) -> None:
        with self._lock:
            self._kill_unlocked()
            self._spawn_unlocked(continue_session=continue_session)

    def shutdown(self) -> None:
        with self._lock:
            self._kill_unlocked()

    def _kill_unlocked(self) -> None:
        proc = self.proc
        self.proc = None
        self._stdout_q = None
        self._reader_thread = None
        if proc is None:
            self._close_stderr()
            return
        try:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        except Exception as exc:  # noqa: BLE001
            log.warning("kill %s cli: %s", self.role, exc)
        self._close_stderr()

    def _close_stderr(self) -> None:
        fh = self._stderr_fh
        self._stderr_fh = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _ensure_role_dirs(self) -> None:
        """Create role trees; chown to role uid when we are root (NB1)."""
        dirs = [
            self.home,
            self.tmp,
            self.xdg,
            self.xdg / "cache",
            self.xdg / "config",
            self.xdg / "data",
            self.spawn_cwd,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
            if os.geteuid() == 0:
                try:
                    os.chown(d, self.uid, self.uid)
                except OSError as exc:
                    log.warning("chown %s uid=%s: %s", d, self.uid, exc)

    def _spawn_unlocked(self, *, continue_session: bool) -> None:
        self._ensure_role_dirs()
        self.acp_session_id = None
        self._rpc_id = 1000

        env = _role_env(self.role, self.home, self.tmp, self.xdg)
        if self.adapter in ("claude-code", "claude"):
            cmd = self._claude_cmd(continue_session)
        elif self.adapter in ("grok-cli", "grok"):
            cmd = self._grok_cmd()
        else:
            raise ValueError(f"no long-lived spawn for adapter {self.adapter!r}")
        # B5: secrets loaded in-child after setuid, not by root runner.
        cmd = _wrap_with_role_secrets(self.role, cmd)

        # B3: never PIPE stderr without a reader — fill → wedged CLI.
        stderr_path = self.tmp / f"cli-{self.role}.stderr.log"
        self._close_stderr()
        self._stderr_fh = open(stderr_path, "a", encoding="utf-8")  # noqa: SIM115

        popen_kwargs: dict[str, Any] = {
            "args": cmd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": self._stderr_fh,
            "text": True,
            "bufsize": 1,
            "cwd": str(self.spawn_cwd),
            "env": env,
        }
        # Prefer C-level drop (safe in threaded parent) over preexec_fn (NB2).
        if os.geteuid() == 0:
            popen_kwargs["user"] = self.uid
            popen_kwargs["group"] = self.uid
            popen_kwargs["extra_groups"] = []

        log.info(
            "spawning long-lived cli role=%s adapter=%s uid=%s cwd=%s continue=%s",
            self.role,
            self.adapter,
            self.uid,
            self.spawn_cwd,
            continue_session,
        )
        try:
            self.proc = subprocess.Popen(**popen_kwargs)
        except (TypeError, ValueError, PermissionError) as exc:
            # Older Python or non-Linux: fall back to preexec_fn setuid.
            log.warning("Popen(user=) failed (%s); falling back to preexec_fn", exc)
            popen_kwargs.pop("user", None)
            popen_kwargs.pop("group", None)
            popen_kwargs.pop("extra_groups", None)

            def preexec() -> None:
                if os.geteuid() == 0:
                    _drop_privs(self.uid)

            popen_kwargs["preexec_fn"] = preexec if os.geteuid() == 0 else None
            self.proc = subprocess.Popen(**popen_kwargs)

        self._start_stdout_reader()
        if self.adapter in ("grok-cli", "grok"):
            self._acp_initialize_unlocked()
        self._sample_rss()

    def _start_stdout_reader(self) -> None:
        """Reader thread avoids select+TextIOWrapper buffer trap (NB3)."""
        assert self.proc and self.proc.stdout
        q: queue.Queue[str | None] = queue.Queue()
        self._stdout_q = q
        stream: TextIO = self.proc.stdout

        def _run() -> None:
            try:
                while True:
                    line = stream.readline()
                    if line == "":
                        q.put(None)
                        return
                    q.put(line.rstrip("\n"))
            except Exception as exc:  # noqa: BLE001
                log.debug("stdout reader ended role=%s: %s", self.role, exc)
                q.put(None)

        t = threading.Thread(
            target=_run, name=f"cli-stdout-{self.role}", daemon=True
        )
        self._reader_thread = t
        t.start()

    def _claude_cmd(self, continue_session: bool) -> list[str]:
        binary = shutil.which("claude") or os.environ.get("CLAUDE_BIN") or "claude"
        cmd = [
            binary,
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if continue_session:
            cmd.append("-c")
        return cmd

    def _grok_cmd(self) -> list[str]:
        binary = shutil.which("grok") or os.environ.get("GROK_BIN") or "grok"
        return [binary, "agent", "stdio"]

    def _acp_write(self, obj: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _acp_initialize_unlocked(self) -> None:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self._rpc_id += 1
        # B1: advertise only what we implement. Grok does its own file I/O as
        # the role UID; we only answer session/request_permission.
        init = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "agentd-runner", "version": "1.1.1"},
            },
        }
        self._acp_write(init)
        resp = self._read_jsonrpc_result(self._rpc_id, timeout_s=30)
        if resp is None:
            raise RuntimeError("grok ACP initialize timed out")
        if resp.get("error"):
            raise RuntimeError(f"grok ACP initialize error: {resp['error']!r}")
        self._rpc_id += 1
        new = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "session/new",
            "params": {"cwd": str(self.spawn_cwd), "mcpServers": []},
        }
        self._acp_write(new)
        resp2 = self._read_jsonrpc_result(self._rpc_id, timeout_s=30)
        if not resp2 or not (resp2.get("result") or {}).get("sessionId"):
            raise RuntimeError(f"grok ACP session/new failed: {resp2!r}")
        self.acp_session_id = str(resp2["result"]["sessionId"])
        log.info("grok ACP session %s role=%s", self.acp_session_id, self.role)

    def _handle_acp_server_request(self, obj: dict[str, Any]) -> None:
        """Answer agent→client requests so tool turns do not wedge (B1/B6)."""
        method = str(obj.get("method") or "")
        rid = obj.get("id")
        if method == "session/request_permission":
            params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
            option_id = _pick_permission_option(params or {})
            if not option_id:
                self._acp_write(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {
                            "code": -32602,
                            "message": "no permission options offered",
                        },
                    }
                )
                log.warning("acp permission with empty options role=%s", self.role)
                return
            self._acp_write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option_id,
                        }
                    },
                }
            )
            log.debug(
                "acp allow permission id=%s option=%s role=%s",
                rid,
                option_id,
                self.role,
            )
            return
        # Capabilities are false; refuse any accidental fs/terminal calls.
        if method.startswith("fs/") or method.startswith("terminal/"):
            self._acp_write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": -32601,
                        "message": f"client capability not offered: {method}",
                    },
                }
            )
            log.warning("acp unexpected client request method=%s role=%s", method, self.role)
            return
        log.warning("acp unhandled server request method=%s role=%s", method, self.role)
        if rid is not None:
            self._acp_write(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )

    def turn(
        self,
        prompt: str,
        *,
        deadline_s: int = 900,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                try:
                    self._spawn_unlocked(continue_session=True)
                except Exception as exc:  # noqa: BLE001
                    return {
                        "status": "failed",
                        "summary": f"cli respawn failed: {exc}",
                        "public_actions": [],
                        "artifacts": [],
                    }
            assert self.proc is not None
            try:
                if self.adapter in ("claude-code", "claude"):
                    result = self._claude_turn_unlocked(prompt, deadline_s, progress)
                else:
                    result = self._grok_turn_unlocked(prompt, deadline_s, progress)
            except TimeoutError:
                log.warning("turn deadline exceeded role=%s; killing cli", self.role)
                self._kill_unlocked()
                return {
                    "status": "failed",
                    "summary": f"turn deadline exceeded after {deadline_s}s; cli killed",
                    "public_actions": [],
                    "artifacts": [],
                }
            except Exception as exc:  # noqa: BLE001
                log.exception("live turn failed role=%s", self.role)
                return {
                    "status": "failed",
                    "summary": f"live turn error: {exc}",
                    "public_actions": [],
                    "artifacts": [],
                }
            self._sample_rss()
            result["cli_rss_kb"] = self.last_rss_kb
            result["live_session"] = True
            return result

    def _emit_progress(
        self,
        progress: Callable[[dict[str, Any]], None] | None,
        chunk: str,
    ) -> None:
        if not progress or not chunk:
            return
        try:
            progress({"role": self.role, "chunk": chunk[:2000]})
        except Exception as exc:  # noqa: BLE001
            log.debug("progress callback failed: %s", exc)

    def _stderr_tail(self, n: int = 500) -> str:
        path = self.tmp / f"cli-{self.role}.stderr.log"
        try:
            data = path.read_text(encoding="utf-8", errors="replace")
            return data[-n:]
        except OSError:
            return ""

    def _claude_turn_unlocked(
        self,
        prompt: str,
        deadline_s: int,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert self.proc and self.proc.stdin
        msg: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        if self.claude_session_id:
            msg["session_id"] = self.claude_session_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

        texts: list[str] = []
        public_actions: list[dict[str, Any]] = []
        result_obj: dict[str, Any] | None = None
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"claude exited early: {self._stderr_tail()}")
            line = self._readline_timeout(max(0.1, deadline - time.time()))
            if line is None:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("session_id"):
                self.claude_session_id = str(obj["session_id"])
            public_actions.extend(from_claude_stream_obj(obj))
            if obj.get("type") == "assistant":
                content = (obj.get("message") or {}).get("content")
                piece = ""
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = str(c.get("text") or "")
                            texts.append(t)
                            piece += t
                elif isinstance(content, str):
                    texts.append(content)
                    piece = content
                self._emit_progress(progress, piece)
            if obj.get("type") == "result":
                result_obj = obj
                break
        else:
            raise TimeoutError("claude turn deadline")

        summary = (result_obj or {}).get("result") or "".join(texts)
        if isinstance(summary, str):
            summary = summary[:4000]
        else:
            summary = str(summary)[:4000]
        is_err = bool((result_obj or {}).get("is_error"))
        return {
            "status": "failed" if is_err else "done",
            "summary": summary,
            "public_actions": dedupe_actions(public_actions),
            "artifacts": [],
        }

    def _grok_turn_unlocked(
        self,
        prompt: str,
        deadline_s: int,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert self.proc and self.proc.stdin
        if not self.acp_session_id:
            self._acp_initialize_unlocked()
        self._rpc_id += 1
        mid = self._rpc_id
        req = {
            "jsonrpc": "2.0",
            "id": mid,
            "method": "session/prompt",
            "params": {
                "sessionId": self.acp_session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
        }
        self._acp_write(req)

        chunks: list[str] = []
        public_actions: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"grok exited early: {self._stderr_tail()}")
            line = self._readline_timeout(max(0.1, deadline - time.time()))
            if line is None:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            # Notifications and progress
            if obj.get("method") == "session/update":
                u = (obj.get("params") or {}).get("update") or {}
                if isinstance(u, dict):
                    public_actions.extend(from_grok_session_update(u))
                    if u.get("sessionUpdate") == "agent_message_chunk":
                        piece = str((u.get("content") or {}).get("text") or "")
                        chunks.append(piece)
                        self._emit_progress(progress, piece)
                continue
            # B1/B2: agent→client requests (own id space) — answer, do not treat as result
            if _is_jsonrpc_request(obj):
                self._handle_acp_server_request(obj)
                continue
            # B2: only frames with result/error and no method are responses
            if _is_jsonrpc_response(obj) and obj.get("id") == mid:
                result = obj
                break
        else:
            raise TimeoutError("grok turn deadline")

        actions = dedupe_actions(public_actions)
        if result and result.get("error"):
            return {
                "status": "failed",
                "summary": str(result["error"])[:800],
                "public_actions": actions,
                "artifacts": [],
            }
        # B7: stopReason drives status — cancelled must not look like done.
        stop = str(((result or {}).get("result") or {}).get("stopReason") or "")
        text = "".join(chunks)
        if not text and result:
            text = str((result.get("result") or {}).get("text") or "")[:4000]
        text = (text or "").strip()
        if stop and stop != "end_turn":
            status = "needs_human" if stop in ("refusal", "rejected") else "failed"
            summary = text or f"grok stopReason={stop}"
            log.info("grok stopReason=%s → %s role=%s", stop, status, self.role)
            return {
                "status": status,
                "summary": summary[:4000],
                "public_actions": actions,
                "artifacts": [],
                "stop_reason": stop,
            }
        if not text:
            return {
                "status": "failed",
                "summary": "empty agent result (no text after end_turn)",
                "public_actions": actions,
                "artifacts": [],
                "stop_reason": stop or "end_turn",
            }
        return {
            "status": "done",
            "summary": text[:4000],
            "public_actions": actions,
            "artifacts": [],
            "stop_reason": stop or "end_turn",
        }

    def _read_jsonrpc_result(
        self, mid: int, *, timeout_s: float
    ) -> dict[str, Any] | None:
        assert self.proc
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return None
            line = self._readline_timeout(max(0.1, deadline - time.time()))
            if line is None:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if _is_jsonrpc_request(obj):
                self._handle_acp_server_request(obj)
                continue
            if _is_jsonrpc_response(obj) and obj.get("id") == mid:
                return obj
        return None

    def _readline_timeout(self, timeout_s: float) -> str | None:
        if timeout_s <= 0:
            return None
        q = self._stdout_q
        if q is None:
            return None
        try:
            line = q.get(timeout=timeout_s)
        except queue.Empty:
            return None
        if line is None:
            # EOF — leave a sentinel for subsequent readers
            q.put(None)
            return None
        return line

    def _sample_rss(self) -> None:
        if not self.proc or self.proc.pid is None:
            return
        try:
            status = Path(f"/proc/{self.proc.pid}/status").read_text(encoding="utf-8")
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    self.last_rss_kb = int(line.split()[1])
                    return
        except (OSError, ValueError, IndexError):
            pass


def get_or_create_session(
    *,
    role: str,
    adapter: str,
    uid: int,
    home: Path,
    tmp: Path,
    xdg: Path,
    spawn_cwd: Path | None = None,
) -> LiveCliSession:
    with _REGISTRY_LOCK:
        existing = _SESSIONS.get(role)
        if existing is not None and existing.adapter == adapter and existing.is_alive():
            return existing
        if existing is not None:
            if existing.adapter != adapter:
                log.warning(
                    "adapter swap role=%s %s→%s; killing live CLI (conversation reset, §5.3)",
                    role,
                    existing.adapter,
                    adapter,
                )
            existing.shutdown()
        sess = LiveCliSession(
            role=role,
            adapter=adapter,
            uid=uid,
            home=home,
            tmp=tmp,
            xdg=xdg,
            spawn_cwd=spawn_cwd or project_root(),
        )
        _SESSIONS[role] = sess
        return sess


def shutdown_all() -> None:
    with _REGISTRY_LOCK:
        for s in list(_SESSIONS.values()):
            s.shutdown()
        _SESSIONS.clear()


def rss_snapshot() -> dict[str, int]:
    with _REGISTRY_LOCK:
        return {role: s.last_rss_kb for role, s in _SESSIONS.items()}
