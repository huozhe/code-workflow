"""Derive public_actions from vendor tool-call streams (#39 / §14.3).

A public action is something that changes GitHub state other agents (or the
gateway) can observe: comments, reviews, PRs, merges. Local file edits alone
are not public.
"""

from __future__ import annotations

import re
from typing import Any


# gh subcommands that mutate GitHub (observed live as Bash/execute tools).
# Note: `gh api` is *not* listed by path — it defaults to GET (#43). Writes
# require an explicit -X / --method with a write verb (see _http_write_method).
_GH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgh\s+pr\s+create\b", re.I), "pr_opened"),
    (re.compile(r"\bgh\s+pr\s+edit\b", re.I), "pr_edit"),
    (re.compile(r"\bgh\s+pr\s+merge\b", re.I), "merge"),
    (re.compile(r"\bgh\s+pr\s+close\b", re.I), "pr_close"),
    (re.compile(r"\bgh\s+pr\s+ready\b", re.I), "pr_ready"),
    (re.compile(r"\bgh\s+pr\s+review\b", re.I), "review"),
    (re.compile(r"\bgh\s+pr\s+comment\b", re.I), "comment"),
    (re.compile(r"\bgh\s+issue\s+comment\b", re.I), "comment"),
    (re.compile(r"\bgh\s+issue\s+create\b", re.I), "issue_create"),
    (re.compile(r"\bgh\s+issue\s+edit\b", re.I), "issue_edit"),
    (re.compile(r"\bgh\s+issue\s+close\b", re.I), "issue_close"),
]

# git push moves PR heads → pull_request.synchronize (PR #42 B2).
_GIT_PUSH = re.compile(r"\bgit\s+push\b", re.I)
_GH_API = re.compile(r"\bgh\s+api\b", re.I)

# Explicit HTTP method flags (order: method may appear before or after the path).
_HTTP_METHOD = re.compile(
    r"(?:(?:-X|--method|--request)\s+)(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b",
    re.I,
)

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _http_write_method(text: str) -> bool:
    """True only when the command explicitly uses a write HTTP method.

    `gh api` defaults to GET — a path containing issues/comments/pulls is not
    a write (#43). Explicit `-X GET` / `--method GET` is never a public action.
    """
    methods = [m.group(1).upper() for m in _HTTP_METHOD.finditer(text)]
    if not methods:
        return False
    # Last explicit method wins if the shell line is odd; any write is enough.
    return any(m in _WRITE_METHODS for m in methods)


def classify_shell_command(cmd: str) -> dict[str, Any] | None:
    """Return a public_action dict if *cmd* mutates GitHub; else None."""
    text = (cmd or "").strip()
    if not text:
        return None
    for pat, kind in _GH_PATTERNS:
        if pat.search(text):
            return {
                "kind": kind,
                "tool": "Bash",
                "command": text[:500],
            }
    # gh api: verb-only (POST/PATCH/PUT/DELETE), never path-only or GET (#43).
    if _GH_API.search(text) and _http_write_method(text):
        return {
            "kind": "api_write",
            "tool": "Bash",
            "command": text[:500],
        }
    # Push moves PR head → synchronize webhook (diagnostic record; counter
    # resets only on *observed* progress — PR #42 B1/B2).
    if _GIT_PUSH.search(text):
        return {
            "kind": "push",
            "tool": "Bash",
            "command": text[:500],
        }
    # curl/httpie against api.github.com — same write-method gate as gh api.
    if "api.github.com" in text.lower() and _http_write_method(text):
        return {
            "kind": "api_write",
            "tool": "Bash",
            "command": text[:500],
        }
    return None


def classify_tool_use(name: str, input_obj: Any) -> dict[str, Any] | None:
    """Map a vendor tool_use / tool_call to a public_action or None."""
    tool = str(name or "").strip()
    if not tool:
        return None
    inp = input_obj if isinstance(input_obj, dict) else {}

    # Claude Bash / Grok execute: command in input
    for key in ("command", "cmd", "script"):
        if key in inp and isinstance(inp[key], str):
            act = classify_shell_command(inp[key])
            if act:
                act["tool"] = tool
                return act

    # Nested args (some ACP shapes)
    args = inp.get("args") if isinstance(inp.get("args"), dict) else {}
    for key in ("command", "cmd"):
        if key in args and isinstance(args[key], str):
            act = classify_shell_command(args[key])
            if act:
                act["tool"] = tool
                return act

    # Dedicated GitHub tools by name (mcp / plugin)
    low = tool.lower()
    if any(
        x in low
        for x in (
            "create_pull_request",
            "create_pr",
            "pull_request_create",
            "createpullrequest",
        )
    ):
        return {"kind": "pr_opened", "tool": tool, "input": _clip_input(inp)}
    if any(
        x in low
        for x in (
            "create_issue_comment",
            "add_issue_comment",
            "pull_request_review_comment",
            "create_review",
            "submit_review",
        )
    ):
        kind = "review" if "review" in low else "comment"
        return {"kind": kind, "tool": tool, "input": _clip_input(inp)}
    if "merge" in low and ("pr" in low or "pull" in low):
        return {"kind": "merge", "tool": tool, "input": _clip_input(inp)}

    # Generic HTTP tool with write method against GitHub
    url = str(inp.get("url") or inp.get("path") or "")
    method = str(inp.get("method") or inp.get("http_method") or "").upper()
    if "api.github.com" in url.lower() and method in _WRITE_METHODS:
        return {
            "kind": "api_write",
            "tool": tool,
            "method": method,
            "url": url[:300],
        }
    return None


def _clip_input(inp: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in list(inp.items())[:12]:
        if isinstance(v, str):
            out[str(k)] = v[:200]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[str(k)] = v
        else:
            out[str(k)] = str(v)[:200]
    return out


def from_claude_message_content(content: Any) -> list[dict[str, Any]]:
    """Extract public_actions from Claude assistant message content blocks."""
    actions: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return actions
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        act = classify_tool_use(str(block.get("name") or ""), block.get("input"))
        if act:
            if block.get("id"):
                act["tool_use_id"] = str(block["id"])
            actions.append(act)
    return actions


def from_claude_stream_obj(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract from one stream-json line (assistant / content_block_start)."""
    actions: list[dict[str, Any]] = []
    if obj.get("type") == "assistant":
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        actions.extend(from_claude_message_content(msg.get("content")))
    # content_block_start with tool_use
    if obj.get("type") == "content_block_start":
        block = obj.get("content_block") if isinstance(obj.get("content_block"), dict) else {}
        if block.get("type") == "tool_use":
            act = classify_tool_use(str(block.get("name") or ""), block.get("input"))
            if act:
                if block.get("id"):
                    act["tool_use_id"] = str(block["id"])
                actions.append(act)
    return actions


def from_grok_session_update(update: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract from ACP session/update.params.update frames."""
    actions: list[dict[str, Any]] = []
    kind = str(update.get("sessionUpdate") or update.get("session_update") or "")
    if kind not in (
        "tool_call",
        "tool_call_update",
        "toolCall",
        "toolCallUpdate",
    ):
        return actions
    # Prefer completed / completed-like; still record if status missing (start frame)
    status = str(update.get("status") or "").lower()
    if status and status not in (
        "completed",
        "complete",
        "success",
        "done",
        "finished",
        "",
    ):
        # in_progress / pending — wait for update with final input if possible
        if status in ("in_progress", "pending", "running", "started"):
            # Still try: some CLIs only emit one frame with full input
            pass

    name = str(
        update.get("title")
        or update.get("toolName")
        or update.get("name")
        or update.get("kind")
        or "tool"
    )
    raw_in = (
        update.get("rawInput")
        or update.get("raw_input")
        or update.get("input")
        or update.get("arguments")
        or {}
    )
    act = classify_tool_use(name, raw_in)
    if act:
        if update.get("toolCallId") or update.get("tool_call_id"):
            act["tool_call_id"] = str(
                update.get("toolCallId") or update.get("tool_call_id")
            )
        actions.append(act)
    else:
        # Title often embeds the command for execute tools
        title = str(update.get("title") or "")
        if title:
            shell_act = classify_shell_command(title)
            if shell_act:
                shell_act["tool"] = name
                actions.append(shell_act)
    return actions


def dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable de-dupe (tool_use may appear on start + result frames)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for a in actions:
        key = (
            str(a.get("tool_use_id") or a.get("tool_call_id") or "")
            + "|"
            + str(a.get("kind") or "")
            + "|"
            + str(a.get("command") or a.get("tool") or "")[:120]
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out
