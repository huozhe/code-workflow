"""#21 R1: inspect Env must not carry subscription/API credentials."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agentd.supervisor import assert_no_secrets_in_inspect_env


def _fake_docker(env_list: list[str]):
    class R:
        stdout = json.dumps(env_list)
        returncode = 0

    def _docker(*args, check=True):
        return R()

    return _docker


def test_allowlist_env_ok() -> None:
    env = [
        "PATH=/usr/bin",
        "AGENTD_RPC_HOST=0.0.0.0",
        "AGENTD_RPC_PORT=7000",
        "AGENTD_HOST_ROOT=/srv/agentd",
        "AGENTD_PROJECT_ROOT=/srv/agentd",
        "AGENTD_SESSION_DIR=/srv/agentd/sessions/1",
        "HOSTNAME=abc",
    ]
    with patch("agentd.supervisor._docker", _fake_docker(env)):
        assert_no_secrets_in_inspect_env("cid")


def test_claude_oauth_in_env_forbidden() -> None:
    env = [
        "PATH=/usr/bin",
        "AGENTD_RPC_HOST=0.0.0.0",
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-FAKE",
    ]
    with (
        patch("agentd.supervisor._docker", _fake_docker(env)),
        pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"),
    ):
        assert_no_secrets_in_inspect_env("cid")


def test_bearer_in_env_forbidden() -> None:
    env = ["PATH=/usr/bin", "AGENTD_RUNNER_BEARER=secret"]
    with (
        patch("agentd.supervisor._docker", _fake_docker(env)),
        pytest.raises(RuntimeError, match="AGENTD_RUNNER_BEARER"),
    ):
        assert_no_secrets_in_inspect_env("cid")
