"""
Phase 4 integration tests.

These drive the whole webhook stack — the real FastAPI ``/webhook`` endpoint
over fastapi's TestClient, real HMAC signature verification, the real handler
orchestration, a real on-disk SQLite database via ``schema.connect``, and a real
``BareCloneManager`` fetching from a real (local) git "remote" — through the
build-plan's Phase 4 validation list (docs/build-plan.md):

  - open two conflicting PRs        → conflict comment posted on both;
  - push one PR to resolve          → both comments updated to "resolved";
  - push one PR to a new conflict    → a new comment appears;
  - close a PR                      → its conflict comment on the other goes "resolved";
  - push the same SHA again          → SHA-skip, no duplicate comment.

The GitHub API is the only fake: a ``GitHubFake`` over ``httpx.MockTransport``
serves ``GET /pulls/{n}/files`` from the local git repo's diff and records every
``POST``/``PATCH`` comment call so the test can assert on what the reporter
posted. Everything below the client — signature check, payload parsing, the SQL,
git fetch, merge-tree, the pipeline, the comment lifecycle — is production code.

Because the bare clone is fed from a local git repo, the server's
installation-authenticated clone URL is monkeypatched to that repo path, and the
App auth (which would otherwise require real credentials) is replaced with a
fake that returns a fixed token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from git_lookout import server
from git_lookout.github.client import GitHubClient
from git_lookout.storage import queries
from git_lookout.storage.schema import connect

WEBHOOK_SECRET = "phase4-secret"
INSTALLATION_ID = 99


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
    A git repo with `main` and two PR branches that conflict on shared.py:

      - pr-1: rewrites line1 one way
      - pr-2: rewrites line1 another way  (collides with pr-1)

    Plus spare branches the tests check out commits onto to simulate pushes:
      - pr-1-resolved: edits line3 instead of line1 (no longer collides)
      - pr-3:          a third branch that also rewrites line1 (new conflict)
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "shared.py").write_text("line1\nline2\nline3\n")
    _commit(repo, "init")

    _git(repo, "checkout", "-b", "pr-1")
    (repo / "shared.py").write_text("PR1-line1\nline2\nline3\n")
    _commit(repo, "pr-1 edits line1")

    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "pr-2")
    (repo / "shared.py").write_text("PR2-line1\nline2\nline3\n")
    _commit(repo, "pr-2 edits line1")

    # A future state of pr-1 that no longer touches line1 (resolves vs pr-2).
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "pr-1-resolved")
    (repo / "shared.py").write_text("line1\nline2\nPR1-line3\n")
    _commit(repo, "pr-1 moves to line3")

    # A third branch that rewrites line1 (introduces a fresh conflict).
    _git(repo, "checkout", "main")
    _git(repo, "checkout", "-b", "pr-3")
    (repo / "shared.py").write_text("PR3-line1\nline2\nline3\n")
    _commit(repo, "pr-3 edits line1")

    _git(repo, "checkout", "main")
    return repo


def _sha(remote: Path, ref: str) -> str:
    return _git(remote, "rev-parse", ref).strip()


# --- The GitHub API fake (files + comment capture) --------------------------


class FakeAuth:
    """Returns a fixed installation token without any network."""

    def installation_token(self, installation_id: int) -> str:
        return "ghs_test"

    def close(self) -> None:  # matches AppAuth.close, called on shutdown
        pass


class GitHubFake:
    """
    A GitHub REST fake over httpx.MockTransport.

    Serves ``GET /pulls/{n}/files`` from a caller-supplied map and records every
    comment ``POST`` (assigning incrementing ids) and ``PATCH`` so the test can
    inspect what the webhook posted. ``comments`` maps comment_id -> latest body;
    ``posted_on`` maps comment_id -> PR number it was created on.
    """

    def __init__(self, files: dict[int, list[str]]):
        self.files = files
        self.comments: dict[int, str] = {}
        self.posted_on: dict[int, int] = {}
        self._next_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/files"):
            number = int(path.split("/pulls/")[1].split("/files")[0])
            entries = [{"filename": f} for f in self.files.get(number, [])]
            return httpx.Response(200, json=entries)
        if request.method == "POST" and path.endswith("/comments"):
            number = int(path.split("/issues/")[1].split("/comments")[0])
            body = json.loads(request.content)["body"]
            cid = self._next_id
            self._next_id += 1
            self.comments[cid] = body
            self.posted_on[cid] = number
            return httpx.Response(201, json={"id": cid})
        if request.method == "PATCH" and "/issues/comments/" in path:
            cid = int(path.split("/issues/comments/")[1])
            self.comments[cid] = json.loads(request.content)["body"]
            return httpx.Response(200, json={"id": cid})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    def client(self) -> GitHubClient:
        transport = httpx.MockTransport(self.handler)
        http = httpx.Client(base_url="https://api.github.com", transport=transport)
        return GitHubClient(FakeAuth(), installation_id=INSTALLATION_ID, client=http)


# --- Booting the server against the fixtures --------------------------------


@pytest.fixture
def ctx(
    tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, GitHubFake, Path]]:
    """
    Boot the real app, wire it to the local remote + the GitHub fake, and seed
    one tracked repo. Yields (client, github_fake, remote).

    pr-1 and pr-2 both change shared.py, so the fake serves that file for both.
    """
    db_path = tmp_path / "lookout.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("REPO_CACHE_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    # No real App creds — we override _auth/client factory below instead.
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)

    fake = GitHubFake(files={1: ["shared.py"], 2: ["shared.py"], 3: ["shared.py"]})

    with TestClient(server.app) as c:
        # Pre-clone the local "remote" so fetch pulls our branches.
        server._manager.ensure_clone("acme", "widgets", str(remote))

        # Seed the tracked repo (the webhook handler ignores untracked repos —
        # repositories are created by App-installation events / the sweep).
        conn = connect(str(db_path))
        conn.execute(
            "INSERT INTO repositories (id, owner, name, installation_id, default_branch) "
            "VALUES (1, 'acme', 'widgets', ?, 'main')",
            (INSTALLATION_ID,),
        )
        conn.commit()
        conn.close()

        # Stand in for the GitHub App: a fake auth + a factory that returns the
        # fake-backed client, and a clone URL pointing at the local remote.
        server._auth = FakeAuth()
        monkeypatch.setattr(server, "app_client_factory", lambda auth: (lambda iid: fake.client()))
        monkeypatch.setattr(server, "_clone_url", lambda token, owner, name: str(remote))

        yield c, fake, remote


# --- Delivering signed webhook events ---------------------------------------


def _pr_payload(number: int, ref: str, sha: str, *, base: str = "main",
                merged: bool = False) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "head": {"sha": sha, "ref": ref},
        "base": {"ref": base},
        "user": {"login": "octocat"},
        "updated_at": "2026-06-16T00:00:00Z",
        "merged": merged,
    }


def _deliver(
    client: TestClient, action: str, pr: dict, *, secret: str = WEBHOOK_SECRET
) -> httpx.Response:
    payload = {
        "action": action,
        "pull_request": pr,
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
        "installation": {"id": INSTALLATION_ID},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        },
    )


def _open(client: TestClient, number: int, ref: str, remote: Path) -> httpx.Response:
    return _deliver(client, "opened", _pr_payload(number, ref, _sha(remote, ref)))


# --- Signature gate ---------------------------------------------------------


def test_bad_signature_is_rejected(ctx):
    client, _fake, remote = ctx
    resp = _deliver(
        client, "opened", _pr_payload(1, "pr-1", _sha(remote, "pr-1")),
        secret="wrong-secret",
    )
    assert resp.status_code == 401


def test_non_pull_request_event_is_accepted_and_ignored(ctx):
    client, fake, _remote = ctx
    body = json.dumps({"zen": "ping"}).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-Hub-Signature-256": sig},
    )
    assert resp.status_code == 202
    assert fake.comments == {}


# --- The build-plan validation list -----------------------------------------


def test_two_conflicting_prs_get_a_comment_on_both(ctx):
    client, fake, remote = ctx

    assert _open(client, 1, "pr-1", remote).status_code == 202
    # Second PR opening is the event that runs pr-2 against the overlapping pr-1.
    assert _open(client, 2, "pr-2", remote).status_code == 202

    # One comment on each PR.
    assert sorted(fake.posted_on.values()) == [1, 2]
    for body in fake.comments.values():
        assert "Potential conflict" in body


def test_pushing_to_resolve_updates_comments_to_resolved(ctx):
    client, fake, remote = ctx
    _open(client, 1, "pr-1", remote)
    _open(client, 2, "pr-2", remote)
    assert len(fake.comments) == 2

    # PR 1 pushes a commit that no longer touches line1 -> pair becomes clean.
    fake.files[1] = ["shared.py"]  # still overlaps the file
    resp = _deliver(
        client, "synchronize",
        _pr_payload(1, "pr-1-resolved", _sha(remote, "pr-1-resolved")),
    )
    assert resp.status_code == 202

    # No new comments — the two existing ones are edited to "resolved".
    assert len(fake.comments) == 2
    assert all("Resolved" in body for body in fake.comments.values())


def test_pushing_to_introduce_new_conflict_posts_a_new_comment(ctx):
    client, fake, remote = ctx
    # PR 1 starts on the resolved branch (no conflict with pr-2).
    _deliver(client, "opened",
             _pr_payload(1, "pr-1-resolved", _sha(remote, "pr-1-resolved")))
    _open(client, 2, "pr-2", remote)
    assert fake.comments == {}  # no conflict yet

    # PR 1 pushes pr-1 (rewrites line1) -> now conflicts with pr-2.
    resp = _deliver(client, "synchronize",
                    _pr_payload(1, "pr-1", _sha(remote, "pr-1")))
    assert resp.status_code == 202
    assert sorted(fake.posted_on.values()) == [1, 2]
    assert all("Potential conflict" in b for b in fake.comments.values())


def test_closing_a_pr_resolves_its_conflict_comments(ctx):
    client, fake, remote = ctx
    _open(client, 1, "pr-1", remote)
    _open(client, 2, "pr-2", remote)
    assert len(fake.comments) == 2

    # Close PR 1. Its comment on PR 2 must flip to resolved; the row is dropped.
    resp = _deliver(client, "closed",
                    _pr_payload(1, "pr-1", _sha(remote, "pr-1"), merged=True))
    assert resp.status_code == 202

    # The surviving PR 2's comment is resolved.
    pr2_comments = [b for cid, b in fake.comments.items() if fake.posted_on.get(cid) == 2]
    assert pr2_comments and all("Resolved" in b for b in pr2_comments)

    # PR 1 is gone from tracking and the conflict_checks row is cleaned up.
    conn = connect(str(remote.parent / "lookout.db"))
    try:
        repo = queries.get_repository(conn, "acme", "widgets")
        assert queries.get_pull_request(conn, repo["id"], 1) is None
        assert queries.conflict_checks_for_pr(conn, repo["id"], 1) == []
    finally:
        conn.close()


def test_repeated_push_same_sha_is_idempotent(ctx):
    client, fake, remote = ctx
    _open(client, 1, "pr-1", remote)
    _open(client, 2, "pr-2", remote)
    assert len(fake.comments) == 2

    # Re-deliver PR 2 at the identical SHA: SHA-skip should fire, no new comment.
    before = dict(fake.comments)
    resp = _deliver(client, "synchronize",
                    _pr_payload(2, "pr-2", _sha(remote, "pr-2")))
    assert resp.status_code == 202
    assert fake.comments == before  # unchanged: no duplicate, no edit
