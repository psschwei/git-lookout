"""
Phase 1 integration tests.

These tests exercise the full core-engine workflow end-to-end:
  1. Create a local source repo with a conflict scenario.
  2. Bare-clone it via BareCloneManager.
  3. Use file_overlap as the pre-filter.
  4. Run merge_tree and assert on the full ConflictResult.

The fixture helpers are deliberately minimal – we want a realistic round-trip,
not mocked internals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_lookout.core.git_manager import BareCloneManager, file_overlap
from git_lookout.core.models import ConflictResult, PRInfo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _sha(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    """
    A source repo (acts as the 'remote') with an initial commit on main.
    shared.py is the file that both PR branches will touch.
    """
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, ["init", "-b", "main"])
    _git(repo, ["config", "user.email", "test@test.com"])
    _git(repo, ["config", "user.name", "Test"])

    # shared.py: a multi-line file so the conflict regions have real line numbers
    (repo / "shared.py").write_text(
        "# shared module\n"
        "\n"
        "def compute(x):\n"
        "    return x\n"
        "\n"
        "def helper():\n"
        "    pass\n"
    )
    (repo / "other.py").write_text("# unrelated\n")
    _git(repo, ["add", "."])
    _git(repo, ["commit", "-m", "init"])
    return repo


@pytest.fixture
def manager(tmp_path: Path) -> BareCloneManager:
    base = tmp_path / "bare_clones"
    base.mkdir()
    return BareCloneManager(base_path=base)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

class TestConflictingBranches:
    """Two PRs that both modify the same lines in shared.py."""

    @pytest.fixture(autouse=True)
    def setup(self, manager: BareCloneManager, source_repo: Path):
        # Clone before any branches exist so the initial state is captured
        manager.ensure_clone("org", "repo", str(source_repo))

        # Branch A: change compute() to return x * 2
        _git(source_repo, ["checkout", "-b", "pr-a"])
        (source_repo / "shared.py").write_text(
            "# shared module\n"
            "\n"
            "def compute(x):\n"
            "    return x * 2\n"
            "\n"
            "def helper():\n"
            "    pass\n"
        )
        _git(source_repo, ["add", "."])
        _git(source_repo, ["commit", "-m", "pr-a: double x"])
        self.sha_a = _sha(source_repo, "HEAD")
        _git(source_repo, ["checkout", "main"])

        # Branch B: change compute() to return x + 1
        _git(source_repo, ["checkout", "-b", "pr-b"])
        (source_repo / "shared.py").write_text(
            "# shared module\n"
            "\n"
            "def compute(x):\n"
            "    return x + 1\n"
            "\n"
            "def helper():\n"
            "    pass\n"
        )
        _git(source_repo, ["add", "."])
        _git(source_repo, ["commit", "-m", "pr-b: increment x"])
        self.sha_b = _sha(source_repo, "HEAD")
        _git(source_repo, ["checkout", "main"])

        manager.fetch(owner="org", repo="repo")
        self.manager = manager
        self.pr_a = PRInfo(number=1, title="PR A", head_sha=self.sha_a, head_ref="pr-a")
        self.pr_b = PRInfo(number=2, title="PR B", head_sha=self.sha_b, head_ref="pr-b")

    def test_file_overlap_pre_filter_detects_shared_file(self):
        """file_overlap should flag shared.py before we even run merge_tree."""
        files_a = ["shared.py"]
        files_b = ["shared.py", "other.py"]
        overlap = file_overlap(files_a, files_b)
        assert "shared.py" in overlap

    def test_conflict_detected(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert isinstance(result, ConflictResult)
        assert len(result.conflicting_files) > 0, "Expected at least one conflicting file"

    def test_conflicting_file_identified(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert "shared.py" in result.conflicting_files

    def test_conflict_regions_have_line_numbers(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        regions_in_shared = [r for r in result.conflict_regions if r.file == "shared.py"]
        assert len(regions_in_shared) > 0, "Expected conflict regions for shared.py"
        for region in regions_in_shared:
            assert region.ours_start > 0, "ours_start should be a positive line number"
            assert region.theirs_start > 0, "theirs_start should be a positive line number"

    def test_result_preserves_pr_info(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert result.pr_a == self.pr_a
        assert result.pr_b == self.pr_b

    def test_optional_fields_are_none(self):
        """Fields reserved for future analyzers must not be set by the core engine."""
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert result.classification is None
        assert result.suggested_merge_order is None
        assert result.proposed_resolution is None
        assert result.confidence is None


class TestNonConflictingBranches:
    """Two PRs that touch completely different files – no conflict expected."""

    @pytest.fixture(autouse=True)
    def setup(self, manager: BareCloneManager, source_repo: Path):
        manager.ensure_clone("org", "repo", str(source_repo))

        # Branch A: add a new file
        _git(source_repo, ["checkout", "-b", "pr-a"])
        (source_repo / "module_a.py").write_text("A = 1\n")
        _git(source_repo, ["add", "."])
        _git(source_repo, ["commit", "-m", "pr-a: add module_a"])
        self.sha_a = _sha(source_repo, "HEAD")
        _git(source_repo, ["checkout", "main"])

        # Branch B: add a different new file
        _git(source_repo, ["checkout", "-b", "pr-b"])
        (source_repo / "module_b.py").write_text("B = 2\n")
        _git(source_repo, ["add", "."])
        _git(source_repo, ["commit", "-m", "pr-b: add module_b"])
        self.sha_b = _sha(source_repo, "HEAD")
        _git(source_repo, ["checkout", "main"])

        manager.fetch(owner="org", repo="repo")
        self.manager = manager
        self.pr_a = PRInfo(number=3, title="PR A", head_sha=self.sha_a, head_ref="pr-a")
        self.pr_b = PRInfo(number=4, title="PR B", head_sha=self.sha_b, head_ref="pr-b")

    def test_file_overlap_pre_filter_returns_empty(self):
        """No common files, so the pre-filter should short-circuit to empty."""
        overlap = file_overlap(["module_a.py"], ["module_b.py"])
        assert overlap == []

    def test_no_conflict_detected(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert result.conflicting_files == []

    def test_no_conflict_regions(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert result.conflict_regions == []

    def test_result_preserves_pr_info(self):
        result = self.manager.merge_tree("org", "repo", self.pr_a, self.pr_b)
        assert result.pr_a == self.pr_a
        assert result.pr_b == self.pr_b
