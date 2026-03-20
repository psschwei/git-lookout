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
            # Get conflict regions by re-running without --name-only
            detail = subprocess.run(
                ["git", "merge-tree", "--write-tree",
                 pr_a.head_sha, pr_b.head_sha],
                cwd=path,
                capture_output=True,
                text=True,
            )
            conflict_regions = _parse_conflict_regions(detail.stdout, conflicting_files)

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

# Matches hunk headers like: @@ -10,7 +10,7 @@
_HUNK_RE = re.compile(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", re.MULTILINE)


def _parse_conflicting_files(output: str) -> list[str]:
    """
    Parse conflicting file names from git merge-tree --name-only output.
    Conflict lines look like: "CONFLICT (content): Merge conflict in <file>"
    """
    return _CONFLICT_LINE_RE.findall(output)


def _parse_conflict_regions(output: str, conflicting_files: list[str]) -> list[ConflictRegion]:
    """
    Parse conflict hunk positions from full git merge-tree output.
    Sections are split by "diff --cc <file>" headers.
    """
    conflict_regions: list[ConflictRegion] = []
    sections = re.split(r"^diff --(?:cc|git a/\S+ b/) ", output, flags=re.MULTILINE)

    for section in sections[1:]:
        lines = section.splitlines()
        if not lines:
            continue

        filename = lines[0].strip()
        if filename not in conflicting_files:
            continue

        for match in _HUNK_RE.finditer(section):
            ours_start = int(match.group(1))
            theirs_start = int(match.group(2))
            conflict_regions.append(
                ConflictRegion(
                    file=filename,
                    ours_start=ours_start,
                    ours_end=ours_start,
                    theirs_start=theirs_start,
                    theirs_end=theirs_start,
                )
            )

    return conflict_regions
