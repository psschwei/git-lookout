from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PRInfo:
    number: int
    title: str
    head_sha: str
    head_ref: str


@dataclass
class ConflictRegion:
    file: str
    ours_start: int
    ours_end: int
    theirs_start: int
    theirs_end: int


@dataclass
class MergeOrder:
    merge_first: int   # PR number that should merge first
    reason: str


@dataclass
class ConflictResult:
    # v1 — always present
    pr_a: PRInfo
    pr_b: PRInfo
    conflicting_files: list[str]
    conflict_regions: list[ConflictRegion]

    # future — populated by analyzers, None until then
    classification: str | None = None        # "complementary" | "contradictory" | "duplicative"
    suggested_merge_order: MergeOrder | None = None
    proposed_resolution: str | None = None
    confidence: float | None = None
