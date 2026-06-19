from __future__ import annotations

import hashlib
import hmac

# GitHub signs each webhook delivery with the app's configured secret and sends
# the result in the X-Hub-Signature-256 header as "sha256=<hexdigest>". Verifying
# it is how we know a payload genuinely came from GitHub and wasn't forged or
# tampered with in transit — the secret never leaves our config and GitHub.

_PREFIX = "sha256="


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """
    Return True iff ``header`` is GitHub's valid HMAC-SHA256 signature of ``body``.

    ``header`` is the raw ``X-Hub-Signature-256`` value, e.g. ``"sha256=abc123..."``.
    A missing header, a header without the ``sha256=`` prefix, or a digest that
    doesn't match yields False — never an exception, so the caller maps any
    falsey result to a single 401.

    The comparison uses :func:`hmac.compare_digest` to avoid leaking, via timing,
    how many leading bytes of a forged signature were correct.
    """
    if not header or not header.startswith(_PREFIX):
        return False

    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    provided = header[len(_PREFIX) :]
    return hmac.compare_digest(expected, provided)
