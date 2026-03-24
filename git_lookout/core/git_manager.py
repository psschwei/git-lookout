from __future__ import annotations

import re
import subprocess
from pathlib import Path

from git_lookout.core.models import ConflictRegion, ConflictResult, PRInfo


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

    def merge_tree(
        self,
        owner: str,
        repo: str,
        pr_a: PRInfo,
        pr_b: PRInfo,
    ) -> ConflictResult:
        """
        Simulate merging pr_a and pr_b using git merge-tree.
        Returns a ConflictResult with conflicting files and regions.
        Operates entirely in memory — no working directory writes.
        """
        path = self.repo_path(owner, repo)

        # Use --name-only to get the list of conflicting files cleanly.
        # Non-zero exit = at least one conflict.
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--name-only",
             pr_a.head_sha, pr_b.head_sha],
            cwd=path,
            capture_output=True,
            text=True,
        )

        conflicting_files: list[str] = []
        conflict_regions: list[ConflictRegion] = []

        if result.returncode != 0:
            conflicting_files = _parse_conflicting_files(result.stdout)
            merged_tree_sha = result.stdout.splitlines()[0].strip()
            conflict_regions = _extract_conflict_regions(
                path, merged_tree_sha, conflicting_files
            )

        return ConflictResult(
            pr_a=pr_a,
            pr_b=pr_b,
            conflicting_files=conflicting_files,
            conflict_regions=conflict_regions,
        )


def file_overlap(files_a: list[str], files_b: list[str]) -> list[str]:
    """
    Return the intersection of two file lists.
    Used as a fast pre-filter before running merge-tree.
    """
    return list(set(files_a) & set(files_b))


# Matches "CONFLICT (content): Merge conflict in <file>" lines
_CONFLICT_LINE_RE = re.compile(r"^CONFLICT \([^)]+\): .+ in (.+)$", re.MULTILINE)


def _parse_conflicting_files(output: str) -> list[str]:
    """
    Parse conflicting file names from git merge-tree --name-only output.
    Conflict lines look like: "CONFLICT (content): Merge conflict in <file>"
    """
    return _CONFLICT_LINE_RE.findall(output)


def _extract_conflict_regions(
    repo_path: Path, merged_tree_sha: str, conflicting_files: list[str]
) -> list[ConflictRegion]:
    """
    Read each conflicting file from the merged tree (which contains inline
    conflict markers) and convert the marker positions to ConflictRegion objects.

    git merge-tree --write-tree leaves conflict markers in the merged blob:
        <<<<<<< <ours-sha>
        ... ours ...
        =======
        ... theirs ...
        >>>>>>> <theirs-sha>

    We scan the blob line-by-line to locate each hunk.
    """
    regions: list[ConflictRegion] = []

    for filename in conflicting_files:
        result = subprocess.run(
            ["git", "show", f"{merged_tree_sha}:{filename}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue

        ours_start: int | None = None
        separator: int | None = None

        for lineno, line in enumerate(result.stdout.splitlines(), start=1):
            if line.startswith("<<<<<<<"):
                ours_start = lineno
                separator = None
            elif line.startswith("=======") and ours_start is not None:
                separator = lineno
            elif line.startswith(">>>>>>>") and ours_start is not None and separator is not None:
                ours_end = separator - 1
                theirs_start = separator + 1
                theirs_end = lineno - 1
                regions.append(
                    ConflictRegion(
                        file=filename,
                        ours_start=ours_start,
                        ours_end=ours_end,
                        theirs_start=theirs_start,
                        theirs_end=theirs_end,
                    )
                )
                ours_start = None
                separator = None

    return regions
