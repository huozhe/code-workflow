"""Turn execution: privilege drop + adapter + transcript (§7.3, §6.3, §14)."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# os used for fork/setuid

from agentd_runner.adapters import resolve_adapter, run_adapter
from agentd_runner.server import ROLE_UIDS, session_base

log = logging.getLogger("agentd_runner.turn")


def role_paths(role: str) -> dict[str, Path]:
    base = session_base() / role
    return {
        "base": base,
        "home": base / "home",
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
    return {"summary": summary, "transcript_tail": tail, "transcript_lines": len(lines) if paths["transcript"].is_file() else 0}


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
    """fork → setgid/setuid → adapter. Returns turn result dict."""
    role = str(params.get("role") or "")
    if role not in ROLE_UIDS:
        return {"status": "failed", "summary": f"unknown role {role}", "public_actions": [], "artifacts": []}

    uid = ROLE_UIDS[role]
    paths = role_paths(role)
    for p in (paths["home"], paths["tmp"], paths["xdg"], paths["context"], paths["scratch"]):
        p.mkdir(parents=True, exist_ok=True)

    rehydrate = load_rehydration(role)
    prompt = build_prompt(params, rehydrate)
    adapter = resolve_adapter(role, params.get("adapter"))
    deadline_s = int(params.get("deadline_s") or 900)
    turn_id = str(params.get("turn_id") or f"t-{int(time.time())}")

    # Write prompt for the child / debugging
    prompt_path = paths["context"] / f"prompt-{turn_id}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    result_path = paths["context"] / f"result-{turn_id}.json"

    # Child writes result_path as the role UID
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

    # Prefer fork+setuid in container (euid 0). On unprivileged test hosts, run inline.
    if os.geteuid() == 0:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                os.setgid(uid)
                os.setuid(uid)
                result = _child_body()
                result_path.write_text(json.dumps(result), encoding="utf-8")
                os.write(w, b"ok")
                os._exit(0)
            except Exception as exc:  # noqa: BLE001
                try:
                    result_path.write_text(
                        json.dumps(
                            {
                                "status": "failed",
                                "summary": f"turn child error: {exc}",
                                "public_actions": [],
                                "artifacts": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                try:
                    os.write(2, f"turn child: {exc}\n".encode())
                except OSError:
                    pass
                os._exit(1)
            finally:
                os.close(w)
        os.close(w)
        _, status = os.waitpid(pid, 0)
        try:
            os.close(r)
        except OSError:
            pass
    else:
        log.warning("euid!=0; running adapter without setuid (test host)")
        try:
            result = _child_body()
            result_path.write_text(json.dumps(result), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            result_path.write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "summary": f"turn error: {exc}",
                        "public_actions": [],
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
        status = 0

    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {
                "status": "failed",
                "summary": "invalid result JSON",
                "public_actions": [],
                "artifacts": [],
            }
    else:
        result = {
            "status": "failed",
            "summary": f"adapter produced no result (exit={status})",
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
