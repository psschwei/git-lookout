from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

# Query helpers over the schema in schema.py. These run SQL only — the caller
# owns the transaction and decides when to commit(), so a sweep can batch many
# writes into a single commit. No ORM; raw parameterized SQL over the four
# tables, matching the rest of the storage layer.


def get_repository(
    conn: sqlite3.Connection, owner: str, name: str
) -> sqlite3.Row | None:
    """Return the repository row for owner/name, or None if not tracked."""
    return conn.execute(
        "SELECT * FROM repositories WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()


def list_tracked_prs(
    conn: sqlite3.Connection, repo_id: int
) -> dict[int, sqlite3.Row]:
    """
    Return the repo's tracked pull requests keyed by ``pr_number``.

    The sweep uses this to detect which PRs are new, stale (head SHA changed),
    or gone — so each row carries at least ``id`` and ``head_sha``.
    """
    rows = conn.execute(
        "SELECT * FROM pull_requests WHERE repo_id = ?",
        (repo_id,),
    ).fetchall()
    return {row["pr_number"]: row for row in rows}


def prs_overlapping_files(
    conn: sqlite3.Connection, repo_id: int, file_paths: Iterable[str]
) -> list[sqlite3.Row]:
    """
    Return the repo's tracked PRs that touch at least one of ``file_paths``.

    This is the file-overlap pre-filter the API check runs before any git
    operations: only PRs sharing a changed file with the candidate ref can
    possibly conflict, so the expensive merge-tree pass is limited to these.

    Each PR appears once even when it shares several files (DISTINCT). An empty
    ``file_paths`` yields no rows without touching the database. The query rides
    the ``idx_pr_files_path`` index on ``pr_files.file_path``.
    """
    paths = list(file_paths)
    if not paths:
        return []

    placeholders = ",".join("?" for _ in paths)
    return conn.execute(
        f"""
        SELECT DISTINCT pr.*
        FROM pull_requests pr
        JOIN pr_files pf ON pf.pr_id = pr.id
        WHERE pr.repo_id = ? AND pf.file_path IN ({placeholders})
        """,
        (repo_id, *paths),
    ).fetchall()


def upsert_pull_request(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    pr_number: int,
    head_sha: str,
    base_branch: str,
    title: str | None,
    author: str | None,
    updated_at: str,
) -> int:
    """
    Insert a pull request, or update it in place if (repo_id, pr_number) exists.

    Returns the ``pull_requests.id`` of the row. ``lastrowid`` is unreliable for
    an upsert that takes the DO UPDATE branch, so the id is read back explicitly.
    """
    conn.execute(
        """
        INSERT INTO pull_requests
            (repo_id, pr_number, head_sha, base_branch, title, author, updated_at)
        VALUES (:repo_id, :pr_number, :head_sha, :base_branch, :title, :author, :updated_at)
        ON CONFLICT(repo_id, pr_number) DO UPDATE SET
            head_sha = excluded.head_sha,
            base_branch = excluded.base_branch,
            title = excluded.title,
            author = excluded.author,
            updated_at = excluded.updated_at
        """,
        {
            "repo_id": repo_id,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "base_branch": base_branch,
            "title": title,
            "author": author,
            "updated_at": updated_at,
        },
    )
    row = conn.execute(
        "SELECT id FROM pull_requests WHERE repo_id = ? AND pr_number = ?",
        (repo_id, pr_number),
    ).fetchone()
    return row["id"]


def replace_pr_files(
    conn: sqlite3.Connection, pr_id: int, file_paths: Iterable[str]
) -> None:
    """
    Replace a PR's tracked changed files with ``file_paths``.

    Clears the existing rows and inserts the current set. Called when a PR is new
    or its head SHA moved, so the overlap pre-filter reflects the latest diff.
    """
    conn.execute("DELETE FROM pr_files WHERE pr_id = ?", (pr_id,))
    conn.executemany(
        "INSERT INTO pr_files (pr_id, file_path) VALUES (?, ?)",
        [(pr_id, path) for path in file_paths],
    )


def get_pull_request(
    conn: sqlite3.Connection, repo_id: int, pr_number: int
) -> sqlite3.Row | None:
    """Return one tracked PR by number within a repo, or None."""
    return conn.execute(
        "SELECT * FROM pull_requests WHERE repo_id = ? AND pr_number = ?",
        (repo_id, pr_number),
    ).fetchone()


def delete_pull_request(conn: sqlite3.Connection, pr_id: int) -> None:
    """
    Delete a pull request. Its ``pr_files`` rows cascade-delete via the foreign
    key (foreign keys are enabled per-connection in :func:`schema.connect`).
    """
    conn.execute("DELETE FROM pull_requests WHERE id = ?", (pr_id,))


# --- conflict_checks ---------------------------------------------------------
#
# A conflict_checks row records the result of one pairwise PR-vs-PR merge-tree
# check from the webhook path. Pairs are stored in canonical order — the lower
# PR number is always pr_a_number — so each unordered pair maps to exactly one
# row (the UNIQUE(repo_id, pr_a_number, pr_b_number) constraint). The helpers
# below take an arbitrary (a, b) and canonicalize internally, so callers never
# have to sort first.


def _canonical_pair(a: int, b: int) -> tuple[int, int]:
    """Order a PR-number pair so the lower number is first (spec's canonical form)."""
    return (a, b) if a <= b else (b, a)


def get_conflict_check(
    conn: sqlite3.Connection, repo_id: int, pr_a: int, pr_b: int
) -> sqlite3.Row | None:
    """
    Return the conflict_checks row for a PR pair, or None.

    The pair is canonicalized, so ``get_conflict_check(c, r, 7, 3)`` and
    ``get_conflict_check(c, r, 3, 7)`` return the same row.
    """
    lo, hi = _canonical_pair(pr_a, pr_b)
    return conn.execute(
        """
        SELECT * FROM conflict_checks
        WHERE repo_id = ? AND pr_a_number = ? AND pr_b_number = ?
        """,
        (repo_id, lo, hi),
    ).fetchone()


def upsert_conflict_check(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    pr_a: int,
    pr_b: int,
    sha_a: str,
    sha_b: str,
    status: str,
    conflicting_files: list[str],
    comment_id_a: int | None,
    comment_id_b: int | None,
    checked_at: str,
) -> int:
    """
    Insert or update the conflict_checks row for a PR pair, returning its id.

    The pair (and its SHAs / comment ids) are canonicalized so ``pr_a`` is always
    the lower PR number on disk; the caller passes them in event order and this
    swaps as needed. ``conflicting_files`` is JSON-encoded into the TEXT column.

    Comment ids are carried through the upsert so a status change that updates the
    body (rather than creating a comment) keeps the existing ids.
    """
    lo, hi = _canonical_pair(pr_a, pr_b)
    if lo == pr_a:
        sha_lo, sha_hi = sha_a, sha_b
        comment_lo, comment_hi = comment_id_a, comment_id_b
    else:
        sha_lo, sha_hi = sha_b, sha_a
        comment_lo, comment_hi = comment_id_b, comment_id_a

    conn.execute(
        """
        INSERT INTO conflict_checks
            (repo_id, pr_a_number, pr_b_number, pr_a_sha, pr_b_sha,
             status, conflicting_files, comment_id_a, comment_id_b, checked_at)
        VALUES
            (:repo_id, :pr_a, :pr_b, :sha_a, :sha_b,
             :status, :files, :comment_a, :comment_b, :checked_at)
        ON CONFLICT(repo_id, pr_a_number, pr_b_number) DO UPDATE SET
            pr_a_sha = excluded.pr_a_sha,
            pr_b_sha = excluded.pr_b_sha,
            status = excluded.status,
            conflicting_files = excluded.conflicting_files,
            comment_id_a = excluded.comment_id_a,
            comment_id_b = excluded.comment_id_b,
            checked_at = excluded.checked_at
        """,
        {
            "repo_id": repo_id,
            "pr_a": lo,
            "pr_b": hi,
            "sha_a": sha_lo,
            "sha_b": sha_hi,
            "status": status,
            "files": json.dumps(conflicting_files),
            "comment_a": comment_lo,
            "comment_b": comment_hi,
            "checked_at": checked_at,
        },
    )
    row = conn.execute(
        """
        SELECT id FROM conflict_checks
        WHERE repo_id = ? AND pr_a_number = ? AND pr_b_number = ?
        """,
        (repo_id, lo, hi),
    ).fetchone()
    return row["id"]


def conflict_checks_for_pr(
    conn: sqlite3.Connection, repo_id: int, pr_number: int
) -> list[sqlite3.Row]:
    """
    Return every conflict_checks row that involves ``pr_number`` (on either side).

    Used by the PR-close cleanup to find which other PRs carry a conflict comment
    that must be marked resolved.
    """
    return conn.execute(
        """
        SELECT * FROM conflict_checks
        WHERE repo_id = ? AND (pr_a_number = ? OR pr_b_number = ?)
        """,
        (repo_id, pr_number, pr_number),
    ).fetchall()


def delete_conflict_check(conn: sqlite3.Connection, check_id: int) -> None:
    """Delete one conflict_checks row by id."""
    conn.execute("DELETE FROM conflict_checks WHERE id = ?", (check_id,))
