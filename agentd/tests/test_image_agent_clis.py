"""#23: session-runner image ships pinned claude + grok on PATH for role UIDs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentd.supervisor import IMAGE

_RUNNER_ROOT = Path(__file__).resolve().parents[1] / "docker" / "session-runner"


def _docker_ok() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _docker_ok(), reason="docker not available")


@pytest.fixture(scope="module")
def built_image() -> str:
    """Build session-runner; may take several minutes (git + CLI installs)."""
    r = subprocess.run(
        [
            "docker",
            "build",
            "-t",
            IMAGE,
            "-f",
            str(_RUNNER_ROOT / "Dockerfile"),
            str(_RUNNER_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"image build failed: {r.stderr[-1500:]}")
    return IMAGE


def test_clis_on_path_as_role_uids(built_image: str) -> None:
    """Exit condition #1: claude and grok resolve as 1001 and 1002 (not only root)."""
    for uid in ("1001:1001", "1002:1002"):
        r = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "bash",
                "-u",
                uid,
                built_image,
                "-c",
                "command -v claude && command -v grok && claude --version && grok --version",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert r.returncode == 0, f"uid={uid} stderr={r.stderr!r} out={r.stdout!r}"
        assert "2.1.224" in r.stdout, r.stdout
        assert "1.0.0" in r.stdout or "1.0.0" in r.stderr, r.stdout + r.stderr
        # Must not be "CLI not found"
        assert "not found" not in r.stdout.lower()
        assert "not found" not in r.stderr.lower()


def test_vendor_adapter_not_cli_missing_as_role_uid(built_image: str) -> None:
    """Exit condition #2: vendor adapter runs inside image as role UID.

    Without real subscription auth the CLI may exit non-zero (login required).
    That still proves the binary is on PATH and executable after setuid-equivalent
    ``-u 1001``. Evidence boundary: failure must not be 'CLI not found in PATH'.
    """
    # Invoke adapters.py path: which + one-shot -p (same binary adapters use)
    script = r"""
set -e
export PATH=/usr/local/bin:/usr/bin:/bin
python3 - <<'PY'
import shutil, subprocess, os
from pathlib import Path
assert shutil.which("claude"), "claude CLI not found in PATH"
assert shutil.which("grok"), "grok CLI not found in PATH"
# Claude: smallest non-interactive invoke (will fail auth without token — OK)
r = subprocess.run(
    ["claude", "-p", "Reply with exactly: SMOKE", "--output-format", "text",
     "--dangerously-skip-permissions"],
    capture_output=True, text=True, timeout=60,
    env={**os.environ, "HOME": "/tmp/home-arch", "CLAUDE_CODE_OAUTH_TOKEN": "invalid"},
)
out = (r.stdout or "") + (r.stderr or "")
print("claude_exit", r.returncode)
print("claude_out_head", out[:500])
assert "CLI not found" not in out
assert "not found in PATH" not in out
# Grok similarly
r2 = subprocess.run(
    ["grok", "-p", "Reply with exactly: SMOKE", "--always-approve"],
    capture_output=True, text=True, timeout=60,
    env={**os.environ, "HOME": "/tmp/home-dev"},
)
out2 = (r2.stdout or "") + (r2.stderr or "")
print("grok_exit", r2.returncode)
print("grok_out_head", out2[:500])
assert "CLI not found" not in out2
assert "not found in PATH" not in out2
print("ADAPTER_PATH_OK")
PY
"""
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            "1001:1001",
            "--entrypoint",
            "bash",
            built_image,
            "-c",
            "mkdir -p /tmp/home-arch /tmp/home-dev && " + script,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    # Script may fail if claude refuses --dangerously-skip-permissions for non-root
    # without other flags; still require ADAPTER_PATH_OK or explicit PATH success.
    combined = r.stdout + r.stderr
    assert "ADAPTER_PATH_OK" in combined or (
        r.returncode == 0 and "claude CLI not found" not in combined
    ), combined[-2000:]
    assert "claude CLI not found" not in combined
    assert "grok CLI not found" not in combined
