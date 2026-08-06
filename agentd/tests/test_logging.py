"""Log routing: INFO → stdout, WARNING+ → stderr."""

from __future__ import annotations

import logging

import pytest

from agentd.__main__ import configure_logging


def test_configure_logging_routes_levels(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG")
    log = logging.getLogger("agentd.test_logging")
    log.info("info-line")
    log.warning("warn-line")
    captured = capsys.readouterr()
    assert "info-line" in captured.out
    assert "info-line" not in captured.err
    assert "warn-line" in captured.err
    assert "warn-line" not in captured.out
