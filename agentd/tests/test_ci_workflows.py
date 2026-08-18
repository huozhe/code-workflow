"""#131: CI exists; PR path is Docker-less; image path is filtered."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_PR = _REPO / ".github" / "workflows" / "pr.yml"
_IMAGE = _REPO / ".github" / "workflows" / "image.yml"
_USES = re.compile(r"^\s+- uses:\s+(\S+)\s*$", re.MULTILINE)
_PINNED = re.compile(r".+@((v\d+\.\d+\.\d+)|[0-9a-f]{40})$")


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
    """GHA runners have Docker. The stub must land on PATH via GITHUB_PATH."""
    text = _PR.read_text(encoding="utf-8")
    assert '>> "$GITHUB_PATH"' in text


def test_image_workflow_path_filter_covers_pins() -> None:
    data = _load(_IMAGE)
    trigger = _on(data)
    for event in ("pull_request", "push"):
        paths = trigger[event]["paths"]
        assert "agentd/docker/session-runner/**" in paths, paths
        assert "agentd/tests/test_image_agent_clis.py" in paths, paths
        assert "agentd/tests/conftest.py" in paths, paths
        assert "agentd/src/agentd/supervisor.py" in paths, paths


def test_workflow_uses_are_fully_pinned() -> None:
    """@v10 is not a real tag on setup-uv after v7. Pin vX.Y.Z or a SHA."""
    for path in (_PR, _IMAGE):
        text = path.read_text(encoding="utf-8")
        uses = _USES.findall(text)
        assert uses, path
        for ref in uses:
            assert _PINNED.match(ref), f"{path.name}: unpinned uses: {ref}"
