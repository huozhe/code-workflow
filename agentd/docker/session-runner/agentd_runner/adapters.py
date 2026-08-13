"""Vendor adapters — replaceable behind agentd-runner (ADR-9 / OQ-4)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def resolve_adapter(role: str, name: str | None = None) -> str:
    """Adapter name: env AGENTD_ADAPTER overrides; else role defaults."""
    if os.environ.get("AGENTD_ADAPTER"):
        return os.environ["AGENTD_ADAPTER"]
    if name:
        return name
    # config-style defaults
    return "claude-code" if role == "architect" else "grok-cli"


def run_adapter(
    *,
    adapter: str,
    role: str,
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    deadline_s: int = 900,
) -> dict[str, Any]:
    """Run adapter as the *current* process identity (caller already setuid).

    Returns a turn result dict: status, summary, public_actions, artifacts.
    """
    if adapter in ("script", "mock"):
        return _run_script_adapter(prompt, cwd, env)
    if adapter == "claude-code":
        return _run_claude(prompt, cwd, env, deadline_s)
    if adapter in ("grok-cli", "grok"):
        return _run_grok(prompt, cwd, env, deadline_s)
    return {
        "status": "failed",
        "summary": f"unknown adapter {adapter!r}",
        "public_actions": [],
        "artifacts": [],
    }


def _run_script_adapter(prompt: str, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    """Test/deterministic adapter: optional script at context/adapter_script.sh.

    Script receives prompt on stdin; must print a single JSON object to stdout
    (turn result). Exit non-zero ⇒ failed.
    """
    script = cwd / "context" / "adapter_script.sh"
    if not script.is_file():
        # Minimal deterministic design-loop helper for unit tests
        out_path = cwd / "context" / "rfc.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(f"# Design\n\nPrompt:\n{prompt[:2000]}\n", encoding="utf-8")
        return {
            "status": "done",
            "summary": "mock adapter wrote context/rfc.md",
            "public_actions": [],
            "artifacts": [{"kind": "scratch", "ref": str(out_path)}],
        }
    proc = subprocess.run(
        ["bash", str(script)],
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=120,
    )
    if proc.returncode != 0:
        return {
            "status": "failed",
            "summary": f"script adapter exit {proc.returncode}: {proc.stderr[:500]}",
            "public_actions": [],
            "artifacts": [],
        }
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {
            "status": "done",
            "summary": proc.stdout[:2000],
            "public_actions": [],
            "artifacts": [],
        }


def _run_claude(prompt: str, cwd: Path, env: dict[str, str], deadline_s: int) -> dict[str, Any]:
    from agentd_runner.public_actions import dedupe_actions, from_claude_stream_obj

    binary = shutil.which("claude") or env.get("CLAUDE_BIN") or "claude"
    # Headless: -p/--print with stream-json so tool_use is visible (#39).
    cmd = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            timeout=deadline_s,
        )
    except FileNotFoundError:
        return {
            "status": "failed",
            "summary": "claude CLI not found in PATH",
            "public_actions": [],
            "artifacts": [],
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "summary": f"claude timed out after {deadline_s}s",
            "public_actions": [],
            "artifacts": [],
        }
    actions: list[dict[str, Any]] = []
    texts: list[str] = []
    result_summary = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            texts.append(line)
            continue
        if not isinstance(obj, dict):
            continue
        actions.extend(from_claude_stream_obj(obj))
        if obj.get("type") == "assistant":
            content = (obj.get("message") or {}).get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        texts.append(str(c.get("text") or ""))
        if obj.get("type") == "result":
            r = obj.get("result")
            if isinstance(r, str):
                result_summary = r
    public_actions = dedupe_actions(actions)
    if proc.returncode != 0:
        from agentd_runner.quota import classify_quota

        quota = classify_quota(text=str(result_summary or proc.stderr or proc.stdout or ""))
        if quota is not None:
            return {**quota, "public_actions": public_actions, "artifacts": []}
        return {
            "status": "failed",
            "summary": f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[:800]}",
            "public_actions": public_actions,
            "artifacts": [],
        }
    summary = result_summary or "".join(texts) or (proc.stdout or "")
    return {
        "status": "done",
        "summary": summary[:4000],
        "public_actions": public_actions,
        "artifacts": [],
    }


def _run_grok(prompt: str, cwd: Path, env: dict[str, str], deadline_s: int) -> dict[str, Any]:
    from agentd_runner.public_actions import classify_shell_command, dedupe_actions

    binary = shutil.which("grok") or env.get("GROK_BIN") or "grok"
    # Headless single-turn: -p/--single (positional prompt opens the TUI and
    # fails with ENXIO when there is no controlling terminal — #24 B1).
    # Oneshot text output does not carry ACP tool frames; best-effort scan
    # stdout for gh write commands the agent echoes (#39). Live path uses ACP.
    cmd = [binary, "-p", prompt, "--always-approve", "--cwd", str(cwd)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
            timeout=deadline_s,
        )
    except FileNotFoundError:
        return {
            "status": "failed",
            "summary": "grok CLI not found in PATH",
            "public_actions": [],
            "artifacts": [],
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "summary": f"grok timed out after {deadline_s}s",
            "public_actions": [],
            "artifacts": [],
        }
    out = proc.stdout or ""
    actions: list[dict[str, Any]] = []
    for line in out.splitlines():
        act = classify_shell_command(line)
        if act:
            actions.append(act)
    # Also scan whole blob for multi-line commands
    for m in re.finditer(r"gh\s+\S+(?:\s+[^\n]+)?", out):
        act = classify_shell_command(m.group(0))
        if act:
            actions.append(act)
    public_actions = dedupe_actions(actions)
    if proc.returncode != 0:
        from agentd_runner.quota import classify_quota

        quota = classify_quota(text=proc.stderr or out)
        if quota is not None:
            return {**quota, "public_actions": public_actions, "artifacts": []}
        return {
            "status": "failed",
            "summary": f"grok exit {proc.returncode}: {(proc.stderr or out)[:800]}",
            "public_actions": public_actions,
            "artifacts": [],
        }
    return {
        "status": "done",
        "summary": out[:4000],
        "public_actions": public_actions,
        "artifacts": [],
    }
