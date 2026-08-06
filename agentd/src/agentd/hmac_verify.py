"""GitHub webhook HMAC verification (constant-time)."""

from __future__ import annotations

import hashlib
import hmac


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Verify X-Hub-Signature-256: sha256=<hex>."""
    if not header or not header.startswith("sha256="):
        return False
    their = header.removeprefix("sha256=").strip()
    digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, their)
