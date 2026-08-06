"""macOS Keychain secret helpers (§5.1)."""

from __future__ import annotations

import os
import subprocess


SERVICE = "agentd"


def get_password(account: str, service: str = SERVICE) -> str | None:
    """Read a generic password. Env fallback: AGENTD_SECRET_<ACCOUNT_UPPER>."""
    env_key = "AGENTD_SECRET_" + account.upper().replace("-", "_").replace(".", "_")
    if env_key in os.environ:
        return os.environ[env_key]
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def webhook_secret() -> bytes | None:
    raw = get_password("webhook-secret")
    if raw is None:
        return None
    return raw.encode("utf-8")
