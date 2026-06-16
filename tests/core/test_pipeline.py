"""
Tests for the analysis pipeline loop.

The v1 pipeline is empty — detection feeds directly into reporting. These tests
pin down that pass-through behavior and verify the loop correctly chains
analyzers when they are present (the future case).
"""

from __future__ import annotations

from git_lookout.core.models import ConflictResult, MergeOrder, PRInfo
from git_lookout.core.pipeline import ANALYZERS, Analyzer, run_pipeline


def _result() -> ConflictResult:
    return ConflictResult(
        pr_a=PRInfo(number=1, title="A", head_sha="aaa", head_ref="pr-a"),
        pr_b=PRInfo(number=2, title="B", head_sha="bbb", head_ref="pr-b"),
        conflicting_files=["shared.py"],
        conflict_regions=[],
    )


class TestEmptyPipeline:
    """v1: no analyzers -> pass-through."""

    def test_default_analyzer_list_is_empty(self):
        assert ANALYZERS == []

    def test_returns_same_instance_unchanged(self):
        conflict = _result()
        out = run_pipeline(conflict)
        assert out is conflict

    def test_optional_fields_stay_none(self):
        out = run_pipeline(_result())
        assert out.classification is None
        assert out.suggested_merge_order is None
        assert out.proposed_resolution is None
        assert out.confidence is None

    def test_explicit_empty_list_is_pass_through(self):
        conflict = _result()
        assert run_pipeline(conflict, analyzers=[]) is conflict


class TestPipelineWithAnalyzers:
    """Future: analyzers are chained in order, each enriching the result."""

    def test_single_analyzer_enriches_result(self):
        class Classify:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                conflict.classification = "contradictory"
                return conflict

        out = run_pipeline(_result(), analyzers=[Classify()])
        assert out.classification == "contradictory"

    def test_analyzers_run_in_order(self):
        calls: list[str] = []

        class First:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                calls.append("first")
                conflict.classification = "complementary"
                return conflict

        class Second:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                calls.append("second")
                # Sees the output of First.
                assert conflict.classification == "complementary"
                conflict.suggested_merge_order = MergeOrder(
                    merge_first=1, reason="smaller"
                )
                return conflict

        out = run_pipeline(_result(), analyzers=[First(), Second()])
        assert calls == ["first", "second"]
        assert out.classification == "complementary"
        assert out.suggested_merge_order == MergeOrder(merge_first=1, reason="smaller")

    def test_output_of_one_feeds_next_when_new_instance_returned(self):
        """An analyzer may return a fresh instance; the next sees it."""
        replacement = _result()
        replacement.confidence = 0.9

        class Replace:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                return replacement

        class Observe:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                assert conflict is replacement
                return conflict

        out = run_pipeline(_result(), analyzers=[Replace(), Observe()])
        assert out is replacement
        assert out.confidence == 0.9

    def test_analyzer_protocol_is_runtime_checkable(self):
        class Valid:
            def enrich(self, conflict: ConflictResult) -> ConflictResult:
                return conflict

        class Invalid:
            pass

        assert isinstance(Valid(), Analyzer)
        assert not isinstance(Invalid(), Analyzer)
