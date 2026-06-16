from __future__ import annotations

from typing import Protocol, runtime_checkable

from git_lookout.core.models import ConflictResult


@runtime_checkable
class Analyzer(Protocol):
    """
    An analyzer enriches a ConflictResult and returns it.

    Analyzers populate the optional fields on ConflictResult (classification,
    suggested_merge_order, proposed_resolution, confidence). They must not
    mutate the v1 detection fields (pr_a, pr_b, conflicting_files,
    conflict_regions). An analyzer may return the same instance it was given
    after mutating it, or a new instance — callers always use the return value.
    """

    def enrich(self, conflict: ConflictResult) -> ConflictResult:
        ...


# v1: no analyzers. Detection feeds directly into reporting.
#
# Future capability is added by appending analyzer instances here. No changes
# to detection, reporting, or the trigger/webhook layer are required.
#
#     ANALYZERS = [
#         ClassifyConflict(),
#         SuggestMergeOrder(),
#         GenerateResolution(),
#     ]
ANALYZERS: list[Analyzer] = []


def run_pipeline(
    conflict: ConflictResult,
    analyzers: list[Analyzer] | None = None,
) -> ConflictResult:
    """
    Pass a ConflictResult through the analysis pipeline.

    Each analyzer takes the result, populates its fields, and returns it. The
    output of one analyzer becomes the input of the next:

        detect → [analyzer 1] → [analyzer 2] → ... → report

    In v1 the default analyzer list is empty, so this is a pass-through: the
    conflict is returned unchanged. The loop is wired in so future analyzers
    plug in with no changes to callers.
    """
    if analyzers is None:
        analyzers = ANALYZERS

    for analyzer in analyzers:
        conflict = analyzer.enrich(conflict)
    return conflict
