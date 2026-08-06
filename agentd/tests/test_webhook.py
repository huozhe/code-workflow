"""Webhook contract tests — §4.2 + P7."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentd.config import Config
from agentd.db import Store
from agentd.server import create_app


@pytest.fixture()
def env_secret(monkeypatch: pytest.MonkeyPatch) -> bytes:
    secret = b"unit-test-webhook-secret"
    monkeypatch.setenv("AGENTD_SECRET_WEBHOOK_SECRET", secret.decode())
    return secret


@pytest.fixture()
def client(tmp_path: Path, env_secret: bytes, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = Store(tmp_path / "state.db")
    cfg = Config(raw={"host": {"owner": "huozhe"}, "gateway": {}}, root=tmp_path)
    app = create_app(cfg, store)
    return TestClient(app), store, env_secret


def _sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_accepts_signed_webhook_and_persists(client) -> None:
    tc, store, secret = client
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "huozhe/code-workflow"},
            "sender": {"login": "huozhe"},
            "issue": {"number": 6},
        }
    ).encode()
    r = tc.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(secret, body),
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "issues",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    assert store.queue_depth() == 1
    assert store.delivery_count() == 1


def test_duplicate_delivery_ignored(client) -> None:
    tc, store, secret = client
    body = b'{"action":"opened","repository":{"full_name":"o/r"},"sender":{"login":"u"}}'
    headers = {
        "X-Hub-Signature-256": _sig(secret, body),
        "X-GitHub-Delivery": "same-id",
        "X-GitHub-Event": "issues",
    }
    assert tc.post("/webhooks/github", content=body, headers=headers).status_code == 200
    assert tc.post("/webhooks/github", content=body, headers=headers).status_code == 200
    assert store.delivery_count() == 1


def test_bad_signature_401_no_persist(client) -> None:
    tc, store, secret = client
    body = b"{}"
    r = tc.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=00",
            "X-GitHub-Delivery": "bad-sig",
            "X-GitHub-Event": "ping",
        },
    )
    assert r.status_code == 401
    assert store.delivery_count() == 0


def test_p7_200_while_breaker_open(client) -> None:
    """P7: ingress never 5xx for downstream conditions — breaker open still 200."""
    tc, store, secret = client
    store.set_disk_paused(True, reason="disk < 15 GB")
    body = b'{"zen":"test"}'
    r = tc.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(secret, body),
            "X-GitHub-Delivery": "breaker-open-1",
            "X-GitHub-Event": "ping",
        },
    )
    assert r.status_code == 200
    assert store.queue_depth() == 1
    assert store.is_disk_paused() is True
