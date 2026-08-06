"""Log routing + rotation isolation (root logger must not leak across tests)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

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


def test_configure_logging_file_and_stderr(
    restore_root_logger: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """INFO → agentd.log only; WARNING+ → file + stderr. No stdout mirror."""
    configure_logging("DEBUG", log_dir=tmp_path)
    log = logging.getLogger("agentd.test_logging")
    log.info("info-line")
    log.warning("warn-line")
    captured = capsys.readouterr()
    assert "info-line" not in captured.out
    assert "info-line" not in captured.err
    assert "warn-line" not in captured.out
    assert "warn-line" in captured.err
    body = (tmp_path / "agentd.log").read_text(encoding="utf-8")
    assert "info-line" in body
    assert "warn-line" in body


def test_uvicorn_access_reaches_rotating_file(
    restore_root_logger: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With log_config=None, uvicorn.* propagate to root (capped access log)."""
    configure_logging("INFO", log_dir=tmp_path)
    access = logging.getLogger("uvicorn.access")
    old_handlers = list(access.handlers)
    old_propagate = access.propagate
    access.handlers.clear()
    access.propagate = True
    try:
        access.info('127.0.0.1:0 - "GET /.env HTTP/1.1" 404')
        body = (tmp_path / "agentd.log").read_text(encoding="utf-8")
        assert "GET /.env" in body
        captured = capsys.readouterr()
        assert "GET /.env" not in captured.out
    finally:
        access.handlers.clear()
        access.handlers.extend(old_handlers)
        access.propagate = old_propagate
