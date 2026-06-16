from __future__ import annotations

import sqlite3

import httpx

from git_lookout.github.client import GitHubClient
from git_lookout.storage import queries
from git_lookout.storage.schema import connect
from git_lookout.sweep import SweepResult, reconcile_all, reconcile_repo


class FakeAuth:
    """Returns a fixed installation token without any network (as in test_client)."""

    def installation_token(self, installation_id: int) -> str:
        return "ghs_test"


class GitHubFake:
    """
    A scripted GitHub API over httpx.MockTransport.

    `prs` is the list of open-PR payloads returned by the list endpoint; `files`
    maps pr_number -> changed file list. Counts requests per path prefix so tests
    can assert that changed-files is (or isn't) fetched.
    """

    def __init__(self, prs: list[dict], files: dict[int, list[str]]):
        self.prs = prs
        self.files = files
        self.list_calls = 0
        self.file_calls: dict[int, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            self.list_calls += 1
            return httpx.Response(200, json=self.prs)
        if path.endswith("/files"):
            number = int(path.split("/pulls/")[1].split("/files")[0])
            self.file_calls[number] = self.file_calls.get(number, 0) + 1
            files = self.files.get(number, [])
            return httpx.Response(200, json=[{"filename": f} for f in files])
        raise AssertionError(f"unexpected request: {path}")

    def client(self) -> GitHubClient:
        transport = httpx.MockTransport(self.handler)
        http = httpx.Client(base_url="https://api.github.com", transport=transport)
        return GitHubClient(FakeAuth(), installation_id=99, client=http)


def _pr(number: int, *, sha: str, base: str = "main") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "head": {"sha": sha, "ref": f"feature-{number}"},
        "base": {"ref": base},
        "user": {"login": "octocat"},
        "updated_at": "2026-06-16T00:00:00Z",
    }


def _repo_row(conn: sqlite3.Connection) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id, default_branch) "
        "VALUES (1, 'acme', 'widgets', 99, 'main')"
    )
    conn.commit()
    return queries.get_repository(conn, "acme", "widgets")


def _files(conn: sqlite3.Connection, pr_number: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT pf.file_path
        FROM pr_files pf
        JOIN pull_requests pr ON pr.id = pf.pr_id
        WHERE pr.pr_number = ?
        """,
        (pr_number,),
    ).fetchall()
    return {r["file_path"] for r in rows}


# ---- reconcile_repo -------------------------------------------------------


def test_new_pr_is_added_with_files():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    fake = GitHubFake([_pr(1, sha="s1")], {1: ["a.py", "b.py"]})

    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(added=1)
    assert set(queries.list_tracked_prs(conn, 1)) == {1}
    assert _files(conn, 1) == {"a.py", "b.py"}


def test_stale_pr_is_updated_and_files_refreshed():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    # Seed an out-of-date PR row + stale files.
    pr_id = queries.upsert_pull_request(
        conn,
        1,
        pr_number=1,
        head_sha="old",
        base_branch="main",
        title="PR 1",
        author="octocat",
        updated_at="2026-06-15T00:00:00Z",
    )
    queries.replace_pr_files(conn, pr_id, ["old.py"])
    conn.commit()

    fake = GitHubFake([_pr(1, sha="new")], {1: ["new.py"]})
    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(updated=1)
    assert queries.list_tracked_prs(conn, 1)[1]["head_sha"] == "new"
    assert _files(conn, 1) == {"new.py"}
    assert fake.file_calls.get(1) == 1


def test_unchanged_pr_skips_file_fetch():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    queries.upsert_pull_request(
        conn,
        1,
        pr_number=1,
        head_sha="same",
        base_branch="main",
        title="PR 1",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    conn.commit()

    fake = GitHubFake([_pr(1, sha="same")], {1: ["a.py"]})
    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(unchanged=1)
    assert fake.file_calls.get(1) is None  # never fetched changed files


def test_closed_pr_is_removed():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    pr_id = queries.upsert_pull_request(
        conn,
        1,
        pr_number=7,
        head_sha="abc",
        base_branch="main",
        title="PR 7",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    queries.replace_pr_files(conn, pr_id, ["gone.py"])
    conn.commit()

    fake = GitHubFake([], {})  # PR 7 no longer open
    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(removed=1)
    assert queries.list_tracked_prs(conn, 1) == {}
    assert conn.execute("SELECT COUNT(*) FROM pr_files").fetchone()[0] == 0


def test_pr_targeting_non_default_branch_is_ignored():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    fake = GitHubFake(
        [_pr(1, sha="s1", base="develop"), _pr(2, sha="s2", base="main")],
        {2: ["a.py"]},
    )

    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(added=1)
    assert set(queries.list_tracked_prs(conn, 1)) == {2}
    assert fake.file_calls.get(1) is None


def test_mixed_sweep_counts():
    conn = connect(":memory:")
    repo = _repo_row(conn)
    # Tracked: PR 1 (unchanged), PR 2 (stale), PR 3 (will be removed).
    for number, sha in ((1, "s1"), (2, "old"), (3, "s3")):
        queries.upsert_pull_request(
            conn,
            1,
            pr_number=number,
            head_sha=sha,
            base_branch="main",
            title=f"PR {number}",
            author="octocat",
            updated_at="2026-06-16T00:00:00Z",
        )
    conn.commit()

    # Live: PR 1 unchanged, PR 2 new sha, PR 4 brand new. PR 3 gone.
    fake = GitHubFake(
        [_pr(1, sha="s1"), _pr(2, sha="new"), _pr(4, sha="s4")],
        {2: ["b.py"], 4: ["d.py"]},
    )
    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(added=1, updated=1, removed=1, unchanged=1)
    assert set(queries.list_tracked_prs(conn, 1)) == {1, 2, 4}


# ---- reconcile_all --------------------------------------------------------


def test_reconcile_all_sums_and_isolates_failures():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id) "
        "VALUES (1, 'acme', 'good', 99), (2, 'acme', 'bad', 99)"
    )
    conn.commit()

    good = GitHubFake([_pr(1, sha="s1")], {1: ["a.py"]})

    def factory(installation_id: int) -> GitHubClient:
        # The 'bad' repo's list endpoint errors; the 'good' one succeeds.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/bad/" in request.url.path:
                return httpx.Response(500, json={"message": "boom"})
            return good.handler(request)

        transport = httpx.MockTransport(handler)
        http = httpx.Client(base_url="https://api.github.com", transport=transport)
        return GitHubClient(FakeAuth(), installation_id=installation_id, client=http)

    result = reconcile_all(conn, factory)

    # 'bad' is logged and skipped; 'good' still reconciles its one new PR.
    assert result == SweepResult(added=1)
    assert set(queries.list_tracked_prs(conn, 1)) == {1}
