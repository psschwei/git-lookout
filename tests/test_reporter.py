from __future__ import annotations

from git_lookout.reporter import CommentPR, render_conflict, render_resolved


def test_render_conflict_matches_template():
    body = render_conflict(
        this_pr=CommentPR(number=42, title="Add input validation to order processing"),
        other_pr=CommentPR(number=57, title="Add audit logging to order processing"),
        conflicting_files=["src/api/orders.ts"],
    )
    assert body == (
        "⚠️ **Potential conflict with PR #57**\n"
        "\n"
        "This PR and #57 both modify the following files:\n"
        "- `src/api/orders.ts`\n"
        "\n"
        "**This PR (#42)**: Add input validation to order processing\n"
        "**PR #57**: Add audit logging to order processing\n"
        "\n"
        "Consider coordinating merge order to avoid conflicts."
    )


def test_render_conflict_lists_every_file():
    body = render_conflict(
        this_pr=CommentPR(number=1, title="a"),
        other_pr=CommentPR(number=2, title="b"),
        conflicting_files=["a.py", "b.py"],
    )
    assert "- `a.py`\n- `b.py`" in body


def test_render_resolved_matches_template():
    assert render_resolved(57) == (
        "~~⚠️ **Potential conflict with PR #57**~~\n"
        "\n"
        "✅ **Resolved** — this PR no longer conflicts with #57."
    )
