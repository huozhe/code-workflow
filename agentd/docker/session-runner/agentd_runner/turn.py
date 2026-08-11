"""Turn execution: long-lived CLI pipe or one-shot adapter (§7.3, §6.3, §14).

Default for real vendor adapters: multiplex over a per-role process held by
agentd-runner after setuid at spawn (#25). mock/script and AGENTD_CLI_MODE=oneshot
keep the disposable fork→setuid→run path (ADR-9 recovery).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from agentd_runner.adapters import resolve_adapter, run_adapter
from agentd_runner import cli_session
from agentd_runner.server import ROLE_UIDS, session_base

log = logging.getLogger("agentd_runner.turn")

ProgressCb = Callable[[dict[str, Any]], None]


def project_root() -> Path:
    """Project tree mount point (#20) — durable homes live here."""
    return Path(
        os.environ.get("AGENTD_PROJECT_ROOT")
        or os.environ.get("AGENTD_HOST_ROOT")
        or "/srv/agentd"
    )


def role_paths(role: str) -> dict[str, Path]:
    """Issue-scoped work dirs + durable project HOME for auth (Option D)."""
    base = session_base() / role
    # Prefer durable project home for CLI credentials (Grok auth.json etc.).
    durable = project_root() / "home" / role
    if durable.is_dir() or (project_root() / "home").exists():
        home = durable
    else:
        home = base / "home"
    return {
        "base": base,
        "home": home,
        "tmp": base / "tmp",
        "xdg": base / "xdg",
        "context": base / "context",
        "scratch": base / "scratch",
        "worktrees": base / "worktrees",
        "transcript": base / "transcript.jsonl",
        "summary": base / "context" / "summary.md",
    }


def append_transcript(role: str, record: dict[str, Any]) -> None:
    paths = role_paths(role)
    paths["transcript"].parent.mkdir(parents=True, exist_ok=True)
    with paths["transcript"].open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def load_rehydration(role: str) -> dict[str, Any]:
    """COLD→HOT continuity: summary + recent transcript tail (§6.3)."""
    paths = role_paths(role)
    summary = ""
    lines: list[str] = []
    if paths["summary"].is_file():
        summary = paths["summary"].read_text(encoding="utf-8", errors="replace")
    tail: list[dict[str, Any]] = []
    if paths["transcript"].is_file():
        lines = paths["transcript"].read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-50:]:
            try:
                tail.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {
        "summary": summary,
        "transcript_tail": tail,
        "transcript_lines": len(lines),
    }


# Role × session-state obligations (§8.2). Keep short — prepended every turn (#45).
# Active duties + idle "wait" rows so a mis-routed turn is never silent (PR #46 B1).
_OBLIGATIONS: dict[tuple[str, str], str] = {
    ("architect", "PLANNING"): (
        "Write the RFC into the worktree and open the Design PR from the "
        "architect branch. Do not implement."
    ),
    ("architect", "DESIGN_REWORK"): (
        "Revise the RFC from review feedback and push. Do not implement."
    ),
    ("architect", "DESIGN_APPROVED"): (
        "Merge the Design PR (§8.3). Do not open a Feature PR yet."
    ),
    ("architect", "IMPLEMENTING"): (
        "The Developer is implementing. Wait for the Feature PR. "
        "Do not implement, and do not close the issue."
    ),
    ("developer", "PLANNING"): (
        "Architect is drafting the design. Wait. Do not implement."
    ),
    ("developer", "DESIGN_REVIEW"): (
        "Review the Design PR: request changes or approve. Do not implement."
    ),
    ("developer", "DESIGN_APPROVED"): (
        "Architect is merging the Design PR. Wait. Do not implement yet."
    ),
    ("developer", "IMPLEMENTING"): (
        "Implement against the approved design and open the Feature PR."
    ),
}


def role_obligation(role: str, state: str) -> str:
    """One-line obligation for (role, state); empty if none is defined."""
    return _OBLIGATIONS.get((str(role or "").lower(), str(state or "")), "")


def build_prompt(params: dict[str, Any], rehydrate: dict[str, Any] | None) -> str:
    event = params.get("event") or {}
    digest = params.get("digest") or event
    role = str(params.get("role") or "")
    # Gateway sends session_state (#45); missing → treat as unknown (still warn).
    state = str(params.get("session_state") or "").strip()
    parts = [
        f"Role: {role}",
        f"Turn: {params.get('turn_id')}",
    ]
    if state:
        parts.append(f"Session state: {state}")
        obl = role_obligation(role, state)
        if obl:
            parts.append(f"Your obligation in this state: {obl}")
    parts.append(
        "Invariant: no implementation code before DESIGN_APPROVED. "
        "Until then, design artifacts only (RFC / Design PR)."
    )
    parts.append(f"Event: {json.dumps(digest, indent=2)[:4000]}")
    # Cwd decision (a) #25: CLI lives at project root; worktree is named per turn.
    worktree = (params.get("context") or {}).get("worktree")
    if worktree:
        parts.append(
            f"Issue worktree: {worktree}\n"
            "Perform file/git work inside this path (project-root CLI session)."
        )
    if rehydrate and rehydrate.get("summary"):
        parts.append("--- Context summary ---\n" + rehydrate["summary"][:8000])
    elif rehydrate and rehydrate.get("transcript_tail"):
        parts.append(
            "--- Recent transcript ---\n"
            + json.dumps(rehydrate["transcript_tail"][-10:], indent=2)[:6000]
        )
    ctx = params.get("context") or {}
    if ctx.get("digest") and Path(str(ctx["digest"])).is_file():
        parts.append(
            "--- Event digest file ---\n"
            + Path(str(ctx["digest"])).read_text(encoding="utf-8", errors="replace")[:4000]
        )
    parts.append(
        "Hard rule (§10.3): Never close the GitHub issue and never use closing "
        "keywords (Fixes/Closes) on a PR that would close the *session* issue. "
        "Only the human owner closes session issues — that event is the teardown "
        "trigger. Agents must not call the issues close API."
    )
    parts.append(
        "Respond by performing the agent work for this event. "
        "When finished, summarize what you did."
    )
    return "\n\n".join(parts)


def _oneshot_result(params: dict[str, Any], paths: dict[str, Path], adapter: str, prompt: str) -> dict[str, Any]:
    """Disposable fork→setuid→run path (mock/script + explicit recovery)."""
    role = str(params.get("role") or "")
    uid = ROLE_UIDS[role]
    deadline_s = int(params.get("deadline_s") or 900)

    def _child_body() -> dict[str, Any]:
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(paths["home"]),
                "TMPDIR": str(paths["tmp"]),
                "XDG_CACHE_HOME": str(paths["xdg"] / "cache"),
                "XDG_CONFIG_HOME": str(paths["xdg"] / "config"),
                "XDG_DATA_HOME": str(paths["xdg"] / "data"),
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
        cwd = Path(str((params.get("context") or {}).get("worktree") or paths["base"]))
        if not cwd.is_dir():
            cwd = paths["base"]
        return run_adapter(
            adapter=adapter,
            role=role,
            prompt=prompt,
            cwd=cwd,
            env=env,
            deadline_s=deadline_s,
        )

    if os.geteuid() == 0:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                try:
                    os.setgroups([])
                except OSError as exc:
                    os.write(2, f"setgroups: {exc}\n".encode())
                os.setgid(uid)
                os.setuid(uid)
                if os.geteuid() == 0:
                    os.write(2, b"FATAL: still euid 0 after setuid\n")
                    os._exit(2)
                out = _child_body()
                payload = json.dumps(out).encode("utf-8")
                os.write(w, len(payload).to_bytes(4, "big") + payload)
                os._exit(0)
            except Exception as exc:  # noqa: BLE001
                try:
                    err = json.dumps(
                        {
                            "status": "failed",
                            "summary": f"turn child error: {exc}",
                            "public_actions": [],
                            "artifacts": [],
                        }
                    ).encode()
                    os.write(w, len(err).to_bytes(4, "big") + err)
                except OSError:
                    pass
                try:
                    os.write(2, f"turn child: {exc}\n".encode())
                except OSError:
                    pass
                os._exit(1)
            finally:
                try:
                    os.close(w)
                except OSError:
                    pass

        os.close(w)
        try:
            header = b""
            while len(header) < 4:
                chunk = os.read(r, 4 - len(header))
                if not chunk:
                    break
                header += chunk
            if len(header) == 4:
                n = int.from_bytes(header, "big")
                body = b""
                while len(body) < n:
                    chunk = os.read(r, n - len(body))
                    if not chunk:
                        break
                    body += chunk
                result = json.loads(body.decode("utf-8"))
            else:
                result = {
                    "status": "failed",
                    "summary": "empty result pipe",
                    "public_actions": [],
                    "artifacts": [],
                }
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "failed",
                "summary": f"pipe read failed: {exc}",
                "public_actions": [],
                "artifacts": [],
            }
        finally:
            try:
                os.close(r)
            except OSError:
                pass
        _, status = os.waitpid(pid, 0)
        if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 2:
            return {
                "status": "failed",
                "summary": "refusing to run adapter as root (setuid failed)",
                "public_actions": [],
                "artifacts": [],
            }
        return result

    log.warning("euid!=0; running adapter without setuid (test host only)")
    try:
        return _child_body()
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "summary": f"turn error: {exc}",
            "public_actions": [],
            "artifacts": [],
        }


def _live_result(
    params: dict[str, Any],
    paths: dict[str, Path],
    adapter: str,
    prompt: str,
    progress: ProgressCb | None,
) -> dict[str, Any]:
    """Multiplex one turn over the long-lived per-role CLI (§6.3 / #25)."""
    role = str(params.get("role") or "")
    uid = ROLE_UIDS[role]
    deadline_s = int(params.get("deadline_s") or 900)
    sess = cli_session.get_or_create_session(
        role=role,
        adapter=adapter,
        uid=uid,
        home=paths["home"],
        tmp=paths["tmp"],
        xdg=paths["xdg"],
        spawn_cwd=project_root(),
    )
    try:
        sess.ensure_spawned(continue_session=True)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "failed",
            "summary": f"cli spawn failed: {exc}",
            "public_actions": [],
            "artifacts": [],
        }
    return sess.turn(prompt, deadline_s=deadline_s, progress=progress)


def exec_turn_as_role(
    params: dict[str, Any],
    *,
    progress: ProgressCb | None = None,
) -> dict[str, Any]:
    """Execute one turn for a role.

    Privilege: live CLI — dropped once at spawn (preexec); oneshot — per fork.
    Caller (server) must hold the per-role lock so concurrent issues cannot
    interleave writes on the shared pipe (#20 / ADR-4).
    """
    role = str(params.get("role") or "")
    if role not in ROLE_UIDS:
        return {
            "status": "failed",
            "summary": f"unknown role {role}",
            "public_actions": [],
            "artifacts": [],
        }

    paths = role_paths(role)
    for p in (paths["home"], paths["tmp"], paths["xdg"], paths["context"], paths["scratch"]):
        p.mkdir(parents=True, exist_ok=True)

    rehydrate = load_rehydration(role)
    # Live path: vendor process holds conversational state — inject transcript
    # only on cold recovery (handled by -c / ACP store). Still attach summary
    # when present so our compact layer is never orphaned (§14.4 growth note).
    adapter = resolve_adapter(role, params.get("adapter"))
    oneshot = cli_session.use_oneshot(adapter)
    if oneshot:
        prompt = build_prompt(params, rehydrate)
    else:
        # Prefer vendor memory; still pass summary if we have compacted.
        light = None
        if rehydrate.get("summary"):
            light = {"summary": rehydrate["summary"], "transcript_tail": []}
        prompt = build_prompt(params, light)

    deadline_s = int(params.get("deadline_s") or 900)
    turn_id = str(params.get("turn_id") or f"t-{int(time.time())}")
    prompt_path = paths["context"] / f"prompt-{turn_id}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    if oneshot:
        result = _oneshot_result(params, paths, adapter, prompt)
    else:
        result = _live_result(params, paths, adapter, prompt, progress)

    record = {
        "ts": int(time.time()),
        "turn_id": turn_id,
        "role": role,
        "adapter": adapter,
        "event": params.get("event"),
        "status": result.get("status"),
        "summary": result.get("summary"),
        "public_actions": result.get("public_actions") or [],
        "live_session": bool(result.get("live_session")),
    }
    try:
        append_transcript(role, record)
    except OSError as exc:
        log.warning("transcript append failed: %s", exc)

    result["turn_id"] = turn_id
    result["role"] = role
    result["adapter"] = adapter
    if not oneshot:
        result.setdefault("cli_rss_kb", cli_session.rss_snapshot().get(role, 0))
    # §14.4: fail loudly on unbounded growth rather than silent truncate.
    n_lines = int(rehydrate.get("transcript_lines") or 0)
    if n_lines >= 5000:
        log.error(
            "transcript growth role=%s lines=%s — §14.4 compaction not yet "
            "implemented; consider project conversation reset (no silent truncate)",
            role,
            n_lines,
        )
        result["transcript_growth_warning"] = n_lines
    return result


def ensure_role_cli_spawned(role: str, *, continue_session: bool = False) -> dict[str, Any]:
    """Spawn (or re-enter) the long-lived CLI for a role — session.init/resume.

    Returns a small status dict; failures are soft so init can still succeed
    when the image lacks a binary (tests); the next turn will surface them.
    """
    if role not in ROLE_UIDS:
        return {"role": role, "spawned": False, "error": "unknown role"}
    adapter = resolve_adapter(role, None)
    if cli_session.use_oneshot(adapter):
        return {"role": role, "spawned": False, "mode": "oneshot"}
    paths = role_paths(role)
    for p in (paths["home"], paths["tmp"], paths["xdg"]):
        p.mkdir(parents=True, exist_ok=True)
    try:
        sess = cli_session.get_or_create_session(
            role=role,
            adapter=adapter,
            uid=ROLE_UIDS[role],
            home=paths["home"],
            tmp=paths["tmp"],
            xdg=paths["xdg"],
            spawn_cwd=project_root(),
        )
        sess.ensure_spawned(continue_session=continue_session)
        return {
            "role": role,
            "spawned": True,
            "adapter": adapter,
            "pid": sess.proc.pid if sess.proc else None,
            "rss_kb": sess.last_rss_kb,
            "continue_session": continue_session,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("cli spawn at session lifecycle failed role=%s: %s", role, exc)
        return {"role": role, "spawned": False, "error": str(exc)}
