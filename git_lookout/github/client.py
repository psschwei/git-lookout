from __future__ import annotations

from dataclasses import dataclass

import httpx

from git_lookout.core.models import PRInfo
from git_lookout.github.auth import GITHUB_API, AppAuth

# GitHub paginates list endpoints. 100 is the maximum page size.
_PAGE_SIZE = 100


@dataclass
class PullRequest:
    """
    A GitHub pull request, with the metadata the reconciliation sweep persists.

    This is the GitHub-adapter view of a PR. The platform-agnostic core only knows
    about :class:`PRInfo`; call :meth:`to_pr_info` to hand a PR to the core engine.
    """

    number: int
    title: str
    head_sha: str
    head_ref: str
    base_ref: str
    author: str
    updated_at: str

    def to_pr_info(self) -> PRInfo:
        return PRInfo(
            number=self.number,
            title=self.title,
            head_sha=self.head_sha,
            head_ref=self.head_ref,
        )


class GitHubClient:
    """
    Thin wrapper over the GitHub REST API, authenticated as a GitHub App
    installation.

    Each call resolves a fresh installation token via :class:`AppAuth` (which caches
    until near expiry) and sets it as the bearer credential. Only the two read
    operations Phase 2 needs are implemented: list open PRs, and list a PR's changed
    files. Comment posting (Phase 4) lives elsewhere.
    """

    def __init__(
        self,
        auth: AppAuth,
        installation_id: int,
        *,
        client: httpx.Client | None = None,
    ):
        self.auth = auth
        self.installation_id = installation_id
        self._client = client or httpx.Client(base_url=GITHUB_API, timeout=30.0)

    def _headers(self) -> dict[str, str]:
        token = self.auth.installation_token(self.installation_id)
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def list_open_prs(self, owner: str, repo: str) -> list[PullRequest]:
        """
        List all open pull requests in a repository, following pagination.

        Returns PRs in GitHub's default order (most recently created first). The
        caller filters by base branch — the sweep only tracks PRs targeting the
        default branch, but that decision belongs to the sweep, not the client.
        """
        prs: list[PullRequest] = []
        for page in self._paginate(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": "open"},
        ):
            prs.append(_parse_pull_request(page))
        return prs

    def changed_files(self, owner: str, repo: str, pr_number: int) -> list[str]:
        """Return the list of file paths changed by a PR, following pagination."""
        files: list[str] = []
        for entry in self._paginate(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
        ):
            files.append(entry["filename"])
        return files

    def get_pull_request(
        self, owner: str, repo: str, pr_number: int
    ) -> PullRequest:
        """Fetch a single pull request's metadata."""
        resp = self._client.get(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return _parse_pull_request(resp.json())

    def create_issue_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> int:
        """
        Post a comment on a PR and return its comment id.

        A PR comment is an *issue* comment in GitHub's data model (PRs are issues
        with code), so this hits the issues comments endpoint — the same id can
        later be updated via :meth:`update_issue_comment`. The id is stored in
        conflict_checks so subsequent checks update this comment in place rather
        than posting a duplicate.
        """
        resp = self._client.post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def update_issue_comment(
        self, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        """Edit an existing issue/PR comment in place by its id."""
        resp = self._client.patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
            headers=self._headers(),
        )
        resp.raise_for_status()

    def _paginate(self, path: str, params: dict | None = None):
        """
        Yield items across all pages of a list endpoint.

        Follows the RFC 5988 ``Link`` header's ``rel="next"`` rather than guessing
        page counts, so it stops exactly when GitHub says there is no next page.
        """
        params = {**(params or {}), "per_page": _PAGE_SIZE}
        url: str | None = path
        while url is not None:
            resp = self._client.get(url, params=params, headers=self._headers())
            resp.raise_for_status()
            yield from resp.json()
            url = _next_link(resp.headers.get("Link"))
            # The next URL already carries the query string; clear params so we
            # don't append per_page twice.
            params = None

    def close(self) -> None:
        self._client.close()


def _parse_pull_request(payload: dict) -> PullRequest:
    return PullRequest(
        number=payload["number"],
        title=payload["title"],
        head_sha=payload["head"]["sha"],
        head_ref=payload["head"]["ref"],
        base_ref=payload["base"]["ref"],
        author=(payload.get("user") or {}).get("login", ""),
        updated_at=payload["updated_at"],
    )


def _next_link(link_header: str | None) -> str | None:
    """
    Extract the ``rel="next"`` URL from a GitHub ``Link`` header, or None.

    Header form: '<https://api.github.com/...?page=2>; rel="next", <...>; rel="last"'
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        for rel in section[1:]:
            if rel.strip() == 'rel="next"':
                return url
    return None
