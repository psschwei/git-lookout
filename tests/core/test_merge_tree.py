import subprocess
from pathlib import Path

import pytest

from git_lookout.core.git_manager import BareCloneManager
from git_lookout.core.models import PRInfo


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Create a local git repo with an initial commit on main."""
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, ["init", "-b", "main"])
    _git(repo, ["config", "user.email", "test@test.com"])
    _git(repo, ["config", "user.name", "Test"])
    (repo / "shared.py").write_text("def foo():\n    pass\n")
    _git(repo, ["add", "."])
    _git(repo, ["commit", "-m", "init"])
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> BareCloneManager:
    base = tmp_path / "bare_clones"
    base.mkdir()
    return BareCloneManager(base_path=base)


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True)


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_merge_tree_no_conflict(
    manager: BareCloneManager, source_repo: Path, tmp_path: Path
):
    manager.ensure_clone("org", "repo", str(source_repo))

    # Branch A: add file_a.py
    _git(source_repo, ["checkout", "-b", "branch-a"])
    (source_repo / "file_a.py").write_text("x = 1\n")
    _git(source_repo, ["add", "."])
    _git(source_repo, ["commit", "-m", "add file_a"])
    sha_a = _sha(source_repo, "HEAD")
    _git(source_repo, ["checkout", "main"])

    # Branch B: add file_b.py
    _git(source_repo, ["checkout", "-b", "branch-b"])
    (source_repo / "file_b.py").write_text("y = 2\n")
    _git(source_repo, ["add", "."])
    _git(source_repo, ["commit", "-m", "add file_b"])
    sha_b = _sha(source_repo, "HEAD")
    _git(source_repo, ["checkout", "main"])

    manager.fetch(owner="org", repo="repo")

    pr_a = PRInfo(number=1, title="Add file_a", head_sha=sha_a, head_ref="branch-a")
    pr_b = PRInfo(number=2, title="Add file_b", head_sha=sha_b, head_ref="branch-b")

    result = manager.merge_tree("org", "repo", pr_a, pr_b)

    assert result.conflicting_files == []
    assert result.conflict_regions == []


def test_merge_tree_with_conflict(
    manager: BareCloneManager, source_repo: Path, tmp_path: Path
):
    manager.ensure_clone("org", "repo", str(source_repo))
    base_sha = _sha(source_repo, "main")

    # Branch A: modify shared.py one way
    _git(source_repo, ["checkout", "-b", "branch-a"])
    (source_repo / "shared.py").write_text("def foo():\n    return 'a'\n")
    _git(source_repo, ["add", "."])
    _git(source_repo, ["commit", "-m", "branch-a change"])
    sha_a = _sha(source_repo, "HEAD")

    _git(source_repo, ["checkout", "main"])

    # Branch B: modify shared.py a different way
    _git(source_repo, ["checkout", "-b", "branch-b"])
    (source_repo / "shared.py").write_text("def foo():\n    return 'b'\n")
    _git(source_repo, ["add", "."])
    _git(source_repo, ["commit", "-m", "branch-b change"])
    sha_b = _sha(source_repo, "HEAD")

    _git(source_repo, ["checkout", "main"])
    manager.fetch(owner="org", repo="repo")

    pr_a = PRInfo(number=1, title="Branch A", head_sha=sha_a, head_ref="branch-a")
    pr_b = PRInfo(number=2, title="Branch B", head_sha=sha_b, head_ref="branch-b")

    result = manager.merge_tree("org", "repo", pr_a, pr_b)

    assert "shared.py" in result.conflicting_files
