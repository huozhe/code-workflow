"""#131: CI exists; PR path is Docker-less; image path is filtered."""

from __future__ import annotations

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_PR = _REPO / ".github" / "workflows" / "pr.yml"
_IMAGE = _REPO / ".github" / "workflows" / "image.yml"


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _on(data: dict) -> dict:
    # PyYAML reads the key `on` as True.
    trigger = data.get("on", data.get(True))
    assert isinstance(trigger, dict), trigger
    return trigger


def test_pr_workflow_exists_and_covers_pr_and_main() -> None:
    data = _load(_PR)
    trigger = _on(data)
    assert "pull_request" in trigger
    push = trigger["push"]
    assert "main" in push["branches"]


def test_pr_workflow_hides_docker() -> None:
    """GHA runners have Docker. The PR path must make `docker info` fail."""
    text = _PR.read_text(encoding="utf-8")
    assert "bin-nodocker" in text
    assert "docker" in text


def test_image_workflow_path_filter_covers_pins() -> None:
    data = _load(_IMAGE)
    trigger = _on(data)
    for event in ("pull_request", "push"):
        paths = trigger[event]["paths"]
        assert "agentd/docker/session-runner/**" in paths, paths
        assert "agentd/tests/test_image_agent_clis.py" in paths, paths
