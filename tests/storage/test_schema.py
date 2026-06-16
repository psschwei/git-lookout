from __future__ import annotations

import sqlite3

import pytest

from git_lookout.storage.schema import SCHEMA_VERSION, connect, migrate


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def test_all_four_tables_created():
    conn = connect(":memory:")
    tables = _table_names(conn)
    assert {"repositories", "pull_requests", "pr_files", "conflict_checks"} <= tables


def test_pr_files_path_index_created():
    conn = connect(":memory:")
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert "idx_pr_files_path" in names


def test_schema_version_recorded():
    conn = connect(":memory:")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_migrate_is_idempotent():
    conn = connect(":memory:")
    migrate(conn)  # second run must not error or duplicate anything
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_foreign_keys_enabled():
    conn = connect(":memory:")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_pr_files_cascade_on_pr_delete():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (1, 'acme', 'widgets', 99)"
    )
    conn.execute(
        "INSERT INTO pull_requests "
        "(id, repo_id, pr_number, head_sha, base_branch, updated_at) "
        "VALUES (1, 1, 42, 'abc123', 'main', '2026-06-16T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO pr_files (pr_id, file_path) VALUES (1, 'src/api/orders.ts')"
    )
    conn.commit()

    conn.execute("DELETE FROM pull_requests WHERE id = 1")
    conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM pr_files").fetchone()[0]
    assert remaining == 0


def test_repositories_owner_name_unique():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (owner, name, installation_id) "
        "VALUES ('acme', 'widgets', 1)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO repositories (owner, name, installation_id) "
            "VALUES ('acme', 'widgets', 2)"
        )


def test_pull_requests_unique_per_repo():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (1, 'acme', 'widgets', 1)"
    )
    conn.execute(
        "INSERT INTO pull_requests "
        "(repo_id, pr_number, head_sha, base_branch, updated_at) "
        "VALUES (1, 42, 'abc', 'main', '2026-06-16T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO pull_requests "
            "(repo_id, pr_number, head_sha, base_branch, updated_at) "
            "VALUES (1, 42, 'def', 'main', '2026-06-16T01:00:00Z')"
        )


def test_conflict_checks_unique_per_pair():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (1, 'acme', 'widgets', 1)"
    )
    conn.execute(
        "INSERT INTO conflict_checks "
        "(repo_id, pr_a_number, pr_b_number, pr_a_sha, pr_b_sha, status, checked_at) "
        "VALUES (1, 1, 2, 'a', 'b', 'conflict', '2026-06-16T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO conflict_checks "
            "(repo_id, pr_a_number, pr_b_number, pr_a_sha, pr_b_sha, status, checked_at) "
            "VALUES (1, 1, 2, 'c', 'd', 'clean', '2026-06-16T01:00:00Z')"
        )


def test_connect_creates_db_file(tmp_path):
    db_path = tmp_path / "nested" / "git-lookout.db"
    conn = connect(db_path)
    conn.close()
    assert db_path.exists()


def test_overlap_query_uses_index(tmp_path):
    # The overlap pre-filter query from the spec should find PRs sharing a file.
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (1, 'acme', 'widgets', 1)"
    )
    for pr_id, number in ((1, 10), (2, 20), (3, 30)):
        conn.execute(
            "INSERT INTO pull_requests "
            "(id, repo_id, pr_number, head_sha, base_branch, updated_at) "
            "VALUES (?, 1, ?, 'sha', 'main', '2026-06-16T00:00:00Z')",
            (pr_id, number),
        )
    # PR 1 and PR 2 share orders.ts; PR 3 touches something else.
    conn.execute("INSERT INTO pr_files VALUES (1, 'src/api/orders.ts')")
    conn.execute("INSERT INTO pr_files VALUES (2, 'src/api/orders.ts')")
    conn.execute("INSERT INTO pr_files VALUES (3, 'src/util/log.ts')")
    conn.commit()

    rows = conn.execute(
        """
        SELECT DISTINCT pf2.pr_id
        FROM pr_files pf1
        JOIN pr_files pf2 ON pf1.file_path = pf2.file_path
        WHERE pf1.pr_id = :pr_id
          AND pf2.pr_id != :pr_id
        """,
        {"pr_id": 1},
    ).fetchall()
    assert {row["pr_id"] for row in rows} == {2}
