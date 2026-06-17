"""
Phase 3 integration tests.

These drive the whole API stack — the real FastAPI app over fastapi's
TestClient, a real on-disk SQLite database via schema.connect, and a real
BareCloneManager fetching from a real (local) git "remote" — through the
build-plan's validation list (docs/build-plan.md):

  - a ref that conflicts with an open PR returns the correct conflict;
  - a ref that touches nothing shared returns an empty list;
  - a ref that overlaps a PR's file but edits a different region returns clean;
  - an invalid repo / unknown ref returns a 4xx, not a 500;
  - a request with no / invalid auth is rejected.

The GitHub side is the only fake: the caller-token check (GET /repos/{o}/{r})
is served by an httpx.MockTransport swapped into the server's validator client,
so the real auth dependency — header parsing, bearer extraction, repo_access —
runs end to end without the network. Everything below it (git fetch, the SQL
overlap pre-filter, merge-tree, the pipeline) is the production code path.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from git_lookout import server
from git_lookout.storage import queries
from git_lookout.storage.schema import connect


# --- A local git repo standing in for the GitHub remote ---------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """
    A git repo with a `main` baseline and three branches:

      - pr-branch:     edits the first line of shared.py  (an "open PR")
      - conflicting:   edits the first line of shared.py  (collides with pr-branch)
      - non-conflict:  edits the last line of shared.py   (overlaps file, not region)
      - unrelated:     edits a different file entirely
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "shared.py").write_text("line1\nline2\nline3\n")
    _commit(repo, "init")

    # An open PR's branch: rewrite line1.
    _git(repo, "checkout", "-b", "pr-branch")
    (repo / "shared.py").write_text("PR-line1\nline2\nline3\n")
    _commit(repo, "pr edits line1")

    # Conflicts with pr-branch: also rewrites line1, differently.
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "conflicting")
    (repo / "shared.py").write_text("OTHER-line1\nline2\nline3\n")
    _commit(repo, "conflicting edits line1")

    # Overlaps the file but edits line3 — no region collision with pr-branch.
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "non-conflict")
    (repo / "shared.py").write_text("line1\nline2\nNEW-line3\n")
    _commit(repo, "edits line3 only")

    # Touches a different file entirely.
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "unrelated")
    (repo / "other.py").write_text("hello\n")
    _commit(repo, "adds other.py")

    _git(repo, "checkout", "main")
    return repo


@pytest.fixture
def client(
    tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """
    Boot the real app against on-disk fixtures, seed one tracked repo + open PR,
    and route GitHub token validation through a MockTransport that accepts any
    token (so the auth path runs, but the no/invalid-token cases are still
    exercised before the API call by the dependency itself).
    """
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lookout.db"))
    monkeypatch.setenv("REPO_CACHE_DIR", str(tmp_path / "repos"))
    # No GitHub App creds: the sweep stays disabled, /api/check still works.
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)

    with TestClient(server.app) as c:
        # Pre-clone the "remote" so fetch_ref pulls branches from our local repo.
        server._manager.ensure_clone("acme", "widgets", str(remote))

        # Seed the tracked repo and one open PR (pr-branch) with its changed files.
        conn = connect(str(tmp_path / "lookout.db"))
        conn.execute(
            "INSERT INTO repositories (id, owner, name, installation_id, default_branch) "
            "VALUES (1, 'acme', 'widgets', 99, 'main')"
        )
        head_sha = _git(remote, "rev-parse", "pr-branch").strip()
        pr_id = queries.upsert_pull_request(
            conn,
            1,
            pr_number=42,
            head_sha=head_sha,
            base_branch="main",
            title="Add input validation",
            author="octocat",
            updated_at="2026-06-16T00:00:00Z",
        )
        queries.replace_pr_files(conn, pr_id, ["shared.py"])
        conn.commit()
        conn.close()

        # Make the PR's head SHA reachable in the bare clone for merge-tree.
        server._manager.fetch_ref("acme", "widgets", "pr-branch")

        # Accept any caller token: GET /repos/{o}/{r} -> 200.
        def ok(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"full_name": "acme/widgets"})

        server._validator_client = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(ok)
        )

        yield c


_AUTH = {"Authorization": "Bearer gho_caller"}


def _check(client: TestClient, ref: str, **kwargs) -> httpx.Response:
    body = {"repo": "acme/widgets", "ref": ref}
    return client.post("/api/check", json=body, **kwargs)


def test_conflicting_ref_returns_conflict(client: TestClient):
    resp = _check(client, "conflicting", headers=_AUTH)

    assert resp.status_code == 200
    conflicts = resp.json()["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["pr_number"] == 42
    assert c["title"] == "Add input validation"
    assert c["conflicting_files"] == ["shared.py"]
    assert c["conflict_regions"]  # at least one region located


def test_non_conflicting_overlap_returns_clean(client: TestClient):
    # Overlaps shared.py with PR 42 but edits a different region -> no conflict.
    resp = _check(client, "non-conflict", headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"conflicts": []}


def test_unrelated_ref_returns_empty(client: TestClient):
    # Touches other.py only — the overlap pre-filter excludes PR 42 entirely.
    resp = _check(client, "unrelated", headers=_AUTH)

    assert resp.status_code == 200
    assert resp.json() == {"conflicts": []}


def test_unknown_ref_is_404(client: TestClient):
    resp = _check(client, "does-not-exist", headers=_AUTH)
    assert resp.status_code == 404


def test_untracked_repo_is_404(client: TestClient):
    resp = client.post(
        "/api/check",
        json={"repo": "acme/unknown", "ref": "conflicting"},
        headers=_AUTH,
    )
    assert resp.status_code == 404


def test_malformed_repo_is_400(client: TestClient):
    resp = client.post(
        "/api/check", json={"repo": "not-a-slug", "ref": "conflicting"}, headers=_AUTH
    )
    assert resp.status_code == 400


def test_missing_auth_is_401(client: TestClient):
    resp = _check(client, "conflicting")  # no Authorization header
    assert resp.status_code == 401


def test_token_without_repo_access_is_403(client: TestClient):
    # Swap the validator to reject (404 hides private repos -> no access).
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    server._validator_client = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(forbidden),
    )
    resp = _check(client, "conflicting", headers=_AUTH)
    assert resp.status_code == 403
