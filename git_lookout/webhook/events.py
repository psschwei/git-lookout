from __future__ import annotations

from dataclasses import dataclass

from git_lookout.github.client import PullRequest, _parse_pull_request

# Parsing and validation for GitHub `pull_request` webhook deliveries.
#
# A delivery's JSON body carries an `action` ("opened", "synchronize",
# "reopened", "closed", and several we ignore), the full `pull_request` object,
# the `repository` it belongs to, and the `installation` whose token we use to
# act on it. We project that into a single PullRequestEvent the handler can act
# on without ever touching raw payload dicts.
#
# Payloads come from the public internet (signature-verified, but still). Every
# field access is guarded: a malformed or unexpected payload yields None rather
# than raising, so the receiver answers a clean 400 instead of a 500.

# Actions that mean "this PR's head or open-state may have changed" — re-run
# detection. GitHub fires many other pull_request actions (labeled, assigned,
# edited, …) that don't affect conflicts; those parse to a valid event and the
# handler ignores them by action.
UPDATE_ACTIONS = frozenset({"opened", "synchronize", "reopened"})
CLOSE_ACTIONS = frozenset({"closed"})


@dataclass
class PullRequestEvent:
    """A validated `pull_request` webhook delivery, projected to what we need."""

    action: str
    repo_owner: str
    repo_name: str
    installation_id: int
    pr: PullRequest
    merged: bool  # only meaningful when action == "closed"


def parse_pull_request_event(payload: dict) -> PullRequestEvent | None:
    """
    Project a `pull_request` webhook payload into a :class:`PullRequestEvent`.

    Returns None if the payload is not a well-formed pull_request delivery —
    missing action, pull_request, repository owner/name, or installation id, or
    a pull_request object missing the fields :func:`_parse_pull_request` needs.
    The caller treats None as a 400.
    """
    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    pr_payload = payload.get("pull_request")
    repo = payload.get("repository") or {}
    installation = payload.get("installation") or {}

    if not isinstance(action, str) or not isinstance(pr_payload, dict):
        return None

    owner = (repo.get("owner") or {}).get("login")
    name = repo.get("name")
    installation_id = installation.get("id")

    if not owner or not name or not isinstance(installation_id, int):
        return None

    try:
        pr = _parse_pull_request(pr_payload)
    except (KeyError, TypeError):
        return None

    return PullRequestEvent(
        action=action,
        repo_owner=owner,
        repo_name=name,
        installation_id=installation_id,
        pr=pr,
        merged=bool(pr_payload.get("merged")),
    )
