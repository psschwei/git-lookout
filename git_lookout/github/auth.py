from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import jwt

GITHUB_API = "https://api.github.com"

# GitHub rejects app JWTs whose `exp` is more than 10 minutes out, and clock skew
# between us and GitHub can shift `iat` either direction. We issue a 9-minute token
# and back-date `iat` by 60s, which is the configuration GitHub's own docs recommend.
_JWT_TTL_SECONDS = 9 * 60
_JWT_CLOCK_SKEW_SECONDS = 60

# Refresh an installation token this many seconds before it actually expires, so a
# request started just under the wire doesn't race the expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 60


@dataclass
class InstallationToken:
    """An installation access token and the moment it expires (UTC)."""

    token: str
    expires_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        margin = _TOKEN_REFRESH_MARGIN_SECONDS
        return (self.expires_at - now).total_seconds() > margin


class AppAuth:
    """
    Authenticates as a GitHub App.

    Two credentials are produced:

    1. A JWT signed with the app's private key (RS256), identifying the *app*. Used
       only to mint installation tokens. Generated fresh per call — it is cheap.
    2. An installation access token, identifying the app's installation on a given
       account. Used for all repo/PR API calls. Cached per installation id until it
       is within the refresh margin of expiring.
    """

    def __init__(
        self,
        app_id: str | int,
        private_key: str,
        *,
        client: httpx.Client | None = None,
    ):
        self.app_id = str(app_id)
        self.private_key = private_key
        self._client = client or httpx.Client(base_url=GITHUB_API, timeout=30.0)
        self._tokens: dict[int, InstallationToken] = {}

    def app_jwt(self, *, now: float | None = None) -> str:
        """Mint a short-lived JWT identifying the app itself."""
        issued = int(now if now is not None else time.time())
        payload = {
            "iat": issued - _JWT_CLOCK_SKEW_SECONDS,
            "exp": issued + _JWT_TTL_SECONDS,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def installation_token(self, installation_id: int) -> str:
        """
        Return a valid installation access token, fetching a new one if the cached
        token is missing or close to expiry.
        """
        now = datetime.now(timezone.utc)
        cached = self._tokens.get(installation_id)
        if cached is not None and cached.is_fresh(now):
            return cached.token

        fresh = self._fetch_installation_token(installation_id)
        self._tokens[installation_id] = fresh
        return fresh.token

    def _fetch_installation_token(self, installation_id: int) -> InstallationToken:
        resp = self._client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {self.app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        return InstallationToken(
            token=body["token"],
            expires_at=_parse_github_timestamp(body["expires_at"]),
        )

    def close(self) -> None:
        self._client.close()


def repo_access(
    token: str,
    owner: str,
    repo: str,
    *,
    client: httpx.Client | None = None,
) -> bool:
    """
    Check whether a *caller-supplied* GitHub token grants access to a repo.

    This is the API endpoint's authorization gate, and is independent of the
    GitHub App credentials in :class:`AppAuth`: the caller proves they can see
    ``owner/repo`` by presenting their own token, which we test against
    ``GET /repos/{owner}/{repo}``. GitHub returns 200 when the token can read the
    repo and 401/403/404 (404 hides private repos from unauthorized callers)
    otherwise — all of which mean "no access" here.

    A transport-level failure propagates; only authentication/authorization
    statuses are folded into the ``False`` result.
    """
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=GITHUB_API, timeout=30.0)
    try:
        resp = client.get(
            f"/repos/{owner}/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    finally:
        if own_client:
            client.close()
    return resp.status_code == 200


def _parse_github_timestamp(value: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp (e.g. '2026-06-16T01:02:03Z') as UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
