from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from git_lookout.core.git_manager import BareCloneManager

app = FastAPI(title="git-lookout", version="0.1.0")

# Initialized at startup from environment
_manager: BareCloneManager | None = None


@app.on_event("startup")
async def startup() -> None:
    global _manager
    from pathlib import Path

    repo_dir = os.environ.get("REPO_CACHE_DIR", "/tmp/git-lookout/repos")
    _manager = BareCloneManager(base_path=Path(repo_dir))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("git_lookout.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
