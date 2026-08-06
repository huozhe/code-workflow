"""Log routing + rotation isolation (root logger must not leak across tests)."""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

import pytest
import uvicorn.config

from agentd.__main__ import configure_logging


@pytest.fixture
def restore_root_logger() -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for h in list(root.handlers):
        if h not in old_handlers:
            h.close()
    root.handlers.clear()
    root.handlers.extend(old_handlers)
    root.setLevel(old_level)


def test_configure_logging_routes_levels(
    restore_root_logger: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", log_dir=tmp_path)
    log = logging.getLogger("agentd.test_logging")
    log.info("info-line")
    log.warning("warn-line")
    captured = capsys.readouterr()
    assert "info-line" in captured.out
    assert "info-line" not in captured.err
    assert "warn-line" in captured.err
    assert "warn-line" not in captured.out
    body = (tmp_path / "agentd.log").read_text(encoding="utf-8")
    assert "info-line" in body
    assert "warn-line" in body


def test_configure_logging_survives_uvicorn_dictconfig(
    restore_root_logger: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """uvicorn.run() applies LOGGING_CONFIG; must not steal agentd root handlers."""
    configure_logging("INFO", log_dir=tmp_path)
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    log = logging.getLogger("agentd.test_logging")
    log.info("after-uvicorn")
    log.warning("warn-after")
    captured = capsys.readouterr()
    assert "after-uvicorn" in captured.out
    assert "after-uvicorn" not in captured.err
    assert "warn-after" in captured.err
    assert "warn-after" not in captured.out
