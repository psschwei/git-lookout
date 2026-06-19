from __future__ import annotations

import logging
import sqlite3

from git_lookout.core.git_manager import BareCloneManager
from git_lookout.core.models import PRInfo
from git_lookout.core.pipeline import run_pipeline
from git_lookout.github.client import GitHubClient
from git_lookout.reporter import CommentPR, render_conflict, render_resolved
from git_lookout.storage import queries
from git_lookout.webhook.events import (
    CLOSE_ACTIONS,
    UPDATE_ACTIONS,
    PullRequestEvent,
)

# The webhook orchestration — the "passive monitoring" path. A pull_request
# delivery lands here after signature verification and parsing. We reconcile the
# *one* PR it concerns into the DB, run it against the open PRs it overlaps, and
# post / update / resolve conflict comments on both sides of each pair. The
# periodic reconciliation sweep (sweep.py) remains the safety net for anything a
# webhook misses; this path only touches the triggering PR.
#
# Pure orchestration over injected dependencies (DB connection, git manager,
# GitHub client) — no timing, no HTTP server concerns — so it is testable with
# an in-memory DB, a local git remote, and a mock-transport client. The caller
# owns the connection and commits.

log = logging.getLogger(__name__)


def handle_pull_request_event(
    conn: sqlite3.Connection,
    manager: BareCloneManager,
    client: GitHubClient,
    event: PullRequestEvent,
    *,
    clone_url: str,
    now: str,
) -> None:
    """
    Dispatch a parsed pull_request event to the update or close flow.

    ``clone_url`` is the (installation-authenticated) git URL used to ensure the
    bare clone exists before any fetch. ``now`` is an ISO-8601 timestamp stamped
    onto conflict_checks rows. Actions outside the update/close sets (labeled,
    edited, assigned, …) are no-ops — they can't change the conflict landscape.
    Commits once at the end so handling one delivery is atomic.
    """
    repo = queries.get_repository(conn, event.repo_owner, event.repo_name)
    if repo is None:
        # Not a tracked repo — the App may be installed but the repo never
        # reconciled in. The sweep adds repositories; nothing to do here.
        log.info(
            "webhook for untracked repo %s/%s; ignoring",
            event.repo_owner,
            event.repo_name,
        )
        return

    # Only PRs targeting the repo's default branch are in scope, matching the
    # sweep. A PR onto some other branch can't conflict with what we track.
    if event.action in UPDATE_ACTIONS:
        if event.pr.base_ref != repo["default_branch"]:
            log.info(
                "PR #%d targets %s, not default branch %s; ignoring",
                event.pr.number,
                event.pr.base_ref,
                repo["default_branch"],
            )
            return
        _process_pr_update(conn, manager, client, repo, event, clone_url, now)
    elif event.action in CLOSE_ACTIONS:
        _process_pr_close(conn, client, repo, event)
    else:
        log.debug("pull_request action %r is not actionable; ignoring", event.action)
        return

    conn.commit()


def _process_pr_update(
    conn: sqlite3.Connection,
    manager: BareCloneManager,
    client: GitHubClient,
    repo: sqlite3.Row,
    event: PullRequestEvent,
    clone_url: str,
    now: str,
) -> None:
    """
    Reconcile the triggering PR, then re-check it against every overlapping PR.

    Upserts the PR and refreshes its changed files (so the overlap pre-filter is
    current), then for each overlapping open PR runs merge-tree and reconciles the
    conflict_checks row and its comments. The triggering PR is excluded from its
    own overlap set.
    """
    pr = event.pr
    owner, name, repo_id = repo["owner"], repo["name"], repo["id"]

    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=pr.number,
        head_sha=pr.head_sha,
        base_branch=pr.base_ref,
        title=pr.title,
        author=pr.author,
        updated_at=pr.updated_at,
    )
    files = client.changed_files(owner, name, pr.number)
    queries.replace_pr_files(conn, pr_id, files)

    overlapping = [
        row
        for row in queries.prs_overlapping_files(conn, repo_id, files)
        if row["pr_number"] != pr.number
    ]
    if not overlapping:
        return

    # Both heads must be present locally for merge-tree. Ensure the clone exists
    # and pull all branch heads in one fetch (covers the triggering PR and every
    # candidate). Fork heads are out of scope for v1 — same assumption as the
    # API path, which fetches refs/heads/<ref>.
    manager.ensure_clone(owner, name, clone_url)
    manager.fetch(owner, name)

    triggering = CommentPR(number=pr.number, title=pr.title)
    for other in overlapping:
        other_pr = CommentPR(number=other["pr_number"], title=other["title"] or "")
        _reconcile_pair(
            conn,
            manager,
            client,
            repo,
            triggering=triggering,
            triggering_sha=pr.head_sha,
            other=other_pr,
            other_sha=other["head_sha"],
            now=now,
        )


def _reconcile_pair(
    conn: sqlite3.Connection,
    manager: BareCloneManager,
    client: GitHubClient,
    repo: sqlite3.Row,
    *,
    triggering: CommentPR,
    triggering_sha: str,
    other: CommentPR,
    other_sha: str,
    now: str,
) -> None:
    """
    Re-check one PR pair and reconcile its conflict_checks row + comments.

    SHA-skip: if a prior row recorded both PRs at exactly these SHAs, the result
    can't have changed — return without touching git or GitHub. Otherwise run
    merge-tree and apply the comment lifecycle implied by the status transition:
      clean/none → conflict : post a comment on both PRs
      conflict   → conflict : update both comment bodies (files may have changed)
      conflict   → clean     : mark both comments resolved
      clean/none → clean     : record the result, no comment
    """
    owner, name, repo_id = repo["owner"], repo["name"], repo["id"]

    existing = queries.get_conflict_check(
        conn, repo_id, triggering.number, other.number
    )
    if existing is not None and _shas_unchanged(
        existing, triggering.number, triggering_sha, other_sha
    ):
        return

    result = manager.merge_tree(
        owner,
        name,
        PRInfo(number=triggering.number, title=triggering.title,
               head_sha=triggering_sha, head_ref=""),
        PRInfo(number=other.number, title=other.title,
               head_sha=other_sha, head_ref=""),
    )
    result = run_pipeline(result)
    conflicting_files = result.conflicting_files
    status = "conflict" if conflicting_files else "clean"

    was_conflict = existing is not None and existing["status"] == "conflict"
    comment_trig, comment_other = _existing_comment_ids(
        existing, triggering.number
    )

    if status == "conflict":
        body_trig = render_conflict(triggering, other, conflicting_files)
        body_other = render_conflict(other, triggering, conflicting_files)
        comment_trig = _post_or_update(
            client, owner, name, triggering.number, comment_trig, body_trig
        )
        comment_other = _post_or_update(
            client, owner, name, other.number, comment_other, body_other
        )
    elif was_conflict:
        # conflict → clean: resolve whatever comments we previously posted.
        if comment_trig is not None:
            client.update_issue_comment(
                owner, name, comment_trig, render_resolved(other.number)
            )
        if comment_other is not None:
            client.update_issue_comment(
                owner, name, comment_other, render_resolved(triggering.number)
            )

    queries.upsert_conflict_check(
        conn,
        repo_id,
        pr_a=triggering.number,
        pr_b=other.number,
        sha_a=triggering_sha,
        sha_b=other_sha,
        status=status,
        conflicting_files=conflicting_files,
        comment_id_a=comment_trig,
        comment_id_b=comment_other,
        checked_at=now,
    )


def _process_pr_close(
    conn: sqlite3.Connection,
    client: GitHubClient,
    repo: sqlite3.Row,
    event: PullRequestEvent,
) -> None:
    """
    Clean up after a PR is closed or merged.

    Deletes the PR (its pr_files cascade) and, for every conflict_checks row
    involving it, marks the *surviving* PR's conflict comment resolved before
    dropping the row. The closed PR's own comment is left as-is — the PR is gone.
    """
    owner, name, repo_id = repo["owner"], repo["name"], repo["id"]
    closed = event.pr.number

    tracked = queries.get_pull_request(conn, repo_id, closed)
    if tracked is not None:
        queries.delete_pull_request(conn, tracked["id"])

    for check in queries.conflict_checks_for_pr(conn, repo_id, closed):
        if check["status"] == "conflict":
            survivor, survivor_comment = _other_side(check, closed)
            if survivor_comment is not None:
                client.update_issue_comment(
                    owner, name, survivor_comment, render_resolved(closed)
                )
        queries.delete_conflict_check(conn, check["id"])


# --- helpers ----------------------------------------------------------------


def _post_or_update(
    client: GitHubClient,
    owner: str,
    name: str,
    pr_number: int,
    comment_id: int | None,
    body: str,
) -> int:
    """Update the comment in place if we have its id, else post a new one. Returns the id."""
    if comment_id is not None:
        client.update_issue_comment(owner, name, comment_id, body)
        return comment_id
    return client.create_issue_comment(owner, name, pr_number, body)


def _shas_unchanged(
    row: sqlite3.Row, triggering_number: int, triggering_sha: str, other_sha: str
) -> bool:
    """
    True if ``row`` already records both PRs at exactly these SHAs.

    The row stores the pair in canonical order (lower number is pr_a), so we map
    the triggering/other SHAs onto a/b by which number is lower.
    """
    if triggering_number == row["pr_a_number"]:
        return row["pr_a_sha"] == triggering_sha and row["pr_b_sha"] == other_sha
    return row["pr_a_sha"] == other_sha and row["pr_b_sha"] == triggering_sha


def _existing_comment_ids(
    row: sqlite3.Row | None, triggering_number: int
) -> tuple[int | None, int | None]:
    """Return (triggering_comment_id, other_comment_id) from a stored row, mapping canonical a/b → trig/other."""
    if row is None:
        return None, None
    if triggering_number == row["pr_a_number"]:
        return row["comment_id_a"], row["comment_id_b"]
    return row["comment_id_b"], row["comment_id_a"]


def _other_side(row: sqlite3.Row, this_number: int) -> tuple[int, int | None]:
    """Given a conflict_checks row and one PR number, return the other PR's (number, comment_id)."""
    if this_number == row["pr_a_number"]:
        return row["pr_b_number"], row["comment_id_b"]
    return row["pr_a_number"], row["comment_id_a"]
