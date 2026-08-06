"""Startup gate: no secret → refuse to serve (B1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentd.__main__ import main


def test_serve_exits_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTD_SECRET_WEBHOOK_SECRET", raising=False)
    # Ensure Keychain path also fails: empty account name lookup still fails.
    with pytest.raises(SystemExit) as ei:
        main(["serve", "--skip-docker-wait", "--port", "19999"])
    assert ei.value.code == 1
