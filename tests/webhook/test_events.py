from __future__ import annotations

from git_lookout.webhook.events import (
    CLOSE_ACTIONS,
    UPDATE_ACTIONS,
    parse_pull_request_event,
)


def _payload(action: str = "opened", **overrides) -> dict:
    payload = {
        "action": action,
        "pull_request": {
            "number": 42,
            "title": "Add validation",
            "head": {"sha": "abc123", "ref": "feature"},
            "base": {"ref": "main"},
            "user": {"login": "octocat"},
            "updated_at": "2026-06-16T00:00:00Z",
            "merged": False,
        },
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
        "installation": {"id": 99},
    }
    payload.update(overrides)
    return payload


def test_parses_a_well_formed_event():
    event = parse_pull_request_event(_payload(action="synchronize"))
    assert event is not None
    assert event.action == "synchronize"
    assert event.repo_owner == "acme"
    assert event.repo_name == "widgets"
    assert event.installation_id == 99
    assert event.pr.number == 42
    assert event.pr.head_sha == "abc123"
    assert event.pr.base_ref == "main"
    assert event.merged is False


def test_update_and_close_action_sets_cover_the_lifecycle():
    assert UPDATE_ACTIONS == {"opened", "synchronize", "reopened"}
    assert "closed" in CLOSE_ACTIONS


def test_merged_flag_is_read_from_payload():
    event = parse_pull_request_event(
        _payload(action="closed", pull_request={
            "number": 1, "title": "t",
            "head": {"sha": "s", "ref": "r"}, "base": {"ref": "main"},
            "user": {"login": "u"}, "updated_at": "2026-06-16T00:00:00Z",
            "merged": True,
        })
    )
    assert event is not None and event.merged is True


def test_non_dict_payload_returns_none():
    assert parse_pull_request_event([]) is None  # type: ignore[arg-type]


def test_missing_action_returns_none():
    payload = _payload()
    del payload["action"]
    assert parse_pull_request_event(payload) is None


def test_missing_pull_request_returns_none():
    payload = _payload()
    del payload["pull_request"]
    assert parse_pull_request_event(payload) is None


def test_missing_repo_owner_returns_none():
    payload = _payload(repository={"name": "widgets"})
    assert parse_pull_request_event(payload) is None


def test_missing_installation_id_returns_none():
    payload = _payload(installation={})
    assert parse_pull_request_event(payload) is None


def test_malformed_pull_request_object_returns_none():
    # head missing its sha — _parse_pull_request raises KeyError, folded to None.
    payload = _payload(pull_request={
        "number": 5, "title": "t", "head": {"ref": "r"},
        "base": {"ref": "main"}, "updated_at": "2026-06-16T00:00:00Z",
    })
    assert parse_pull_request_event(payload) is None
