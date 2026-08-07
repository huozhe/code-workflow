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
import select
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

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
    token_file = Path(f"/run/agent/{role}/token")
    if token_file.is_file():
        env["GH_TOKEN"] = token_file.read_text(encoding="utf-8").strip()
        env["GITHUB_TOKEN"] = env["GH_TOKEN"]
    oauth_file = Path(f"/run/agent/{role}/claude_oauth_token")
    if oauth_file.is_file():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_file.read_text(encoding="utf-8").strip()
    return env


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
    # Claude stream-json session_id (if advertised on stream)
    claude_session_id: str | None = None
    # Grok ACP session id
    acp_session_id: str | None = None
    _rpc_id: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    last_rss_kb: int = 0

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
        if proc is None:
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

    def _spawn_unlocked(self, *, continue_session: bool) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.xdg / "cache").mkdir(parents=True, exist_ok=True)
        (self.xdg / "config").mkdir(parents=True, exist_ok=True)
        (self.xdg / "data").mkdir(parents=True, exist_ok=True)
        self.spawn_cwd.mkdir(parents=True, exist_ok=True)

        env = _role_env(self.role, self.home, self.tmp, self.xdg)
        if self.adapter in ("claude-code", "claude"):
            cmd = self._claude_cmd(continue_session)
        elif self.adapter in ("grok-cli", "grok"):
            cmd = self._grok_cmd()
        else:
            raise ValueError(f"no long-lived spawn for adapter {self.adapter!r}")

        def preexec() -> None:
            if os.geteuid() == 0:
                _drop_privs(self.uid)

        log.info(
            "spawning long-lived cli role=%s adapter=%s uid=%s cwd=%s continue=%s",
            self.role,
            self.adapter,
            self.uid,
            self.spawn_cwd,
            continue_session,
        )
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(self.spawn_cwd),
            env=env,
            preexec_fn=preexec if os.geteuid() == 0 else None,
        )
        if self.adapter in ("grok-cli", "grok"):
            self._acp_initialize_unlocked()
        self._sample_rss()

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
        # -c restores conversation from disk when process was killed (S5 recovery).
        if continue_session:
            cmd.append("-c")
        return cmd

    def _grok_cmd(self) -> list[str]:
        binary = shutil.which("grok") or os.environ.get("GROK_BIN") or "grok"
        return [binary, "agent", "stdio"]

    def _acp_initialize_unlocked(self) -> None:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self._rpc_id += 1
        init = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
                "clientInfo": {"name": "agentd-runner", "version": "1.1.0"},
            },
        }
        self.proc.stdin.write(json.dumps(init) + "\n")
        self.proc.stdin.flush()
        resp = self._read_jsonrpc_result(self._rpc_id, timeout_s=30)
        if resp is None:
            raise RuntimeError("grok ACP initialize timed out")
        self._rpc_id += 1
        new = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "session/new",
            "params": {"cwd": str(self.spawn_cwd), "mcpServers": []},
        }
        self.proc.stdin.write(json.dumps(new) + "\n")
        self.proc.stdin.flush()
        resp2 = self._read_jsonrpc_result(self._rpc_id, timeout_s=30)
        if not resp2 or not (resp2.get("result") or {}).get("sessionId"):
            raise RuntimeError(f"grok ACP session/new failed: {resp2!r}")
        self.acp_session_id = str(resp2["result"]["sessionId"])
        log.info("grok ACP session %s role=%s", self.acp_session_id, self.role)

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
                # Wedged turn: kill child so the role is usable again.
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

    def _claude_turn_unlocked(
        self,
        prompt: str,
        deadline_s: int,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert self.proc and self.proc.stdin and self.proc.stdout
        msg: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        if self.claude_session_id:
            msg["session_id"] = self.claude_session_id
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

        texts: list[str] = []
        result_obj: dict[str, Any] | None = None
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() if self.proc.stderr else "") or ""
                raise RuntimeError(f"claude exited early: {err[:500]}")
            line = self._readline_timeout(self.proc.stdout, max(0.1, deadline - time.time()))
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
            "public_actions": [],
            "artifacts": [],
        }

    def _grok_turn_unlocked(
        self,
        prompt: str,
        deadline_s: int,
        progress: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        assert self.proc and self.proc.stdin and self.proc.stdout
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
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

        chunks: list[str] = []
        result: dict[str, Any] | None = None
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                err = (self.proc.stderr.read() if self.proc.stderr else "") or ""
                raise RuntimeError(f"grok exited early: {err[:500]}")
            line = self._readline_timeout(self.proc.stdout, max(0.1, deadline - time.time()))
            if line is None:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("method") == "session/update":
                u = (obj.get("params") or {}).get("update") or {}
                if u.get("sessionUpdate") == "agent_message_chunk":
                    piece = str((u.get("content") or {}).get("text") or "")
                    chunks.append(piece)
                    self._emit_progress(progress, piece)
            elif obj.get("id") == mid:
                result = obj
                break
        else:
            raise TimeoutError("grok turn deadline")

        if result and result.get("error"):
            return {
                "status": "failed",
                "summary": str(result["error"])[:800],
                "public_actions": [],
                "artifacts": [],
            }
        # stopReason end_turn is the turn-complete signal from #19.
        stop = ((result or {}).get("result") or {}).get("stopReason")
        if stop and stop != "end_turn":
            log.info("grok stopReason=%s role=%s", stop, self.role)
        text = "".join(chunks)
        if not text and result:
            text = str((result.get("result") or {}).get("text") or "")[:4000]
        return {
            "status": "done",
            "summary": text[:4000],
            "public_actions": [],
            "artifacts": [],
        }

    def _read_jsonrpc_result(
        self, mid: int, *, timeout_s: float
    ) -> dict[str, Any] | None:
        assert self.proc and self.proc.stdout
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return None
            line = self._readline_timeout(self.proc.stdout, max(0.1, deadline - time.time()))
            if line is None:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id") == mid:
                return obj
        return None

    @staticmethod
    def _readline_timeout(stream: Any, timeout_s: float) -> str | None:
        if timeout_s <= 0:
            return None
        fd = stream.fileno()
        ready, _, _ = select.select([fd], [], [], timeout_s)
        if not ready:
            return None
        line = stream.readline()
        if not line:
            return None
        return line.rstrip("\n")

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
