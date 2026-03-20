from git_lookout.core.models import (
    ConflictRegion,
    ConflictResult,
    MergeOrder,
    PRInfo,
)


def test_conflict_result_defaults():
    pr_a = PRInfo(number=1, title="PR A", head_sha="abc", head_ref="branch-a")
    pr_b = PRInfo(number=2, title="PR B", head_sha="def", head_ref="branch-b")
    result = ConflictResult(
        pr_a=pr_a,
        pr_b=pr_b,
        conflicting_files=["src/foo.py"],
        conflict_regions=[],
    )
    assert result.classification is None
    assert result.suggested_merge_order is None
    assert result.proposed_resolution is None
    assert result.confidence is None


def test_conflict_result_with_optional_fields():
    pr_a = PRInfo(number=1, title="PR A", head_sha="abc", head_ref="branch-a")
    pr_b = PRInfo(number=2, title="PR B", head_sha="def", head_ref="branch-b")
    region = ConflictRegion(file="src/foo.py", ours_start=10, ours_end=15, theirs_start=10, theirs_end=15)
    order = MergeOrder(merge_first=1, reason="smaller changeset")
    result = ConflictResult(
        pr_a=pr_a,
        pr_b=pr_b,
        conflicting_files=["src/foo.py"],
        conflict_regions=[region],
        classification="contradictory",
        suggested_merge_order=order,
        confidence=0.9,
    )
    assert result.classification == "contradictory"
    assert result.suggested_merge_order.merge_first == 1
    assert result.confidence == 0.9
