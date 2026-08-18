"""#121: tests must not resolve Config.root to the live ~/.agentd tree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentd.config import Config


def test_resolved_config_root_is_not_live_agentd() -> None:
    """Fixture must point Config() at a throwaway dir, not ~/.agentd."""
    live = (Path.home() / ".agentd").resolve()
    assert Config().root.resolve() != live


def test_bare_config_raises_under_pytest_without_agentd_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENTD_ROOT", raising=False)
    assert os.environ.get("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="AGENTD_ROOT"):
        Config()


def test_explicit_root_is_allowed_under_pytest(tmp_path: Path) -> None:
    cfg = Config(raw={}, root=tmp_path)
    assert cfg.root == tmp_path


def test_guard_does_not_fire_outside_pytest() -> None:
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("AGENTD_ROOT", None)
    src = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(src) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentd.config import Config; Config(); print('ok')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
