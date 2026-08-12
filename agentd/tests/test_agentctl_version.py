"""#58 / ADR-13: agentctl version prints package metadata without loading config."""

from __future__ import annotations

import importlib.metadata

import pytest

from agentctl.__main__ import main


def _forbid_load_config(*_a, **_k):  # pragma: no cover - must not be called
    raise AssertionError("load_config must not run for version")


def test_version_prints_installed_agentd_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = importlib.metadata.version("agentd")
    monkeypatch.setattr("agentctl.__main__.load_config", _forbid_load_config)

    main(["version"])
    out = capsys.readouterr().out
    assert out == f"{expected}\n"
    # Bare string only — not JSON.
    assert not out.lstrip().startswith("{")


def test_version_uses_importlib_metadata_distribution_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def _fake_version(name: str) -> str:
        seen.append(name)
        return "9.9.9-test"

    monkeypatch.setattr("agentctl.__main__.pkg_version", _fake_version)
    monkeypatch.setattr("agentctl.__main__.load_config", _forbid_load_config)

    main(["version"])
    assert capsys.readouterr().out == "9.9.9-test\n"
    assert seen == ["agentd"]
