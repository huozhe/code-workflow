"""Typed ensure_session refusals (#35).

Capacity is transient (retry). Structural is permanent until a human fixes
the host layout/image — never match on exception message text.
"""

from __future__ import annotations


class CapacityRefusal(RuntimeError):
    """HOT project cap reached (§6.6). Leave deferred; retry when a slot frees."""


class StructuralRefusal(RuntimeError):
    """Allowlist / image / layout broken. Escalate once; do not retry."""
