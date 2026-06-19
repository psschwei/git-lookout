from __future__ import annotations

import hashlib
import hmac

from git_lookout.webhook.signature import verify_signature

SECRET = "it's-a-secret-to-everybody"
BODY = b'{"action":"opened"}'


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_passes():
    assert verify_signature(SECRET, BODY, _sign(SECRET, BODY)) is True


def test_wrong_secret_fails():
    assert verify_signature(SECRET, BODY, _sign("wrong-secret", BODY)) is False


def test_tampered_body_fails():
    header = _sign(SECRET, BODY)
    assert verify_signature(SECRET, BODY + b" ", header) is False


def test_missing_header_fails():
    assert verify_signature(SECRET, BODY, None) is False


def test_empty_header_fails():
    assert verify_signature(SECRET, BODY, "") is False


def test_header_without_sha256_prefix_fails():
    # A bare hexdigest with no "sha256=" prefix is rejected.
    digest = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert verify_signature(SECRET, BODY, digest) is False


def test_garbage_after_prefix_fails():
    assert verify_signature(SECRET, BODY, "sha256=not-a-real-digest") is False
