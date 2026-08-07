"""Turn execution: privilege drop + adapter + transcript (§7.3, §6.3, §14)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from agentd_runner.adapters import resolve_adapter, run_adapter
from agentd_runner.server import ROLE_UIDS, session_base

log = logging.getLogger("agentd_runner.turn")


def project_root() -> Path:
    """Project tree mount point (#20) — durable homes live here."""
    import os

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


def build_prompt(params: dict[str, Any], rehydrate: dict[str, Any] | None) -> str:
    event = params.get("event") or {}
    digest = params.get("digest") or event
    parts = [
        f"Role: {params.get('role')}",
        f"Turn: {params.get('turn_id')}",
        f"Event: {json.dumps(digest, indent=2)[:4000]}",
    ]
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
        "Respond by performing the agent work for this event. "
        "When finished, summarize what you did."
    )
    return "\n\n".join(parts)


def exec_turn_as_role(params: dict[str, Any]) -> dict[str, Any]:
    """setgroups([]) → setgid → setuid → adapter. Result returns on pipe (M3-6)."""
    role = str(params.get("role") or "")
    if role not in ROLE_UIDS:
        return {
            "status": "failed",
            "summary": f"unknown role {role}",
            "public_actions": [],
            "artifacts": [],
        }

    uid = ROLE_UIDS[role]
    paths = role_paths(role)
    for p in (paths["home"], paths["tmp"], paths["xdg"], paths["context"], paths["scratch"]):
        p.mkdir(parents=True, exist_ok=True)

    rehydrate = load_rehydration(role)
    prompt = build_prompt(params, rehydrate)
    adapter = resolve_adapter(role, params.get("adapter"))
    deadline_s = int(params.get("deadline_s") or 900)
    turn_id = str(params.get("turn_id") or f"t-{int(time.time())}")

    # Prompt on bind mount is fine (data, not control-flow). Result must not be.
    prompt_path = paths["context"] / f"prompt-{turn_id}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

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
        # Claude subscription long-lived token (#19) — pass through if host injected.
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
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

    result: dict[str, Any]
    status = 0

    if os.geteuid() == 0:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                # Drop supplementary groups first (M3-4); after setuid we cannot.
                try:
                    os.setgroups([])
                except OSError as exc:
                    # CAP_SETGID required; container has it. Log and continue.
                    os.write(2, f"setgroups: {exc}\n".encode())
                os.setgid(uid)
                os.setuid(uid)
                if os.geteuid() == 0:
                    # Must never run adapters as root in production container.
                    os.write(2, b"FATAL: still euid 0 after setuid\n")
                    os._exit(2)
                out = _child_body()
                payload = json.dumps(out).encode("utf-8")
                # length-prefixed result on the pipe (not the bind mount)
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
        # Read result from pipe
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
            result = {
                "status": "failed",
                "summary": "refusing to run adapter as root (setuid failed)",
                "public_actions": [],
                "artifacts": [],
            }
    else:
        log.warning("euid!=0; running adapter without setuid (test host only)")
        try:
            result = _child_body()
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "failed",
                "summary": f"turn error: {exc}",
                "public_actions": [],
                "artifacts": [],
            }

    record = {
        "ts": int(time.time()),
        "turn_id": turn_id,
        "role": role,
        "adapter": adapter,
        "event": params.get("event"),
        "status": result.get("status"),
        "summary": result.get("summary"),
    }
    try:
        append_transcript(role, record)
    except OSError as exc:
        log.warning("transcript append failed: %s", exc)

    result["turn_id"] = turn_id
    result["role"] = role
    result["adapter"] = adapter
    return result
