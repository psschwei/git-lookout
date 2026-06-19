from __future__ import annotations

import json

import httpx
import pytest

from git_lookout.github.client import GitHubClient, PullRequest, _next_link


class FakeAuth:
    """Stands in for AppAuth — returns a fixed token without any network."""

    def __init__(self, token: str = "ghs_test"):
        self.token = token
        self.calls = 0

    def installation_token(self, installation_id: int) -> str:
        self.calls += 1
        return self.token


def _client(handler) -> GitHubClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url="https://api.github.com", transport=transport)
    return GitHubClient(FakeAuth(), installation_id=99, client=http)


def _pr_payload(number: int, **overrides) -> dict:
    payload = {
        "number": number,
        "title": f"PR {number}",
        "head": {"sha": f"sha{number}", "ref": f"feature-{number}"},
        "base": {"ref": "main"},
        "user": {"login": "octocat"},
        "updated_at": "2026-06-16T00:00:00Z",
    }
    payload.update(overrides)
    return payload


# ---- to_pr_info -----------------------------------------------------------


def test_pull_request_projects_to_core_pr_info():
    pr = PullRequest(
        number=7,
        title="Title",
        head_sha="abc",
        head_ref="branch",
        base_ref="main",
        author="octocat",
        updated_at="2026-06-16T00:00:00Z",
    )
    info = pr.to_pr_info()
    assert (info.number, info.title, info.head_sha, info.head_ref) == (
        7,
        "Title",
        "abc",
        "branch",
    )


# ---- list_open_prs --------------------------------------------------------


def test_list_open_prs_parses_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "open"
        assert request.headers["Authorization"] == "Bearer ghs_test"
        return httpx.Response(200, json=[_pr_payload(1), _pr_payload(2)])

    prs = _client(handler).list_open_prs("acme", "widgets")
    assert [p.number for p in prs] == [1, 2]
    first = prs[0]
    assert first.head_sha == "sha1"
    assert first.head_ref == "feature-1"
    assert first.base_ref == "main"
    assert first.author == "octocat"


def test_list_open_prs_follows_pagination():
    page2 = "https://api.github.com/repos/acme/widgets/pulls?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[_pr_payload(3)])
        return httpx.Response(
            200,
            json=[_pr_payload(1), _pr_payload(2)],
            headers={"Link": f'<{page2}>; rel="next"'},
        )

    prs = _client(handler).list_open_prs("acme", "widgets")
    assert [p.number for p in prs] == [1, 2, 3]


def test_list_open_prs_missing_user_yields_empty_author():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_pr_payload(1, user=None)])

    prs = _client(handler).list_open_prs("acme", "widgets")
    assert prs[0].author == ""


# ---- changed_files --------------------------------------------------------


def test_changed_files_returns_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/pulls/42/files"
        return httpx.Response(
            200,
            json=[
                {"filename": "src/api/orders.ts"},
                {"filename": "src/util/log.ts"},
            ],
        )

    files = _client(handler).changed_files("acme", "widgets", 42)
    assert files == ["src/api/orders.ts", "src/util/log.ts"]


def test_changed_files_follows_pagination():
    page2 = "https://api.github.com/repos/acme/widgets/pulls/42/files?page=2"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"filename": "b.py"}])
        return httpx.Response(
            200,
            json=[{"filename": "a.py"}],
            headers={"Link": f'<{page2}>; rel="next"'},
        )

    files = _client(handler).changed_files("acme", "widgets", 42)
    assert files == ["a.py", "b.py"]


def test_error_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).list_open_prs("acme", "widgets")


# ---- get_pull_request -----------------------------------------------------


def test_get_pull_request_parses_single_pr():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/pulls/42"
        return httpx.Response(200, json=_pr_payload(42))

    pr = _client(handler).get_pull_request("acme", "widgets", 42)
    assert pr.number == 42
    assert pr.base_ref == "main"


# ---- create_issue_comment / update_issue_comment -------------------------


def test_create_issue_comment_posts_and_returns_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)["body"]
        return httpx.Response(201, json={"id": 12345})

    comment_id = _client(handler).create_issue_comment(
        "acme", "widgets", 42, "hello"
    )
    assert comment_id == 12345
    assert seen["method"] == "POST"
    assert seen["path"] == "/repos/acme/widgets/issues/42/comments"
    assert seen["body"] == "hello"


def test_update_issue_comment_patches_existing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)["body"]
        return httpx.Response(200, json={"id": 12345})

    _client(handler).update_issue_comment("acme", "widgets", 12345, "edited")
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/repos/acme/widgets/issues/comments/12345"
    assert seen["body"] == "edited"


def test_create_issue_comment_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        _client(handler).create_issue_comment("acme", "widgets", 42, "x")


# ---- _next_link -----------------------------------------------------------


def test_next_link_extracts_next_url():
    header = (
        '<https://api.github.com/x?page=2>; rel="next", '
        '<https://api.github.com/x?page=5>; rel="last"'
    )
    assert _next_link(header) == "https://api.github.com/x?page=2"


def test_next_link_returns_none_without_next():
    header = '<https://api.github.com/x?page=5>; rel="last"'
    assert _next_link(header) is None


def test_next_link_handles_missing_header():
    assert _next_link(None) is None
    assert _next_link("") is None
