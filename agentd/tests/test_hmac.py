import hashlib
import hmac

from agentd.hmac_verify import verify_signature


def test_valid_signature() -> None:
    secret = b"test-secret"
    body = b'{"action":"opened"}'
    dig = hmac.new(secret, body, hashlib.sha256).hexdigest()
    assert verify_signature(secret, body, f"sha256={dig}")


def test_invalid_signature() -> None:
    assert not verify_signature(b"secret", b"body", "sha256=deadbeef")
    assert not verify_signature(b"secret", b"body", None)
    assert not verify_signature(b"secret", b"body", "sha1=abc")
