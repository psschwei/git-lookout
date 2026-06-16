from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

from git_lookout.github.auth import AppAuth
from git_lookout.github.client import GitHubClient
from git_lookout.storage import queries

# The reconciliation sweep is git-lookout's safety net against missed webhooks.
# It polls the GitHub API for the live set of open PRs and reconciles the
# database to match: add PRs we don't know about, refresh PRs whose head moved,
# and drop PRs that are no longer open. Only PRs targeting the repo's default
# branch are tracked — those are the ones whose conflicts we care about.
#
# This module is pure orchestration over an injected DB connection and GitHub
# client. It does no timing and no I/O of its own, so it is unit-testable with
# an in-memory database and a mocked HTTP transport. The interval timer lives in
# the server.

log = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """Counts from reconciling one repo (or summed across repos)."""

    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0

    def __iadd__(self, other: "SweepResult") -> "SweepResult":
        self.added += other.added
        self.updated += other.updated
        self.removed += other.removed
        self.unchanged += other.unchanged
        return self


def reconcile_repo(
    conn: sqlite3.Connection,
    client: GitHubClient,
    repo: sqlite3.Row,
) -> SweepResult:
    """
    Reconcile one repository's tracked PRs against the GitHub API.

    Adds new PRs, refreshes PRs whose head SHA moved (re-fetching their changed
    files), and removes PRs that are no longer open. PRs not targeting the
    repo's default branch are ignored. Changed files are only fetched for new or
    stale PRs — unchanged PRs cost nothing beyond the single list call.

    Commits once at the end so a sweep is atomic per repo.
    """
    result = SweepResult()

    live = [
        pr
        for pr in client.list_open_prs(repo["owner"], repo["name"])
        if pr.base_ref == repo["default_branch"]
    ]
    live_numbers = {pr.number for pr in live}
    tracked = queries.list_tracked_prs(conn, repo["id"])

    for pr in live:
        existing = tracked.get(pr.number)
        if existing is not None and existing["head_sha"] == pr.head_sha:
            result.unchanged += 1
            continue

        pr_id = queries.upsert_pull_request(
            conn,
            repo["id"],
            pr_number=pr.number,
            head_sha=pr.head_sha,
            base_branch=pr.base_ref,
            title=pr.title,
            author=pr.author,
            updated_at=pr.updated_at,
        )
        files = client.changed_files(repo["owner"], repo["name"], pr.number)
        queries.replace_pr_files(conn, pr_id, files)

        if existing is None:
            result.added += 1
        else:
            result.updated += 1

    for pr_number, row in tracked.items():
        if pr_number not in live_numbers:
            queries.delete_pull_request(conn, row["id"])
            result.removed += 1

    conn.commit()
    return result


# A factory that builds a GitHubClient for a given installation id. Injected so
# tests can supply a client backed by a mock transport. Defaults to the real
# constructor in reconcile_all.
ClientFactory = Callable[[int], GitHubClient]


def reconcile_all(
    conn: sqlite3.Connection,
    client_factory: ClientFactory,
) -> SweepResult:
    """
    Reconcile every tracked repository, summing per-repo results.

    A failure on one repo is logged and skipped so a single bad repo (revoked
    install, transient API error) doesn't abort the whole sweep.
    """
    total = SweepResult()
    repos = conn.execute("SELECT * FROM repositories").fetchall()

    for repo in repos:
        client = client_factory(repo["installation_id"])
        try:
            total += reconcile_repo(conn, client, repo)
        except Exception:
            log.exception(
                "reconcile failed for %s/%s", repo["owner"], repo["name"]
            )

    return total


def app_client_factory(auth: AppAuth) -> ClientFactory:
    """Build a ClientFactory that creates installation-scoped clients from one AppAuth."""
    return lambda installation_id: GitHubClient(auth, installation_id)
