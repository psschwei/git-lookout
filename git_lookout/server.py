from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from git_lookout.core.git_manager import BareCloneManager
from git_lookout.github.auth import AppAuth
from git_lookout.storage import schema
from git_lookout.sweep import app_client_factory, reconcile_all

log = logging.getLogger(__name__)

app = FastAPI(title="git-lookout", version="0.1.0")

# Initialized at startup from environment
_manager: BareCloneManager | None = None
_db = None
_auth: AppAuth | None = None
_sweep_task: asyncio.Task | None = None

# Reconciliation sweep cadence. The spec recommends every 5-15 minutes; 5 is the
# default and tightens data freshness against missed webhooks.
_DEFAULT_SWEEP_INTERVAL_SECONDS = 300


@app.on_event("startup")
async def startup() -> None:
    global _manager, _db, _auth, _sweep_task

    repo_dir = os.environ.get("REPO_CACHE_DIR", "/tmp/git-lookout/repos")
    _manager = BareCloneManager(base_path=Path(repo_dir))

    db_path = os.environ.get("DATABASE_PATH", "/tmp/git-lookout/git-lookout.db")
    _db = schema.connect(db_path)

    _auth = _build_auth()
    if _auth is None:
        # Without GitHub App credentials there is nothing to sweep. The server
        # still serves /health, which keeps local runs and tests usable.
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
    if _db is not None:
        _db.close()


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
            result = await asyncio.to_thread(reconcile_all, _db, factory)
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
