"""FastAPI webhook gateway — M0 ingress (§4.2)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from agentd import __version__
from agentd.config import Config
from agentd.db import Store
from agentd.docker_wait import docker_socket_ready
from agentd.hmac_verify import verify_signature
from agentd.keychain import webhook_secret

log = logging.getLogger("agentd.server")

MAX_BODY = 2 * 1024 * 1024  # 2 MiB


class AppState:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self._nudge = threading.Event()


def create_app(config: Config, store: Store) -> FastAPI:
    state = AppState(config, store)
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

    @app.post("/webhooks/github")
    async def github_webhook(
        request: Request,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> Response:
        # P7: never 5xx for downstream conditions (breaker, dispatcher, docker).
        body = await request.body()
        if len(body) > MAX_BODY:
            return PlainTextResponse("payload too large", status_code=413)

        secret = webhook_secret()
        if secret is None:
            log.error("webhook secret missing; rejecting")
            return PlainTextResponse("webhook secret not configured", status_code=500)

        if not verify_signature(secret, body, x_hub_signature_256):
            return PlainTextResponse("invalid signature", status_code=401)

        if not x_github_delivery:
            return PlainTextResponse("missing delivery id", status_code=400)

        event = x_github_event or "unknown"
        action, repo, issue_num, sender = _parse_meta(body)

        # Persist even when breaker is open — dispatcher pauses, not ingress.
        inserted = store.insert_delivery(
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
            state._nudge.set()
        else:
            log.info("delivery duplicate id=%s ignored", x_github_delivery)

        return PlainTextResponse("ok", status_code=200)

    return app


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
