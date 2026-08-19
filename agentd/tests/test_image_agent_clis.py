"""#23: session-runner image ships pinned claude + grok; adapters are runnable."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"

# Expected pins — must match Dockerfile ARG / LABEL (single source in image).
_EXPECTED_CLAUDE = "2.1.234"
_EXPECTED_GROK = "1.0.5"


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


def _image_labels(image: str) -> dict[str, str]:
    r = subprocess.run(
        ["docker", "inspect", image, "--format", "{{json .Config.Labels}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(r.stdout or "{}") or {}
    return {str(k): str(v) for k, v in data.items()}


def test_image_labels_match_pins(session_runner_image: str) -> None:
    labels = _image_labels(session_runner_image)
    assert labels.get("agentd.claude_code_version") == _EXPECTED_CLAUDE, labels
    assert labels.get("agentd.grok_cli_version") == _EXPECTED_GROK, labels


def test_clis_on_path_as_role_uids(session_runner_image: str) -> None:
    """Exit condition #1: claude and grok resolve as 1001 and 1002 (not only root)."""
    labels = _image_labels(session_runner_image)
    for uid in ("1001:1001", "1002:1002"):
        r = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "bash",
                "--user",
                uid,
                session_runner_image,
                "-c",
                "command -v claude && command -v grok && claude --version && grok --version",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert r.returncode == 0, f"uid={uid} stderr={r.stderr!r} out={r.stdout!r}"
        assert labels["agentd.claude_code_version"] in r.stdout, r.stdout
        assert labels["agentd.grok_cli_version"] in (r.stdout + r.stderr), r.stdout + r.stderr


def test_run_adapter_grok_uses_headless_not_tui(session_runner_image: str) -> None:
    """#24 B1: production adapter path must use -p/--single, not positional TUI.

    Invokes agentd_runner.adapters.run_adapter inside the image as role UID.
    Without auth, exit may be non-zero — but must not be ENXIO / TTY / CLI missing.
    """
    script = r"""
import json, os, sys
sys.path.insert(0, "/opt/agentd-runner")
from pathlib import Path
from agentd_runner.adapters import run_adapter

home = Path("/tmp/home-dev")
home.mkdir(parents=True, exist_ok=True)
cwd = Path("/tmp/work")
cwd.mkdir(parents=True, exist_ok=True)
env = {
    **os.environ,
    "HOME": str(home),
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}
result = run_adapter(
    adapter="grok-cli",
    role="developer",
    prompt="Reply with exactly: GROK_HEADLESS",
    cwd=cwd,
    env=env,
    deadline_s=45,
)
print(json.dumps(result))
summary = str(result.get("summary") or "")
# Broken positional TUI path:
assert "No such device or address" not in summary, summary
assert "os error 6" not in summary.lower(), summary
assert "CLI not found" not in summary, summary
assert "not found in PATH" not in summary, summary
# Headless path reaches auth or model (either is fine for this PR):
print("GROK_ADAPTER_OK")
"""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "1002:1002",
            "--entrypoint",
            "python3",
            session_runner_image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = r.stdout + r.stderr
    assert "GROK_ADAPTER_OK" in combined, combined[-2500:]
    assert "No such device or address" not in combined
    assert "CLI not found" not in combined


def test_run_adapter_claude_not_cli_missing(session_runner_image: str) -> None:
    """Vendor claude adapter reachable as role UID (auth may fail without token)."""
    script = r"""
import json, os, sys
sys.path.insert(0, "/opt/agentd-runner")
from pathlib import Path
from agentd_runner.adapters import run_adapter

home = Path("/tmp/home-arch")
home.mkdir(parents=True, exist_ok=True)
cwd = Path("/tmp/work")
cwd.mkdir(parents=True, exist_ok=True)
env = {
    **os.environ,
    "HOME": str(home),
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "CLAUDE_CODE_OAUTH_TOKEN": "invalid-for-path-test",
}
result = run_adapter(
    adapter="claude-code",
    role="architect",
    prompt="Reply with exactly: CLAUDE_HEADLESS",
    cwd=cwd,
    env=env,
    deadline_s=45,
)
print(json.dumps(result))
summary = str(result.get("summary") or "")
assert "CLI not found" not in summary, summary
assert "not found in PATH" not in summary, summary
print("CLAUDE_ADAPTER_OK")
"""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "1001:1001",
            "--entrypoint",
            "python3",
            session_runner_image,
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = r.stdout + r.stderr
    assert "CLAUDE_ADAPTER_OK" in combined, combined[-2500:]
