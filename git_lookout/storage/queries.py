from __future__ import annotations

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


def delete_pull_request(conn: sqlite3.Connection, pr_id: int) -> None:
    """
    Delete a pull request. Its ``pr_files`` rows cascade-delete via the foreign
    key (foreign keys are enabled per-connection in :func:`schema.connect`).
    """
    conn.execute("DELETE FROM pull_requests WHERE id = ?", (pr_id,))
