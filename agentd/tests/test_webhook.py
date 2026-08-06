"""Webhook contract tests — §4.2 + P7."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentd.config import Config
from agentd.db import Store, decompress_payload
from agentd.server import create_app


@pytest.fixture()
def secret() -> bytes:
    return b"unit-test-webhook-secret"


@pytest.fixture()
def client(tmp_path: Path, secret: bytes, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTD_ROOT", str(tmp_path))
    store = Store(tmp_path / "state.db")
    cfg = Config(raw={"host": {"owner": "huozhe"}, "gateway": {}}, root=tmp_path)
    app = create_app(cfg, store, secret)
    return TestClient(app), store, secret


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


def test_payload_stored_compressed(client) -> None:
    tc, store, secret = client
    body = b'{"action":"opened","repository":{"full_name":"o/r"},"sender":{"login":"u"}}'
    assert (
        tc.post(
            "/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": _sig(secret, body),
                "X-GitHub-Delivery": "zstd-1",
                "X-GitHub-Event": "issues",
            },
        ).status_code
        == 200
    )
    with store._lock:
        row = store._conn.execute(
            "SELECT payload FROM deliveries WHERE delivery_id = ?", ("zstd-1",)
        ).fetchone()
    assert row is not None
    assert row["payload"] != body  # compressed differs from raw
    assert decompress_payload(row["payload"]) == body


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


def test_content_length_over_cap_413(client) -> None:
    tc, store, secret = client
    body = b"x"
    r = tc.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Length": str(3 * 1024 * 1024),
            "X-Hub-Signature-256": _sig(secret, body),
            "X-GitHub-Delivery": "big-1",
            "X-GitHub-Event": "ping",
        },
    )
    # Starlette/TestClient may reject mismatched CL; either 413 or client error is fine
    # for oversize intent. Prefer 413 when our handler runs.
    assert r.status_code in (413, 400, 422)


def test_missing_secret_refuses_create_app(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.db")
    cfg = Config(raw={}, root=tmp_path)
    with pytest.raises(ValueError, match="webhook_secret"):
        create_app(cfg, store, b"")


def test_foreign_keys_pragma_on(tmp_path: Path) -> None:
    store = Store(tmp_path / "fk.db")
    assert store.foreign_keys_enabled() is True
    store.close()
