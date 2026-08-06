"""FastAPI webhook gateway — M0 ingress (§4.2)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from agentd import __version__
from agentd.config import Config
from agentd.db import Store
from agentd.docker_wait import docker_socket_ready
from agentd.hmac_verify import verify_signature

log = logging.getLogger("agentd.server")

MAX_BODY = 2 * 1024 * 1024  # 2 MiB


class AppState:
    def __init__(self, config: Config, store: Store, webhook_secret: bytes) -> None:
        self.config = config
        self.store = store
        # Loaded once at process start — never re-read per request (B1).
        self.webhook_secret = webhook_secret
        self.nudge = threading.Event()


def create_app(config: Config, store: Store, webhook_secret: bytes) -> FastAPI:
    if not webhook_secret:
        raise ValueError("webhook_secret must be non-empty at startup")
    state = AppState(config, store, webhook_secret)
    app = FastAPI(title="agentd", version=__version__)
    app.state.agentd = state

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> Response:
        docker_ok = docker_socket_ready(config.docker_socket)
        try:
            store.status_snapshot()
            db_ok = True
        except Exception:
            db_ok = False
        disk = _disk_free_gb(config.root)
        disk_ok = disk is None or disk >= config.disk_floor_gb
        ok = docker_ok and db_ok and disk_ok
        body = {
            "ready": ok,
            "docker": docker_ok,
            "sqlite": db_ok,
            "disk_free_gb": disk,
            "disk_ok": disk_ok,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    # async def: native ASGI body stream for the 2 MiB cap (B3). SQLite commits
    # are offloaded via run_in_threadpool so they do not stall the loop (B2).
    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
        content_length: str | None = Header(default=None),
    ) -> Response:
        secret = state.webhook_secret
        if not secret:
            # create_app already rejects empty secrets; explicit for -O runs.
            raise RuntimeError("webhook secret missing after startup gate")

        # Cap memory before buffering (B3): honor Content-Length when present.
        if content_length is not None:
            try:
                cl = int(content_length)
            except ValueError:
                return PlainTextResponse("invalid content-length", status_code=400)
            if cl > MAX_BODY:
                return PlainTextResponse("payload too large", status_code=413)

        body = await _read_body_capped(request, MAX_BODY)
        if body is None:
            return PlainTextResponse("payload too large", status_code=413)

        if not verify_signature(secret, body, x_hub_signature_256):
            return PlainTextResponse("invalid signature", status_code=401)

        if not x_github_delivery:
            return PlainTextResponse("missing delivery id", status_code=400)

        event = x_github_event or "unknown"
        action, repo, issue_num, sender = _parse_meta(body)

        # Persist even when breaker is open — dispatcher pauses, not ingress.
        inserted = await run_in_threadpool(
            store.insert_delivery,
            delivery_id=x_github_delivery,
            event=event,
            action=action,
            repo=repo,
            issue_num=issue_num,
            sender=sender,
            payload=body,
            status="queued",
        )
        if inserted:
            log.info(
                "delivery queued id=%s event=%s action=%s repo=%s sender=%s",
                x_github_delivery,
                event,
                action,
                repo,
                sender,
            )
            state.nudge.set()
        else:
            log.info("delivery duplicate id=%s ignored", x_github_delivery)

        return PlainTextResponse("ok", status_code=200)

    return app


async def _read_body_capped(request: Request, max_bytes: int) -> bytes | None:
    """Stream the body with a hard byte cap. Returns None if over limit."""
    total = 0
    parts: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _parse_meta(body: bytes) -> tuple[str | None, str | None, int | None, str | None]:
    try:
        data: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return None, None, None, None
    action = data.get("action")
    repo_obj = data.get("repository") or {}
    repo = repo_obj.get("full_name")
    sender_obj = data.get("sender") or {}
    sender = sender_obj.get("login")
    issue_num = None
    if "issue" in data and isinstance(data["issue"], dict):
        issue_num = data["issue"].get("number")
    elif "pull_request" in data and isinstance(data["pull_request"], dict):
        issue_num = data["pull_request"].get("number")
    return (
        str(action) if action is not None else None,
        str(repo) if repo is not None else None,
        int(issue_num) if issue_num is not None else None,
        str(sender) if sender is not None else None,
    )


def _disk_free_gb(path) -> float | None:
    try:
        import shutil

        usage = shutil.disk_usage(path)
        return usage.free / (1024**3)
    except OSError:
        return None
