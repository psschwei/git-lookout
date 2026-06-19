from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from git_lookout import handlers
from git_lookout.core.git_manager import BareCloneManager
from git_lookout.core.models import ConflictResult, PRInfo
from git_lookout.core.pipeline import run_pipeline
from git_lookout.github import auth as gh_auth
from git_lookout.github.auth import GITHUB_API, AppAuth
from git_lookout.storage import queries, schema
from git_lookout.sweep import app_client_factory, reconcile_all
from git_lookout.webhook.events import PullRequestEvent, parse_pull_request_event
from git_lookout.webhook.signature import verify_signature

log = logging.getLogger(__name__)

app = FastAPI(title="git-lookout", version="0.1.0")

# Initialized at startup from environment
_manager: BareCloneManager | None = None
# The database path, not a live connection: SQLite connections are bound to the
# thread that opened them, and request handling / the sweep run in worker threads
# (asyncio.to_thread). Each worker opens its own short-lived connection from this
# path via schema.connect (idempotent), which keeps thread affinity correct
# without a shared-connection lock.
_db_path: str | None = None
_auth: AppAuth | None = None
# The GitHub App webhook secret, used to verify X-Hub-Signature-256 on each
# delivery. None disables the webhook endpoint (returns 503) — there is no safe
# way to accept unsigned deliveries.
_webhook_secret: str | None = None
_sweep_task: asyncio.Task | None = None
# A plain httpx client used only to validate caller-supplied GitHub tokens. It is
# independent of the GitHub App credentials, so /api/check auth works even when
# the App (and thus the sweep) is not configured.
_validator_client: httpx.Client | None = None

# Reconciliation sweep cadence. The spec recommends every 5-15 minutes; 5 is the
# default and tightens data freshness against missed webhooks.
_DEFAULT_SWEEP_INTERVAL_SECONDS = 300


# --- Request / response models ----------------------------------------------


class CheckRequest(BaseModel):
    repo: str  # "owner/repo"
    ref: str   # branch name, already pushed to the remote


class ConflictRegionOut(BaseModel):
    file: str
    ours_start: int
    ours_end: int
    theirs_start: int
    theirs_end: int


class ConflictOut(BaseModel):
    pr_number: int
    title: str
    conflicting_files: list[str]
    conflict_regions: list[ConflictRegionOut]


class CheckResponse(BaseModel):
    conflicts: list[ConflictOut]


@app.on_event("startup")
async def startup() -> None:
    global _manager, _db_path, _auth, _webhook_secret, _sweep_task, _validator_client

    repo_dir = os.environ.get("REPO_CACHE_DIR", "/tmp/git-lookout/repos")
    _manager = BareCloneManager(base_path=Path(repo_dir))

    _db_path = os.environ.get("DATABASE_PATH", "/tmp/git-lookout/git-lookout.db")
    # Apply the schema once at startup (idempotent); request/sweep threads open
    # their own connections to this path.
    schema.connect(_db_path).close()

    _validator_client = httpx.Client(base_url=GITHUB_API, timeout=30.0)

    _webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET") or None
    if _webhook_secret is None:
        log.warning("GITHUB_WEBHOOK_SECRET not set; /webhook will reject deliveries")

    _auth = _build_auth()
    if _auth is None:
        # Without GitHub App credentials there is nothing to sweep. The server
        # still serves /health and /api/check (caller-token auth is independent
        # of the App), which keeps local runs and tests usable.
        log.warning("GitHub App credentials not configured; reconciliation sweep disabled")
        return

    interval = int(
        os.environ.get("SWEEP_INTERVAL_SECONDS", _DEFAULT_SWEEP_INTERVAL_SECONDS)
    )
    _sweep_task = asyncio.create_task(_sweep_loop(interval))


@app.on_event("shutdown")
async def shutdown() -> None:
    if _sweep_task is not None:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except asyncio.CancelledError:
            pass
    if _auth is not None:
        _auth.close()
    if _validator_client is not None:
        _validator_client.close()


def _build_auth() -> AppAuth | None:
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if not app_id or not private_key:
        return None
    return AppAuth(app_id, private_key)


async def _sweep_loop(interval: int) -> None:
    """
    Run the reconciliation sweep immediately on startup, then every ``interval``
    seconds. Each sweep runs in a worker thread (it is synchronous, blocking I/O
    and SQLite) so the event loop stays responsive. Per-sweep errors are logged
    and the loop continues — a failed sweep must not kill the timer.
    """
    factory = app_client_factory(_auth)
    while True:
        try:
            result = await asyncio.to_thread(_sweep_once, factory)
            log.info(
                "sweep: +%d ~%d -%d =%d",
                result.added,
                result.updated,
                result.removed,
                result.unchanged,
            )
        except Exception:
            log.exception("reconciliation sweep failed")
        await asyncio.sleep(interval)


def _sweep_once(factory) -> object:
    """Run one reconciliation sweep on a connection owned by this worker thread."""
    conn = schema.connect(_db_path)
    try:
        return reconcile_all(conn, factory)
    finally:
        conn.close()


def _parse_repo(repo: str) -> tuple[str, str]:
    """Split an 'owner/name' string, rejecting anything that isn't exactly that."""
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(status_code=400, detail="repo must be 'owner/name'")
    return parts[0], parts[1]


async def require_repo_access(
    body: CheckRequest,
    authorization: str | None = Header(default=None),
) -> tuple[str, str]:
    """
    Authorize the caller for ``body.repo`` and return its (owner, name).

    Auth is by caller-supplied GitHub token: ``Authorization: Bearer <token>``.
    A missing/blank token is a 401; a token that cannot read the repo is a 403.
    Validation hits the GitHub API, so it runs in a worker thread to keep the
    event loop free.
    """
    owner, name = _parse_repo(body.repo)

    token = _bearer_token(authorization)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    allowed = await asyncio.to_thread(
        gh_auth.repo_access, token, owner, name, client=_validator_client
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="token lacks access to repo")

    return owner, name


def _bearer_token(authorization: str | None) -> str | None:
    """Extract a non-empty bearer token from an Authorization header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@app.post("/api/check", response_model=CheckResponse)
async def check(
    body: CheckRequest,
    owner_name: tuple[str, str] = Depends(require_repo_access),
) -> CheckResponse:
    """
    Check a pushed branch for conflicts against the repo's open PRs.

    The branch must already exist on the remote. We fetch it into the bare
    clone, diff it against the default branch, pre-filter open PRs by file
    overlap, and run merge-tree against each remaining candidate. Only PRs that
    actually conflict are returned.
    """
    owner, name = owner_name
    conflicts = await asyncio.to_thread(_run_check, owner, name, body.ref)
    return CheckResponse(conflicts=conflicts)


def _run_check(owner: str, name: str, ref: str) -> list[ConflictOut]:
    """
    Synchronous detection flow — runs in a worker thread off the event loop.

    Opens its own SQLite connection (connections are thread-bound) for the
    repo lookup and overlap pre-filter, then closes it before the git work.
    """
    conn = schema.connect(_db_path)
    try:
        repo = queries.get_repository(conn, owner, name)
        if repo is None:
            raise HTTPException(status_code=404, detail="repository not tracked")
        repo_id = repo["id"]
        default_branch = repo["default_branch"]

        try:
            _manager.fetch_ref(owner, name, ref)
            head_sha = _manager.resolve_sha(owner, name, ref)
            files = _manager.changed_files(owner, name, ref, default_branch)
        except subprocess.CalledProcessError as exc:
            # An unknown ref (or unfetchable branch) is a client error, not a 500.
            raise HTTPException(
                status_code=404, detail=f"ref not found: {ref}"
            ) from exc

        overlapping = queries.prs_overlapping_files(conn, repo_id, files)
    finally:
        conn.close()

    candidate = PRInfo(number=0, title=ref, head_sha=head_sha, head_ref=ref)

    conflicts: list[ConflictOut] = []
    for pr in overlapping:
        pr_info = PRInfo(
            number=pr["pr_number"],
            title=pr["title"] or "",
            head_sha=pr["head_sha"],
            head_ref="",
        )
        result = _manager.merge_tree(owner, name, candidate, pr_info)
        result = run_pipeline(result)
        if result.conflicting_files:
            conflicts.append(_to_conflict_out(pr_info, result))

    return conflicts


def _to_conflict_out(pr: PRInfo, result: ConflictResult) -> ConflictOut:
    return ConflictOut(
        pr_number=pr.number,
        title=pr.title,
        conflicting_files=result.conflicting_files,
        conflict_regions=[
            ConflictRegionOut(
                file=r.file,
                ours_start=r.ours_start,
                ours_end=r.ours_end,
                theirs_start=r.theirs_start,
                theirs_end=r.theirs_end,
            )
            for r in result.conflict_regions
        ],
    )


@app.post("/webhook")
async def webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    """
    Receive a GitHub App webhook delivery (the passive monitoring path).

    Verifies the HMAC signature against ``GITHUB_WEBHOOK_SECRET``, then — only for
    ``pull_request`` events — parses the payload and reconciles the triggering PR
    against its overlapping open PRs, posting/updating/resolving conflict comments.
    All event-handling I/O (git, SQLite, GitHub API) runs in a worker thread.

    Status codes:
      503 — webhook secret not configured (can't verify anything)
      401 — missing or invalid signature
      400 — malformed pull_request payload
      202 — handled (or accepted-and-ignored for non-actionable events)
    """
    if _webhook_secret is None:
        raise HTTPException(status_code=503, detail="webhook secret not configured")

    body = await request.body()
    if not verify_signature(_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Non-PR events (ping, installation, push, …) are accepted and ignored — the
    # signature was valid, there is just nothing for us to do.
    if x_github_event != "pull_request":
        return Response(status_code=202)

    try:
        payload = await request.json()
    except Exception as exc:  # malformed JSON body
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    event = parse_pull_request_event(payload)
    if event is None:
        raise HTTPException(status_code=400, detail="invalid pull_request payload")

    if _auth is None:
        # Without App credentials we can't fetch the clone or post comments. The
        # sweep is already disabled in this mode; surface it rather than 500.
        raise HTTPException(
            status_code=503, detail="GitHub App credentials not configured"
        )

    await asyncio.to_thread(_handle_webhook_event, event)
    return Response(status_code=202)


def _handle_webhook_event(event: PullRequestEvent) -> None:
    """
    Handle one pull_request event on a connection owned by this worker thread.

    Builds an installation-scoped GitHub client and the matching authenticated
    clone URL, then hands off to the platform-agnostic handler. SQLite
    connections are thread-bound, so this opens (and closes) its own.
    """
    client = app_client_factory(_auth)(event.installation_id)
    token = _auth.installation_token(event.installation_id)
    clone_url = _clone_url(token, event.repo_owner, event.repo_name)
    now = datetime.now(timezone.utc).isoformat()

    conn = schema.connect(_db_path)
    try:
        handlers.handle_pull_request_event(
            conn, _manager, client, event, clone_url=clone_url, now=now
        )
    finally:
        conn.close()


def _clone_url(token: str, owner: str, name: str) -> str:
    """
    Build an installation-authenticated HTTPS clone URL.

    GitHub accepts an installation token as the password with the literal
    username ``x-access-token`` for git-over-HTTPS, which is how the bare clone
    fetches private repos without storing long-lived credentials on disk.
    """
    return f"https://x-access-token:{token}@github.com/{owner}/{name}.git"


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("git_lookout.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
