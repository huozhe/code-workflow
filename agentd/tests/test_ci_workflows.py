"""#131: CI exists; PR path is Docker-less; image path is filtered."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"
_PR = _REPO / ".github" / "workflows" / "pr.yml"
_IMAGE = _REPO / ".github" / "workflows" / "image.yml"
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


def _uses_refs(data: dict) -> list[str]:
    refs: list[str] = []
    for job in (data.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("uses"):
                refs.append(str(step["uses"]))
    return refs


def test_ruff_extends_defaults_not_replaces_them() -> None:
    """select discards F821/E9. extend-select keeps them (#134 / #135)."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    lint = data["tool"]["ruff"]["lint"]
    assert "extend-select" in lint
    assert "select" not in lint
    assert "RUF100" in lint["extend-select"]


def test_ruff_ignores_ann_arg_on_test_fakes() -> None:
    """A fake is unused args without types. Scoped ignore, not a blanket (#134)."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    ignores = data["tool"]["ruff"]["lint"]["per-file-ignores"]
    tests = ignores["tests/*"]
    for code in ("ANN001", "ANN002", "ANN003", "ARG001", "ARG005"):
        assert code in tests, code
    assert list(ignores) == ["tests/*"]


def test_workflow_uses_are_fully_pinned() -> None:
    """@v10 is not a real tag on setup-uv after v7. Pin vX.Y.Z or a SHA."""
    for path in (_PR, _IMAGE):
        refs = _uses_refs(_load(path))
        assert refs, path
        for ref in refs:
            assert _PINNED.match(ref), f"{path.name}: unpinned uses: {ref}"


def _job_run(data: dict, job: str, needle: str) -> str:
    steps = data["jobs"][job]["steps"]
    hits = [
        s["run"]
        for s in steps
        if isinstance(s, dict) and needle in str(s.get("run", ""))
    ]
    assert len(hits) == 1, hits
    return hits[0]


def test_types_job_is_blocking() -> None:
    """mypy src is 0. A regression must fail the job (#134)."""
    run = _job_run(_load(_PR), "types", "mypy")
    assert "uv run mypy src" in run
    assert "set +e" not in run
    assert "exit 0" not in run


def test_lint_job_stays_advisory() -> None:
    """ruff tests still 27. Do not fail the job on that yet (#134)."""
    run = _job_run(_load(_PR), "lint", "ruff")
    assert "exit 0" in run


# Mechanical leftovers on tests. Not the 26 that need a judgement
# (PLW1510/RUF012/RUF059/E402/C408/F841/SIM117).
_TESTS_AUTOFIX = "PIE807,F401,UP017,I001,F541"


def test_tests_autofixable_ruff_is_clean() -> None:
    """PIE807 F401 UP017 I001 F541 on tests stay at 0 (#134)."""
    r = subprocess.run(
        ["uv", "run", "ruff", "check", "tests", "--select", _TESTS_AUTOFIX],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stdout + r.stderr
