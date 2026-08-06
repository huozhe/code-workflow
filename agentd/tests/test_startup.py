"""Startup gate: no secret → refuse to serve (B1 / R1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentd.__main__ import main
from agentd.config import Config
from agentd.db import Store
from agentd.server import create_app


def test_serve_exits_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Must not depend on ambient Keychain state (R1)."""
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTD_SECRET_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr("agentd.__main__.webhook_secret", lambda: None)
    with pytest.raises(SystemExit) as ei:
        main(["serve", "--skip-docker-wait", "--port", "19999"])
    assert ei.value.code == 1


def test_create_app_accepts_nonempty_secret(tmp_path: Path) -> None:
    """Complement of the startup gate: a real secret is not over-eagerly rejected."""
    store = Store(tmp_path / "state.db")
    cfg = Config(raw={}, root=tmp_path)
    app = create_app(cfg, store, b"unit-test-secret")
    assert app is not None
    store.close()
