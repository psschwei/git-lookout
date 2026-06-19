from __future__ import annotations

import sqlite3

from git_lookout.storage import queries
from git_lookout.storage.schema import connect


def _repo(conn: sqlite3.Connection, repo_id: int = 1) -> int:
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (?, 'acme', 'widgets', 99)",
        (repo_id,),
    )
    conn.commit()
    return repo_id


# ---- get_repository -------------------------------------------------------


def test_get_repository_returns_row():
    conn = connect(":memory:")
    _repo(conn)
    row = queries.get_repository(conn, "acme", "widgets")
    assert row is not None
    assert row["installation_id"] == 99
    assert row["default_branch"] == "main"


def test_get_repository_missing_returns_none():
    conn = connect(":memory:")
    assert queries.get_repository(conn, "acme", "widgets") is None


# ---- upsert_pull_request --------------------------------------------------


def test_upsert_inserts_new_pr():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=42,
        head_sha="abc",
        base_branch="main",
        title="Title",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM pull_requests WHERE id = ?", (pr_id,)
    ).fetchone()
    assert row["pr_number"] == 42
    assert row["head_sha"] == "abc"


def test_upsert_updates_existing_pr_in_place():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    first = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=42,
        head_sha="abc",
        base_branch="main",
        title="Old",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    second = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=42,
        head_sha="def",
        base_branch="main",
        title="New",
        author="octocat",
        updated_at="2026-06-16T01:00:00Z",
    )
    conn.commit()

    assert first == second  # same row id, updated in place
    rows = conn.execute("SELECT * FROM pull_requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["head_sha"] == "def"
    assert rows[0]["title"] == "New"


# ---- replace_pr_files -----------------------------------------------------


def test_replace_pr_files_clears_then_inserts():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=1,
        head_sha="abc",
        base_branch="main",
        title=None,
        author=None,
        updated_at="2026-06-16T00:00:00Z",
    )

    queries.replace_pr_files(conn, pr_id, ["a.py", "b.py"])
    queries.replace_pr_files(conn, pr_id, ["b.py", "c.py"])
    conn.commit()

    paths = {
        r["file_path"]
        for r in conn.execute(
            "SELECT file_path FROM pr_files WHERE pr_id = ?", (pr_id,)
        )
    }
    assert paths == {"b.py", "c.py"}


def test_replace_pr_files_empty_clears_all():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=1,
        head_sha="abc",
        base_branch="main",
        title=None,
        author=None,
        updated_at="2026-06-16T00:00:00Z",
    )
    queries.replace_pr_files(conn, pr_id, ["a.py"])
    queries.replace_pr_files(conn, pr_id, [])
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM pr_files WHERE pr_id = ?", (pr_id,)
    ).fetchone()[0]
    assert count == 0


# ---- list_tracked_prs -----------------------------------------------------


def test_list_tracked_prs_keyed_by_number():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    for number, sha in ((10, "s10"), (20, "s20")):
        queries.upsert_pull_request(
            conn,
            repo_id,
            pr_number=number,
            head_sha=sha,
            base_branch="main",
            title=None,
            author=None,
            updated_at="2026-06-16T00:00:00Z",
        )
    conn.commit()

    tracked = queries.list_tracked_prs(conn, repo_id)
    assert set(tracked) == {10, 20}
    assert tracked[10]["head_sha"] == "s10"


# ---- prs_overlapping_files ------------------------------------------------


def _pr_with_files(
    conn: sqlite3.Connection, repo_id: int, number: int, files: list[str]
) -> int:
    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=number,
        head_sha=f"sha{number}",
        base_branch="main",
        title=f"PR {number}",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    queries.replace_pr_files(conn, pr_id, files)
    conn.commit()
    return pr_id


def test_overlapping_returns_prs_sharing_a_file():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    _pr_with_files(conn, repo_id, 1, ["api.py", "db.py"])
    _pr_with_files(conn, repo_id, 2, ["web.py"])

    rows = queries.prs_overlapping_files(conn, repo_id, ["api.py", "other.py"])

    assert {r["pr_number"] for r in rows} == {1}


def test_overlapping_dedups_pr_sharing_multiple_files():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    _pr_with_files(conn, repo_id, 1, ["api.py", "db.py"])

    rows = queries.prs_overlapping_files(conn, repo_id, ["api.py", "db.py"])

    # PR 1 touches both queried files but must appear exactly once.
    assert [r["pr_number"] for r in rows] == [1]


def test_overlapping_empty_input_returns_empty():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    _pr_with_files(conn, repo_id, 1, ["api.py"])

    assert queries.prs_overlapping_files(conn, repo_id, []) == []


def test_overlapping_scoped_to_repo():
    conn = connect(":memory:")
    repo_a = _repo(conn, repo_id=1)
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (2, 'acme', 'other', 99)"
    )
    conn.commit()
    _pr_with_files(conn, repo_a, 1, ["shared.py"])
    _pr_with_files(conn, 2, 2, ["shared.py"])

    rows = queries.prs_overlapping_files(conn, repo_a, ["shared.py"])

    assert {r["pr_number"] for r in rows} == {1}


# ---- delete_pull_request --------------------------------------------------


def test_delete_pull_request_cascades_to_files():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    pr_id = queries.upsert_pull_request(
        conn,
        repo_id,
        pr_number=1,
        head_sha="abc",
        base_branch="main",
        title=None,
        author=None,
        updated_at="2026-06-16T00:00:00Z",
    )
    queries.replace_pr_files(conn, pr_id, ["a.py", "b.py"])
    conn.commit()

    queries.delete_pull_request(conn, pr_id)
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM pull_requests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM pr_files").fetchone()[0] == 0


# ---- conflict_checks ------------------------------------------------------


def test_get_conflict_check_missing_returns_none():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    assert queries.get_conflict_check(conn, repo_id, 1, 2) is None


def test_upsert_conflict_check_canonicalizes_pair_order():
    # Insert with the higher number first; it must be stored as (lo, hi).
    conn = connect(":memory:")
    repo_id = _repo(conn)
    queries.upsert_conflict_check(
        conn,
        repo_id,
        pr_a=7,
        pr_b=3,
        sha_a="sha7",
        sha_b="sha3",
        status="conflict",
        conflicting_files=["x.py"],
        comment_id_a=700,
        comment_id_b=300,
        checked_at="2026-06-16T00:00:00Z",
    )
    conn.commit()
    row = queries.get_conflict_check(conn, repo_id, 3, 7)
    assert row["pr_a_number"] == 3 and row["pr_b_number"] == 7
    # SHAs and comment ids follow the canonical swap: a→3, b→7.
    assert row["pr_a_sha"] == "sha3" and row["pr_b_sha"] == "sha7"
    assert row["comment_id_a"] == 300 and row["comment_id_b"] == 700
    assert row["conflicting_files"] == '["x.py"]'


def test_get_conflict_check_is_order_independent():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    queries.upsert_conflict_check(
        conn, repo_id, pr_a=3, pr_b=7, sha_a="a", sha_b="b",
        status="clean", conflicting_files=[], comment_id_a=None,
        comment_id_b=None, checked_at="2026-06-16T00:00:00Z",
    )
    conn.commit()
    assert queries.get_conflict_check(conn, repo_id, 3, 7)["id"] == \
        queries.get_conflict_check(conn, repo_id, 7, 3)["id"]


def test_upsert_conflict_check_updates_in_place():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    first = queries.upsert_conflict_check(
        conn, repo_id, pr_a=1, pr_b=2, sha_a="old1", sha_b="old2",
        status="conflict", conflicting_files=["a.py"], comment_id_a=10,
        comment_id_b=20, checked_at="2026-06-16T00:00:00Z",
    )
    second = queries.upsert_conflict_check(
        conn, repo_id, pr_a=1, pr_b=2, sha_a="new1", sha_b="old2",
        status="clean", conflicting_files=[], comment_id_a=10,
        comment_id_b=20, checked_at="2026-06-16T01:00:00Z",
    )
    conn.commit()
    assert first == second  # same row id — upsert, not insert
    assert conn.execute("SELECT COUNT(*) FROM conflict_checks").fetchone()[0] == 1
    row = queries.get_conflict_check(conn, repo_id, 1, 2)
    assert row["status"] == "clean" and row["pr_a_sha"] == "new1"


def test_conflict_checks_for_pr_matches_either_side():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    queries.upsert_conflict_check(
        conn, repo_id, pr_a=1, pr_b=2, sha_a="a", sha_b="b", status="conflict",
        conflicting_files=[], comment_id_a=None, comment_id_b=None,
        checked_at="t",
    )
    queries.upsert_conflict_check(
        conn, repo_id, pr_a=2, pr_b=3, sha_a="b", sha_b="c", status="clean",
        conflicting_files=[], comment_id_a=None, comment_id_b=None,
        checked_at="t",
    )
    conn.commit()
    rows = queries.conflict_checks_for_pr(conn, repo_id, 2)
    assert len(rows) == 2  # PR 2 is in both pairs
    assert queries.conflict_checks_for_pr(conn, repo_id, 1) and \
        len(queries.conflict_checks_for_pr(conn, repo_id, 1)) == 1


def test_delete_conflict_check_removes_row():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    check_id = queries.upsert_conflict_check(
        conn, repo_id, pr_a=1, pr_b=2, sha_a="a", sha_b="b", status="clean",
        conflicting_files=[], comment_id_a=None, comment_id_b=None,
        checked_at="t",
    )
    conn.commit()
    queries.delete_conflict_check(conn, check_id)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM conflict_checks").fetchone()[0] == 0


def test_get_pull_request_by_number():
    conn = connect(":memory:")
    repo_id = _repo(conn)
    queries.upsert_pull_request(
        conn, repo_id, pr_number=9, head_sha="abc", base_branch="main",
        title="t", author="a", updated_at="t",
    )
    conn.commit()
    row = queries.get_pull_request(conn, repo_id, 9)
    assert row is not None and row["head_sha"] == "abc"
    assert queries.get_pull_request(conn, repo_id, 999) is None
