from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest

from git_lookout.github.auth import AppAuth, InstallationToken, repo_access


def _now_iso(delta_seconds: int) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_app_jwt_is_signed_and_claims_are_correct(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    auth = AppAuth(app_id="12345", private_key=private_pem)

    token = auth.app_jwt(now=1_000_000)
    # now=1_000_000 is in 1970, so skip exp validation — we assert the claim directly.
    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], options={"verify_exp": False}
    )

    assert claims["iss"] == "12345"
    # iat is back-dated by the clock-skew margin; exp is ~9 min out.
    assert claims["iat"] == 1_000_000 - 60
    assert claims["exp"] == 1_000_000 + 9 * 60


def test_app_jwt_integer_app_id_is_stringified(rsa_keypair):
    private_pem, public_pem = rsa_keypair
    auth = AppAuth(app_id=999, private_key=private_pem)
    claims = jwt.decode(auth.app_jwt(), public_pem, algorithms=["RS256"])
    assert claims["iss"] == "999"


def _mock_token_client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    )


def test_installation_token_fetched_and_returned(rsa_keypair):
    private_pem, _ = rsa_keypair
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(
            201, json={"token": "ghs_abc", "expires_at": _now_iso(3600)}
        )

    auth = AppAuth("1", private_pem, client=_mock_token_client(handler))
    token = auth.installation_token(42)

    assert token == "ghs_abc"
    assert captured["url"].endswith("/app/installations/42/access_tokens")
    assert captured["auth"].startswith("Bearer ")


def test_installation_token_is_cached_until_near_expiry(rsa_keypair):
    private_pem, _ = rsa_keypair
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            201, json={"token": f"ghs_{calls['n']}", "expires_at": _now_iso(3600)}
        )

    auth = AppAuth("1", private_pem, client=_mock_token_client(handler))

    first = auth.installation_token(42)
    second = auth.installation_token(42)

    assert first == second == "ghs_1"
    assert calls["n"] == 1  # second call served from cache


def test_expiring_token_is_refreshed(rsa_keypair):
    private_pem, _ = rsa_keypair
    auth = AppAuth("1", private_pem, client=_mock_token_client(lambda r: None))

    # Seed a token that expires within the refresh margin.
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    auth._tokens[42] = InstallationToken(token="stale", expires_at=soon)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201, json={"token": "fresh", "expires_at": _now_iso(3600)}
        )

    auth._client = _mock_token_client(handler)
    assert auth.installation_token(42) == "fresh"


def test_separate_installations_have_separate_tokens(rsa_keypair):
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        # Echo the installation id from the path into the token.
        inst = str(request.url).rsplit("/installations/", 1)[1].split("/", 1)[0]
        return httpx.Response(
            201, json={"token": f"tok-{inst}", "expires_at": _now_iso(3600)}
        )

    auth = AppAuth("1", private_pem, client=_mock_token_client(handler))
    assert auth.installation_token(1) == "tok-1"
    assert auth.installation_token(2) == "tok-2"


def test_failed_token_request_raises(rsa_keypair):
    private_pem, _ = rsa_keypair

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    auth = AppAuth("1", private_pem, client=_mock_token_client(handler))
    with pytest.raises(httpx.HTTPStatusError):
        auth.installation_token(42)


# ---- repo_access ----------------------------------------------------------


def test_repo_access_true_on_200():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"full_name": "acme/widgets"})

    assert repo_access(
        "gho_caller", "acme", "widgets", client=_mock_token_client(handler)
    ) is True
    assert captured["url"].endswith("/repos/acme/widgets")
    assert captured["auth"] == "Bearer gho_caller"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_repo_access_false_on_auth_failure(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "no"})

    assert repo_access(
        "gho_caller", "acme", "widgets", client=_mock_token_client(handler)
    ) is False
