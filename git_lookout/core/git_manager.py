import subprocess
from pathlib import Path


class BareCloneManager:
    def __init__(self, base_path: Path):
        """base_path is the root for all bare clones (e.g., /data/repos)."""
        self.base_path = base_path

    def repo_path(self, owner: str, repo: str) -> Path:
        """Returns {base_path}/{owner}/{repo}.git"""
        return self.base_path / owner / f"{repo}.git"

    def ensure_clone(self, owner: str, repo: str, url: str) -> Path:
        """Clone if missing, return path. Idempotent."""
        path = self.repo_path(owner, repo)
        if not path.exists():
            subprocess.run(
                ["git", "clone", "--bare", url, str(path)], check=True
            )
        return path

    def fetch(self, owner: str, repo: str) -> None:
        """Fetch all refs from origin."""
        path = self.repo_path(owner, repo)
        subprocess.run(
            ["git", "fetch", "origin", "+refs/heads/*:refs/heads/*"],
            cwd=path,
            check=True,
        )

    def fetch_ref(self, owner: str, repo: str, ref: str) -> None:
        """Fetch a specific ref (branch name) from origin."""
        path = self.repo_path(owner, repo)
        subprocess.run(
            ["git", "fetch", "origin", f"+refs/heads/{ref}:refs/heads/{ref}"],
            cwd=path,
            check=True,
        )
