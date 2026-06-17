import subprocess
from pathlib import Path

import pytest

from git_lookout.core.git_manager import BareCloneManager


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """Create a local git repo to act as the 'remote'."""
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True
    )
    # Initial commit so there's something to clone
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> BareCloneManager:
    base = tmp_path / "bare_clones"
    base.mkdir()
    return BareCloneManager(base_path=base)


def test_repo_path(manager: BareCloneManager):
    path = manager.repo_path("acme", "widget")
    assert path == manager.base_path / "acme" / "widget.git"


def test_ensure_clone_creates_bare_clone(
    manager: BareCloneManager, source_repo: Path
):
    path = manager.ensure_clone("acme", "widget", str(source_repo))
    assert path.exists()
    # A bare clone has HEAD at the top level, not inside a .git subdir
    assert (path / "HEAD").exists()
    assert not (path / ".git").exists()


def test_ensure_clone_idempotent(
    manager: BareCloneManager, source_repo: Path
):
    path1 = manager.ensure_clone("acme", "widget", str(source_repo))
    path2 = manager.ensure_clone("acme", "widget", str(source_repo))
    assert path1 == path2


def test_fetch(manager: BareCloneManager, source_repo: Path):
    manager.ensure_clone("acme", "widget", str(source_repo))

    # Add a new commit to the source repo
    (source_repo / "new.txt").write_text("new content")
    subprocess.run(["git", "add", "."], cwd=source_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "second"], cwd=source_repo, check=True
    )

    manager.fetch("acme", "widget")

    # Verify the new commit is reachable in the bare clone
    bare = manager.repo_path("acme", "widget")
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=bare, capture_output=True, text=True
    )
    assert "second" in result.stdout


def test_fetch_ref(manager: BareCloneManager, source_repo: Path):
    manager.ensure_clone("acme", "widget", str(source_repo))

    # Create a new branch in the source repo
    subprocess.run(
        ["git", "checkout", "-b", "feature-x"], cwd=source_repo, check=True
    )
    (source_repo / "feature.txt").write_text("feature")
    subprocess.run(["git", "add", "."], cwd=source_repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feature commit"], cwd=source_repo, check=True
    )

    manager.fetch_ref("acme", "widget", "feature-x")

    # Verify the branch exists in the bare clone
    bare = manager.repo_path("acme", "widget")
    result = subprocess.run(
        ["git", "branch"], cwd=bare, capture_output=True, text=True
    )
    assert "feature-x" in result.stdout


def _branch_with_files(repo: Path, branch: str, files: dict[str, str]) -> None:
    """Create `branch` off the current HEAD, write `files`, and commit."""
    subprocess.run(["git", "checkout", "-b", branch], cwd=repo, check=True)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", f"{branch} commit"], cwd=repo, check=True)


def test_resolve_sha(manager: BareCloneManager, source_repo: Path):
    manager.ensure_clone("acme", "widget", str(source_repo))
    _branch_with_files(source_repo, "feature-x", {"feature.txt": "x"})
    manager.fetch_ref("acme", "widget", "feature-x")

    sha = manager.resolve_sha("acme", "widget", "feature-x")

    # 40-char hex SHA matching what the source repo reports for the branch.
    expected = subprocess.run(
        ["git", "rev-parse", "feature-x"],
        cwd=source_repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert sha == expected


def test_resolve_sha_unknown_ref_raises(
    manager: BareCloneManager, source_repo: Path
):
    manager.ensure_clone("acme", "widget", str(source_repo))
    with pytest.raises(subprocess.CalledProcessError):
        manager.resolve_sha("acme", "widget", "does-not-exist")


def test_changed_files_against_base(
    manager: BareCloneManager, source_repo: Path
):
    # Default branch already has README.md (from the source_repo fixture).
    base = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=source_repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    manager.ensure_clone("acme", "widget", str(source_repo))

    _branch_with_files(
        source_repo, "feature-x", {"a.txt": "a", "dir/b.txt": "b"}
    )
    manager.fetch_ref("acme", "widget", base)
    manager.fetch_ref("acme", "widget", "feature-x")

    files = manager.changed_files("acme", "widget", "feature-x", base)

    assert set(files) == {"a.txt", "dir/b.txt"}
