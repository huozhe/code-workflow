"""Disk free-space helper (shared by /readyz and Resource Governor)."""

from __future__ import annotations

import shutil
from pathlib import Path


def disk_free_gb(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024**3)
    except OSError:
        return None
