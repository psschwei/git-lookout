from __future__ import annotations

from dataclasses import dataclass

# Renders the conflict-comment markdown posted on PRs by the webhook path.
#
# These are pure string builders — no GitHub calls, no DB — matching the
# templates in docs/spec.md ("Reporting" section). A comment is posted on *both*
# PRs of a conflicting pair, each written from the perspective of the PR it lands
# on: `this_pr` is the PR the comment is on, `other_pr` is the one it conflicts
# with. The handler renders twice (once per side) with the roles swapped.
#
# v1 renders only the always-present ConflictResult fields. When future analyzers
# populate classification / suggested merge order / resolution, add sections here
# that render only when the field is present ("render what's there") — no
# version-gated branching elsewhere.


@dataclass
class CommentPR:
    """The minimal PR identity a comment needs: its number and title."""

    number: int
    title: str


def render_conflict(
    this_pr: CommentPR, other_pr: CommentPR, conflicting_files: list[str]
) -> str:
    """
    Render the "potential conflict" comment for the PR identified by ``this_pr``.

    Names ``other_pr`` as the conflicting PR and lists the shared files. Mirrors
    the spec's conflict-detected template.
    """
    file_lines = "\n".join(f"- `{path}`" for path in conflicting_files)
    return (
        f"⚠️ **Potential conflict with PR #{other_pr.number}**\n"
        f"\n"
        f"This PR and #{other_pr.number} both modify the following files:\n"
        f"{file_lines}\n"
        f"\n"
        f"**This PR (#{this_pr.number})**: {this_pr.title}\n"
        f"**PR #{other_pr.number}**: {other_pr.title}\n"
        f"\n"
        f"Consider coordinating merge order to avoid conflicts."
    )


def render_resolved(other_pr_number: int) -> str:
    """
    Render the "resolved" comment that replaces a prior conflict comment once the
    pair no longer conflicts (or the other PR closed). Mirrors the spec's
    conflict-resolved template.
    """
    return (
        f"~~⚠️ **Potential conflict with PR #{other_pr_number}**~~\n"
        f"\n"
        f"✅ **Resolved** — this PR no longer conflicts with #{other_pr_number}."
    )
