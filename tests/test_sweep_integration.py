"""
Phase 2 integration tests.

The unit tests in test_sweep.py exercise each reconcile branch in isolation
against an in-memory database. These tests instead drive the *whole* Phase 2
stack — the real GitHubClient over httpx, the real on-disk SQLite database via
schema.connect, and reconcile_repo — through the sequence of state transitions
the build-plan calls out as validation:

  1. First sweep populates pull_requests + pr_files from "GitHub".
  2. Re-run with no changes is a true no-op (zero database writes).
  3. A new commit on a PR updates its SHA and refreshes its files.
  4. Closing a PR removes it and cascades away its files.

The GitHub side is a mutable scripted fake over httpx.MockTransport: mutating
its `prs`/`files` between sweeps simulates real-world activity (pushes, closes)
without touching the network. Everything below the client — pagination, the SQL,
the foreign-key cascade — is the production code path.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from git_lookout.github.client import GitHubClient
from git_lookout.storage import queries
from git_lookout.storage.schema import connect
from git_lookout.sweep import SweepResult, reconcile_repo


class FakeAuth:
    """Returns a fixed installation token without any network."""

    def installation_token(self, installation_id: int) -> str:
        return "ghs_test"


class GitHubFake:
    """
    A mutable scripted GitHub API over httpx.MockTransport.

    `prs` is the list of open-PR payloads the list endpoint returns; `files`
    maps pr_number -> changed file list. Both are mutated between sweeps to
    simulate pushes and closes. Paginates at a small page size so the client's
    Link-header pagination is genuinely exercised across multiple pages.
    """

    _PER_PAGE = 2

    def __init__(self) -> None:
        self.prs: list[dict] = []
        self.files: dict[int, list[str]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/pulls"):
            return self._paged(request, self.prs)
        if path.endswith("/files"):
            number = int(path.split("/pulls/")[1].split("/files")[0])
            entries = [{"filename": f} for f in self.files.get(number, [])]
            return self._paged(request, entries)
        raise AssertionError(f"unexpected request: {path}")

    def _paged(self, request: httpx.Request, items: list) -> httpx.Response:
        """Serve `items` one page at a time, advertising rel="next" via Link."""
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * self._PER_PAGE
        chunk = items[start : start + self._PER_PAGE]
        headers = {}
        if start + self._PER_PAGE < len(items):
            next_url = request.url.copy_set_param("page", page + 1)
            headers["Link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

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


def _make_repo(conn) -> object:
    conn.execute(
        "INSERT INTO repositories (id, owner, name, installation_id, default_branch) "
        "VALUES (1, 'acme', 'widgets', 99, 'main')"
    )
    conn.commit()
    return queries.get_repository(conn, "acme", "widgets")


def _snapshot(conn) -> dict[int, tuple[str, frozenset[str]]]:
    """The full tracked state: pr_number -> (head_sha, frozenset of changed files)."""
    rows = conn.execute(
        """
        SELECT pr.pr_number, pr.head_sha, pf.file_path
        FROM pull_requests pr
        LEFT JOIN pr_files pf ON pf.pr_id = pr.id
        """
    ).fetchall()
    state: dict[int, tuple[str, set[str]]] = {}
    for r in rows:
        sha, files = state.setdefault(r["pr_number"], (r["head_sha"], set()))
        if r["file_path"] is not None:
            files.add(r["file_path"])
    return {num: (sha, frozenset(files)) for num, (sha, files) in state.items()}


def test_full_phase2_lifecycle(tmp_path: Path) -> None:
    """Walk the build-plan's validation sequence end-to-end on a real on-disk DB."""
    db = tmp_path / "lookout.db"
    conn = connect(db)
    repo = _make_repo(conn)
    fake = GitHubFake()

    # --- 1. First sweep: two open PRs get tracked with their files ----------
    # PR 5 spans three files so it needs a second page from the files endpoint.
    fake.prs = [_pr(5, sha="aaa"), _pr(6, sha="bbb")]
    fake.files = {5: ["api.py", "db.py", "util.py"], 6: ["web.py"]}

    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(added=2)
    assert _snapshot(conn) == {
        5: ("aaa", frozenset({"api.py", "db.py", "util.py"})),
        6: ("bbb", frozenset({"web.py"})),
    }

    # --- 2. Re-run with no changes: a true no-op, zero database writes -------
    writes_before = conn.total_changes
    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(unchanged=2)
    # reconcile_repo always calls commit(); the no-op assertion is that no rows
    # were inserted, updated, or deleted between the two snapshots.
    assert conn.total_changes == writes_before
    assert _snapshot(conn) == {
        5: ("aaa", frozenset({"api.py", "db.py", "util.py"})),
        6: ("bbb", frozenset({"web.py"})),
    }

    # --- 3. Push a commit to PR 5: SHA moves, files refresh -----------------
    fake.prs = [_pr(5, sha="ccc"), _pr(6, sha="bbb")]
    fake.files[5] = ["api.py", "renamed.py"]  # util.py/db.py dropped, renamed.py added

    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(updated=1, unchanged=1)
    assert _snapshot(conn)[5] == ("ccc", frozenset({"api.py", "renamed.py"}))
    assert _snapshot(conn)[6] == ("bbb", frozenset({"web.py"}))

    # --- 4. Close PR 6: it and its files are removed ------------------------
    fake.prs = [_pr(5, sha="ccc")]

    result = reconcile_repo(conn, fake.client(), repo)

    assert result == SweepResult(removed=1, unchanged=1)
    assert set(_snapshot(conn)) == {5}
    # The cascade deleted PR 6's pr_files rows too — only PR 5's remain.
    assert conn.execute("SELECT COUNT(*) FROM pr_files").fetchone()[0] == 2

    conn.close()


def test_state_survives_reconnect(tmp_path: Path) -> None:
    """
    Tracked state is durable: a sweep, then a fresh connection to the same file,
    sees the persisted PRs. Guards against anything relying on connection-local
    state and confirms schema.connect is a no-op on an existing database.
    """
    db = tmp_path / "lookout.db"

    conn = connect(db)
    repo = _make_repo(conn)
    fake = GitHubFake()
    fake.prs = [_pr(1, sha="s1")]
    fake.files = {1: ["only.py"]}
    reconcile_repo(conn, fake.client(), repo)
    conn.close()

    # Reopen the same database; the schema re-application must not wipe data.
    reopened = connect(db)
    assert _snapshot(reopened) == {1: ("s1", frozenset({"only.py"}))}
    reopened.close()
