"""Vendor quota recognition — adapter-owned (ADR-9 / #86).

The gateway must never grep vendor copy. This module is the only place
that may look at Claude/Grok prose, and the match is vendor copy that
will break when they change the string.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("agentd_runner.quota")

# Vendor copy — #84 live sample: "You've hit your session limit · resets 9:30am (UTC)"
# Limit + reset together. Bare "quota" / "rate limit" fire on model text
# and on GitHub's own 5000/hr limit (#94 B1).
_LIMIT_PHRASES = ("session limit", "usage limit")
_RESET_PHRASE = "resets"
_GROK_STOP_QUOTA = frozenset(
    {"rate_limited", "quota", "quota_exceeded", "resource_exhausted"}
)

_RESET_CLOCK = re.compile(
    r"resets\s+(\d{1,2}):(\d{2})\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)
_RESET_IN = re.compile(
    r"resets\s+in\s+(\d+)\s*(h|hours?|m|min|minutes?)\b",
    re.IGNORECASE,
)

_REDACT_KEYS = frozenset({"prompt", "message", "content", "messages"})


def redact_envelope(obj: Any) -> Any:
    """Drop prompt-sized fields before logging a failed result envelope."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if str(k).lower() in _REDACT_KEYS:
                out[k] = "<redacted>"
            else:
                out[k] = redact_envelope(v)
        return out
    if isinstance(obj, list):
        return [redact_envelope(x) for x in obj[:20]]
    if isinstance(obj, str) and len(obj) > 800:
        return obj[:800] + "…"
    return obj


def parse_reset_epoch(text: str, *, now: float | None = None) -> int | None:
    """Best-effort reset instant from vendor prose. None if unparseable."""
    if not text:
        return None
    now = time.time() if now is None else float(now)
    m_in = _RESET_IN.search(text)
    if m_in:
        n = int(m_in.group(1))
        unit = m_in.group(2).lower()
        secs = n * 3600 if unit.startswith("h") else n * 60
        return int(now + secs)
    m = _RESET_CLOCK.search(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = (m.group(3) or "").lower()
    tzname = (m.group(4) or "UTC").strip()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        tz = ZoneInfo(tzname) if tzname.upper() != "UTC" else timezone.utc
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    local_now = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate = candidate + timedelta(days=1)
    return int(candidate.timestamp())


def _vendor_prose(text: str) -> bool:
    """True only for vendor refusal copy: a limit phrase and a reset phrase."""
    low = (text or "").lower()
    return any(p in low for p in _LIMIT_PHRASES) and _RESET_PHRASE in low


def classify_quota(
    *,
    text: str = "",
    stop_reason: str = "",
    error: Any = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Return a quota_exhausted result fragment, or None if this is not quota.

    Matching vendor prose is intentional and local to the adapter. The
    gateway must only see the typed status.

    ``stop_reason`` and ``error`` are vendor channels. ``text`` is treated
    as vendor copy only when it carries both a limit phrase and a reset
    phrase — a substring ``quota`` or ``rate limit`` is not enough.
    """
    err_text = ""
    if isinstance(error, dict):
        err_text = str(error.get("message") or error)
    elif error is not None:
        err_text = str(error)
    blob = " ".join(p for p in (text, err_text) if p)
    stop = (stop_reason or "").lower()
    if stop in _GROK_STOP_QUOTA:
        summary = (text or err_text or stop_reason or "quota exhausted")[:4000]
        return {
            "status": "quota_exhausted",
            "retry_after": parse_reset_epoch(blob, now=now),
            "summary": summary,
        }
    if error is not None and _vendor_prose(err_text):
        return {
            "status": "quota_exhausted",
            "retry_after": parse_reset_epoch(err_text, now=now),
            "summary": (err_text or "quota exhausted")[:4000],
        }
    if error is None and not stop and _vendor_prose(text):
        return {
            "status": "quota_exhausted",
            "retry_after": parse_reset_epoch(text, now=now),
            "summary": (text or "quota exhausted")[:4000],
        }
    return None
