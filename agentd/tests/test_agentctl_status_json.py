"""#32: agentctl status --json prints the same fields as the default output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentctl.__main__ import main


def test_status_json_flag_matches_default_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))

    main(["status"])
    default_parsed = json.loads(capsys.readouterr().out)

    main(["status", "--json"])
    json_parsed = json.loads(capsys.readouterr().out)

    # disk_free_gb is sampled live and may drift by a few bytes between the
    # two calls; compare everything else for exact equality.
    default_parsed.pop("disk_free_gb")
    json_parsed.pop("disk_free_gb")
    assert default_parsed == json_parsed
    assert "queue_depth" in json_parsed
    assert "hot_sessions" in json_parsed
