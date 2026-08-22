"""Long-lived per-role CLI sessions held by agentd-runner (§6.3 / #25).

Spawn once per role (after setuid in the child), multiplex turns over stdio.
One-shot ``-p`` adapters remain the recovery path (ADR-9 / §14.5).

Cwd decision (#25): spawn at project root ``/srv/agentd``; each turn's
digest/prompt names the issue worktree — cross-issue context is retained in
the vendor conversation without rebinding the process cwd.
"""

from __future__ import annotations

import errno
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
from agentd_runner.quota import classify_quota, redact_envelope

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
        "set -e\n"
        'ROLE="$1"; shift\n'
        'TOK="/run/agent/${ROLE}/token"\n'
        'if [ -r "$TOK" ]; then\n'
        '  export GH_TOKEN="$(cat "$TOK")"\n'
        '  export GITHUB_TOKEN="$GH_TOKEN"\n'
        "fi\n"
        'OAUTH="/run/agent/${ROLE}/claude_oauth_token"\n'
        'if [ -r "$OAUTH" ]; then\n'
        '  export CLAUDE_CODE_OAUTH_TOKEN="$(cat "$OAUTH")"\n'
        "fi\n"
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
    return (
        bool(obj.get("method"))
        and "id" in obj
        and "result" not in obj
        and "error" not in obj
    )


# #92: claude documents its accepted --effort levels and silently falls back to
# the default on anything else, so validate before spawn. grok 1.0.5 accepts any
# --reasoning-effort string at parse time (exit 0 on a bogus value), so there is
# no equivalent set to check against — that gap is recorded in the ADR.
_CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})

# ADR-29 (b′): post-spawn poll window. Tests patch this to 0.
_SPAWN_LIVENESS_S = 0.75


def _frame_fields(line: str) -> str:
    """Compact frame identity for stale-drain WARNING lines (ADR-29 (a))."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        raw = line if len(line) <= 200 else line[:200] + "…"
        return f"raw={raw!r}"
    if not isinstance(obj, dict):
        raw = line if len(line) <= 200 else line[:200] + "…"
        return f"raw={raw!r}"
    parts: list[str] = []
    for key in ("type", "subtype", "is_error", "session_id", "method", "id"):
        if key in obj:
            parts.append(f"{key}={obj[key]!r}")
    return " ".join(parts) if parts else "type=absent"


def _killpg_as_role(pgid: int, uid: int, sig: int) -> int:
    """Signal a process group from a forked helper that has become the role uid.

    Pid 1 is root, and §7.2's ``--cap-drop ALL`` removes ``CAP_KILL``; signal
    permission needs a matching uid, which uid 0 does not bypass. So the runner
    cannot signal its own CLIs directly and must borrow the role's identity to
    do it (ADR-35). Returns 0 on success, otherwise an errno.

    Inside the fork, nothing that is not async-signal-safe, and **no logging**:
    the fork happens in a threaded process and another thread may hold the
    logging lock. ``_run_as_role`` (``server.py``) avoids it for the same reason.
    """
    pid = os.fork()
    if pid == 0:
        try:
            os.setgid(uid)
            os.setuid(uid)
            os.killpg(pgid, sig)
            os._exit(0)
        except OSError as exc:
            os._exit(exc.errno or 1)
        except BaseException:
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def _is_our_child(pid: int) -> bool | None:
    """Does ``/proc`` say we are this pid's parent? ``None`` when unknowable.

    Tri-state on purpose. In the container ``/proc`` is always there and the
    runner is the parent by construction, so a ``False`` is a real answer worth
    refusing on. On a dev host without ``/proc`` there is no answer at all, and
    treating that as ``False`` would make the kill path refuse everywhere off
    Linux — divergence between host and container being the very thing that let
    this defect live since M2.
    """
    if not os.path.isdir("/proc"):
        return None
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("PPid:"):
                    return line.split()[1] == str(os.getpid())
    except OSError:
        return False
    return False


def _live_pgid_members(pgid: int) -> list[int]:
    """Non-zombie processes still in ``pgid``, from ``/proc``. ``[]`` off Linux.

    The direct child exiting is **not** the group being gone: a descendant that
    ignores ``SIGTERM`` survives it, and the leader's status says nothing about
    that. Enumerating is also what makes escalation safe — ``proc.wait()`` has
    just reaped the leader, so if it was the last member the pgid is free to be
    recycled, and a blind second ``killpg`` could signal a stranger. A live
    member found here is the proof the pgid still means what we think.
    """
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    want = str(pgid)
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        state = gid = ""
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("State:"):
                        state = line.split(None, 2)[1]
                    elif line.startswith("NSpgid:"):
                        gid = line.split()[1]
                    if state and gid:
                        break
        except OSError:
            continue
        if gid == want and state != "Z":
            out.append(pid)
    return out


def _kill_group_direct(pgid: int, sig: int) -> int:
    """killpg without the helper — correct only when we share the target's uid."""
    try:
        os.killpg(pgid, sig)
        return 0
    except OSError as exc:
        return exc.errno or 1


def _tracked_pids() -> set[int]:
    """Pids the runner owns a ``Popen`` for — the reaper must never take these.

    Deliberately without ``_REGISTRY_LOCK``: ``shutdown_all`` holds it and then
    takes a session's ``_lock``, so a caller holding ``_lock`` that reached for
    it would invert the order and deadlock. A snapshot is enough — a stale
    extra pid only means we decline to reap something its owner will reap.
    """
    return {s.proc.pid for s in list(_SESSIONS.values()) if s.proc is not None}


def reap_orphans(tracked: set[int]) -> list[int]:
    """Reap untracked zombies reparented to us. Enumerated, never peeked (ADR-35).

    ``waitid(..., WNOWAIT)`` does not consume, so a peek returns the *same*
    child forever: whenever a tracked CLI is a reapable-but-unpolled zombie —
    the state immediately after a kill — the orphans starve and the reaper is a
    no-op. A blanket ``waitpid(-1)`` is worse than useless: it consumes the
    sibling role's status, and CPython's ``Popen._try_wait`` then treats
    ``ECHILD`` as already-reaped and reports that CLI's exit code as **0**, so
    a failed turn reads as a successful one.

    Must not run between the SIGTERM and the SIGKILL: a process group stays
    addressable through its zombie members, and reaping them frees the pgid to
    be recycled onto something else.
    """
    reaped: list[int] = []
    me = str(os.getpid())
    try:
        entries = os.listdir("/proc")
    except OSError:
        return reaped  # not Linux; production always is
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in tracked:
            continue
        state = ppid = ""
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("State:"):
                        state = line.split(None, 2)[1]
                    elif line.startswith("PPid:"):
                        ppid = line.split()[1]
                    if state and ppid:
                        break
        except OSError:
            continue
        if state != "Z" or ppid != me:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            continue
        reaped.append(pid)
    if reaped:
        log.info("reaped orphaned cli descendants: %s", reaped)
    return reaped


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
    # ADR-9 v2 (#92). None ⇒ the CLI's own default; never a silent fallback if a
    # value is set and the vendor rejects it — the spawn fails instead.
    model: str | None = None
    reasoning_effort: str | None = None
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
    # ADR-29 §2.4: assigned every spawn, True iff this spawn's argv used --resume.
    _spawned_with_resume: bool = False

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
            reason = self._kill_unlocked()
            if reason is not None:
                # ADR-29 (b) never depended on the old process dying — the
                # fresh _stdout_q is what delivers stream freshness — but it
                # was described as if it did. Proceed, loudly (ADR-35).
                log.error("respawn %s: previous cli not killed: %s", self.role, reason)
            self._spawn_unlocked(continue_session=continue_session)

    def shutdown(self) -> str | None:
        """FR-4.3 is a claim about the container, so a failed kill is reported."""
        with self._lock:
            return self._kill_unlocked()

    def _kill_unlocked(self) -> str | None:
        """Kill the CLI's process group as the role. ``None`` on success.

        State is cleared **only once the process is actually dead** (ADR-35).
        Clearing first made the runner forget a process it had not killed:
        ``is_alive()`` went ``False`` and the next turn spawned a second CLI
        against the same durable ``home/<role>`` (1.25.0). On failure the
        bookkeeping is left intact so that cannot happen, and the reason is
        returned to the caller instead of being swallowed at WARNING.
        """
        proc = self.proc
        if proc is None:
            self._close_stderr()
            return None
        if proc.poll() is not None:
            # The CLI exited on its own. That is not the group being gone — a
            # tool subprocess it left behind is still running, and the leader's
            # status says nothing about it. Same defect as trusting proc.wait()
            # below, reached by a different door. The leader is already reaped,
            # so getpgid(its pid) would fail; the pgid == pid invariant enforced
            # in _kill_group_unlocked is what lets us name the group anyway.
            leftovers = _live_pgid_members(proc.pid)
            reason = None
            if leftovers:
                log.warning(
                    "%s cli exited on its own, leaving %d group member(s): %s",
                    self.role,
                    len(leftovers),
                    leftovers,
                )
                rc = (
                    _killpg_as_role(proc.pid, self.uid, signal.SIGKILL)
                    if os.geteuid() == 0
                    else _kill_group_direct(proc.pid, signal.SIGKILL)
                )
                time.sleep(0.1)
                still = _live_pgid_members(proc.pid)
                if still:
                    reason = (
                        f"cli exited leaving group {proc.pid} alive "
                        f"(errno {rc}): {still}"
                    )
            self._forget_dead_unlocked()
            if reason is None:
                reap_orphans(_tracked_pids())
            return reason
        reason = self._kill_group_unlocked(proc)
        if reason is not None:
            log.error("kill %s cli failed: %s", self.role, reason)
            return reason
        self._forget_dead_unlocked()
        # After the escalation completes, never inside it.
        reap_orphans(_tracked_pids())
        return None

    def _kill_group_unlocked(self, proc: subprocess.Popen[str]) -> str | None:
        """SIGTERM then SIGKILL to the CLI's group, signalled as the role uid."""
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            # Exited and was reaped between our poll() and here. Nothing to
            # kill — a benign race, and it must not log like a guard refusal.
            return None
        except OSError as exc:
            return f"getpgid({proc.pid}): {exc}"
        # start_new_session=True makes the CLI *lead* its own group, so
        # pgid == pid is the invariant acceptance (3) asserts — enforced here at
        # runtime, because the cost of it being false is signalling a group we
        # do not own, up to and including the runner's own.
        if pgid != proc.pid:
            return (
                f"refusing killpg({pgid}): cli pid {proc.pid} does not lead its "
                f"own group — spawn lost start_new_session=True"
            )
        if pgid <= 1 or pgid == os.getpgid(0):
            # In the container the runner is pid 1, so its own group *is* 1 and
            # the second test would catch it — but only there. Naming pgid <= 1
            # outright also holds off Linux, where getpgid(0) is something else
            # and a stray pid 1 resolves to the host's init group. No CLI ever
            # leads group 1: start_new_session makes it lead its own.
            return (
                f"refusing killpg({pgid}): that is the runner's or init's group "
                f"— killing it would signal the runner"
            )
        owned = _is_our_child(proc.pid)
        if owned is False:
            # A different bug from the two above, with a different fix: the pid
            # is real and leads a group, but it is not ours to signal.
            return (
                f"refusing killpg({pgid}): pid {proc.pid} is not our child "
                f"(/proc says its parent is not {os.getpid()})"
            )
        if owned is None:
            log.debug(
                "kill %s: no /proc, parent of pid %s unverifiable — "
                "guard inactive off Linux",
                self.role,
                proc.pid,
            )
        for sig, timeout in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 3.0)):
            # The helper exists because root cannot signal the role's uid
            # without CAP_KILL. When we are not root the CLI already shares our
            # uid and a direct killpg is both permitted and correct — the same
            # condition _popen_unlocked uses to decide the privilege drop. The
            # group semantics are identical on both paths; only the privilege
            # step differs, and that half is provable only inside the image.
            if os.geteuid() == 0:
                rc = _killpg_as_role(pgid, self.uid, sig)
            else:
                rc = _kill_group_direct(pgid, sig)
            if rc not in (0, errno.ESRCH):
                return (
                    f"killpg({pgid}, {sig}) as uid {self.uid} failed: "
                    f"errno {rc} ({errno.errorcode.get(rc, '?')})"
                )
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
            survivors = _live_pgid_members(pgid)
            if not survivors:
                return None
            log.warning(
                "kill %s: direct child gone but %d group member(s) survive %s: %s",
                self.role,
                len(survivors),
                sig.name,
                survivors,
            )
        survivors = _live_pgid_members(pgid)
        if survivors:
            return f"group {pgid} still has live members after SIGKILL: {survivors}"
        return None

    def _forget_dead_unlocked(self) -> None:
        self.proc = None
        self._stdout_q = None
        self._reader_thread = None
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

        log.info(
            "spawning long-lived cli role=%s adapter=%s uid=%s cwd=%s continue=%s",
            self.role,
            self.adapter,
            self.uid,
            self.spawn_cwd,
            continue_session,
        )
        used_resume = bool(
            continue_session
            and self.adapter in ("claude-code", "claude")
            and self.claude_session_id
        )
        self._popen_unlocked(cmd)
        self._spawned_with_resume = used_resume
        if not self._await_liveness(_SPAWN_LIVENESS_S):
            tail = self._stderr_tail()
            if used_resume:
                log.warning(
                    "claude died %.2gs after --resume %s; falling back to -c: %s",
                    _SPAWN_LIVENESS_S,
                    self.claude_session_id,
                    tail,
                )
                self.claude_session_id = None
                self._discard_dead_child_unlocked()
                cmd = _wrap_with_role_secrets(self.role, self._claude_cmd(True))
                self._popen_unlocked(cmd)
                self._spawned_with_resume = False
                if not self._await_liveness(_SPAWN_LIVENESS_S):
                    raise RuntimeError(
                        f"claude died after -c fallback: {self._stderr_tail()}"
                    )
            else:
                raise RuntimeError(f"cli died {_SPAWN_LIVENESS_S}s after spawn: {tail}")

        reap_orphans(_tracked_pids())  # before each spawn (ADR-35)
        self._start_stdout_reader()
        if self.adapter in ("grok-cli", "grok"):
            self._acp_initialize_unlocked()
        self._sample_rss()

    def _popen_unlocked(self, cmd: list[str]) -> None:
        """Popen + preexec_fn fallback only (ADR-29 (b′): :300–314)."""
        env = _role_env(self.role, self.home, self.tmp, self.xdg)
        popen_kwargs: dict[str, Any] = {
            "args": cmd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": self._stderr_fh,
            "text": True,
            "bufsize": 1,
            "cwd": str(self.spawn_cwd),
            "env": env,
            # ADR-35: the CLI must lead its own process group, or the group
            # kill below is killpg(1) — the runner itself. Lands with or before
            # the kill change, never after.
            "start_new_session": True,
        }
        # Prefer C-level drop (safe in threaded parent) over preexec_fn (NB2).
        if os.geteuid() == 0:
            popen_kwargs["user"] = self.uid
            popen_kwargs["group"] = self.uid
            popen_kwargs["extra_groups"] = []
        try:
            self.proc = subprocess.Popen(**popen_kwargs)
        except (TypeError, ValueError, PermissionError) as exc:
            log.warning("Popen(user=) failed (%s); falling back to preexec_fn", exc)
            popen_kwargs.pop("user", None)
            popen_kwargs.pop("group", None)
            popen_kwargs.pop("extra_groups", None)

            def preexec() -> None:
                if os.geteuid() == 0:
                    _drop_privs(self.uid)

            popen_kwargs["preexec_fn"] = preexec if os.geteuid() == 0 else None
            self.proc = subprocess.Popen(**popen_kwargs)

    def _await_liveness(self, timeout_s: float) -> bool:
        """True if the child is still alive after timeout_s. False on death."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return False
            if time.monotonic() >= deadline:
                return True
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _discard_dead_child_unlocked(self) -> None:
        """Close a dead child's pipes before overwriting self.proc (ADR-29 (b′))."""
        proc = self.proc
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _drain_stdout_unlocked(self, *, turn_id: str | None, reason: str) -> int:
        """ADR-29 (a). Anything queued before this turn's prompt is stale."""
        q = self._stdout_q
        if q is None:
            return 0
        n = 0
        while True:
            try:
                line = q.get_nowait()
            except queue.Empty:
                return n
            if line is None:
                q.put(None)  # EOF sentinel: re-put and stop
                return n
            n += 1
            log.warning(
                "stale frame discarded role=%s turn_id=%s reason=%s %s",
                self.role,
                turn_id or "unknown",
                reason,
                _frame_fields(line),
            )

    def _respawn_after_error_unlocked(self) -> None:
        """ADR-29 (b). is_error does not prove the vendor is done with the prompt."""
        log.warning(
            "respawning cli after is_error role=%s pid=%s",
            self.role,
            None if self.proc is None else self.proc.pid,
        )
        try:
            reason = self._kill_unlocked()
            if reason is not None:
                log.error(
                    "respawn after is_error role=%s: previous cli not killed: %s",
                    self.role,
                    reason,
                )
            self._spawn_unlocked(continue_session=True)
        except Exception as exc:  # noqa: BLE001
            log.error("respawn after is_error failed role=%s: %s", self.role, exc)

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

        t = threading.Thread(target=_run, name=f"cli-stdout-{self.role}", daemon=True)
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
            if self.claude_session_id:
                cmd += ["--resume", self.claude_session_id]
            else:
                cmd.append("-c")
        # #92: claude 2.1.234 takes --model <alias|full> and --effort <level>.
        if self.model:
            cmd += ["--model", self.model]
        if self.reasoning_effort:
            # claude WARNS and silently uses the default on an unknown effort
            # ("ignoring it and using the default effort"). That is the exact
            # failure #92 exists to prevent, so refuse before spawning instead.
            if self.reasoning_effort not in _CLAUDE_EFFORTS:
                raise ValueError(
                    f"unknown claude reasoning_effort {self.reasoning_effort!r}; "
                    f"valid: {', '.join(sorted(_CLAUDE_EFFORTS))}"
                )
            cmd += ["--effort", self.reasoning_effort]
        return cmd

    def _grok_cmd(self) -> list[str]:
        binary = shutil.which("grok") or os.environ.get("GROK_BIN") or "grok"
        # #92: -m / --reasoning-effort belong to `grok agent`, NOT to the `stdio`
        # subcommand — `grok agent stdio -m X` exits 2 with "unexpected argument".
        # Verified against grok 1.0.5 inside the image.
        cmd = [binary, "agent"]
        if self.model:
            cmd += ["-m", self.model]
        if self.reasoning_effort:
            cmd += ["--reasoning-effort", self.reasoning_effort]
        cmd.append("stdio")
        return cmd

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
            log.warning(
                "acp unexpected client request method=%s role=%s", method, self.role
            )
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
        turn_id: str | None = None,
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
                    result = self._claude_turn_unlocked(
                        prompt, deadline_s, progress, turn_id
                    )
                else:
                    result = self._grok_turn_unlocked(
                        prompt, deadline_s, progress, turn_id
                    )
            except TimeoutError:
                log.warning("turn deadline exceeded role=%s; killing cli", self.role)
                reason = self._kill_unlocked()
                if reason is None:
                    summary = (
                        f"turn deadline exceeded after {deadline_s}s; cli killed"
                    )
                else:
                    # The role is deliberately left un-reusable: is_alive() stays
                    # True, so ensure_spawned will not start a second CLI beside
                    # a live one against the same durable home/<role>. The
                    # gateway sees the failure instead of a silent leak (ADR-35).
                    summary = (
                        f"turn deadline exceeded after {deadline_s}s; "
                        f"CLI COULD NOT BE KILLED: {reason}"
                    )
                    log.error("deadline kill failed role=%s: %s", self.role, reason)
                return {
                    "status": "failed",
                    "summary": summary,
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
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        assert self.proc and self.proc.stdin
        msg: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": prompt},
        }
        if self.claude_session_id:
            msg["session_id"] = self.claude_session_id
        self._drain_stdout_unlocked(turn_id=turn_id, reason="before claude prompt")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

        texts: list[str] = []
        public_actions: list[dict[str, Any]] = []
        result_obj: dict[str, Any] | None = None
        deadline = time.time() + deadline_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                if self._spawned_with_resume:
                    log.warning(
                        "claude died mid-turn after --resume role=%s; "
                        "clearing session id",
                        self.role,
                    )
                    self.claude_session_id = None
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
        if is_err:
            # Envelope is vendor-owned; log it so the next quota hit pins
            # the shape instead of us guessing (#86). Prompt-sized keys go.
            log.warning(
                "claude failed envelope role=%s %s",
                self.role,
                redact_envelope(result_obj),
            )
            # ADR-29 (b): keyed on is_error, before either return path.
            self._respawn_after_error_unlocked()
            # ADR-33 (a): a refusal can land *after* real work — the session
            # limit is hit mid-turn, so `texts` is non-empty and the old
            # emptiness condition made the limit invisible. `is_error` plus
            # _LIMIT_PHRASES is the whole signal: #94 B1's counter-examples are
            # kept out by the phrase list, which excludes bare "quota" and
            # "rate limit", and a turn that merely *talks about* a limit ends
            # is_error: false and never reaches this branch at all.
            quota = classify_quota(text=str(summary))
            if quota is not None:
                return {
                    **quota,
                    "public_actions": dedupe_actions(public_actions),
                    "artifacts": [],
                }
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
        turn_id: str | None = None,
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
        self._drain_stdout_unlocked(turn_id=turn_id, reason="before grok prompt")
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
            log.warning(
                "grok failed envelope role=%s %s",
                self.role,
                redact_envelope(result),
            )
            err = result["error"]
            quota = classify_quota(error=err)
            if quota is not None:
                return {
                    **quota,
                    "public_actions": actions,
                    "artifacts": [],
                }
            return {
                "status": "failed",
                "summary": str(err)[:800],
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
            # stopReason is a vendor channel. Do not classify from
            # accumulated agent_message_chunk text (#94 B1).
            quota = classify_quota(stop_reason=stop)
            if quota is not None:
                return {
                    **quota,
                    "public_actions": actions,
                    "artifacts": [],
                    "stop_reason": stop,
                }
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
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> LiveCliSession:
    with _REGISTRY_LOCK:
        existing = _SESSIONS.get(role)
        # #92: model/effort are spawn-time flags, so a change needs a respawn.
        # Without this the cached CLI would keep the old model for the container's
        # lifetime and the config would silently not apply.
        same_model = (
            existing is not None
            and existing.model == model
            and existing.reasoning_effort == reasoning_effort
        )
        if (
            existing is not None
            and existing.adapter == adapter
            and same_model
            and existing.is_alive()
        ):
            return existing
        if existing is not None:
            if existing.adapter != adapter:
                log.warning(
                    "adapter swap role=%s %s→%s; killing live CLI (conversation reset, §5.3)",
                    role,
                    existing.adapter,
                    adapter,
                )
            elif not same_model:
                log.warning(
                    "model swap role=%s %s/%s→%s/%s; killing live CLI "
                    "(conversation reset, #92)",
                    role,
                    existing.model,
                    existing.reasoning_effort,
                    model,
                    reasoning_effort,
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
            model=model,
            reasoning_effort=reasoning_effort,
        )
        _SESSIONS[role] = sess
        return sess


def shutdown_all() -> list[str]:
    """Returns the roles whose CLI could not be killed (empty on success).

    FR-4.3's "kill held CLI children" is a claim about the container, so
    ``session.teardown`` fails the RPC rather than reporting success when this
    is non-empty (ADR-35).
    """
    failed: list[str] = []
    with _REGISTRY_LOCK:
        for s in list(_SESSIONS.values()):
            reason = s.shutdown()
            if reason is not None:
                failed.append(f"{s.role}: {reason}")
        _SESSIONS.clear()
    return failed


def rss_snapshot() -> dict[str, int]:
    with _REGISTRY_LOCK:
        return {role: s.last_rss_kb for role, s in _SESSIONS.items()}
