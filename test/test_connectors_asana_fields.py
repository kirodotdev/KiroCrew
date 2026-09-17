"""Tests for Asana task/project field normalization.

Every vendor constraint the module enforces has both a positive and a negative
test: GID opacity (no int coercion), due_on/due_at strict separation (no
mixing, no inference), multi-project membership, and explicit-workspace
placement.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.asana.fields import (
    ME_SENTINEL,
    AsanaFieldError,
    DueDate,
    StartDate,
    normalize_gid,
    normalize_gid_set,
    normalize_task_create,
    parse_project,
    parse_task,
)

# ── GID is an opaque string, never a number ─────────────────────────────────


def test_normalize_gid_returns_string_verbatim():
    assert normalize_gid("1203987654321098") == "1203987654321098"


def test_normalize_gid_strips_surrounding_whitespace():
    assert normalize_gid("  1203987654321098  ") == "1203987654321098"


def test_normalize_gid_preserves_leading_zero_never_coerces_to_int():
    # A digit-only GID with a leading zero must survive verbatim: int() would
    # drop the zero and change the identity.
    assert normalize_gid("0012345") == "0012345"


def test_normalize_gid_accepts_non_numeric_opaque_id():
    # The digit-only spelling is not a contract; an opaque alphanumeric id is a
    # legal GID and must not be rejected for "not being a number".
    assert normalize_gid("abc_DEF-123") == "abc_DEF-123"


def test_normalize_gid_rejects_int():
    with pytest.raises(AsanaFieldError):
        normalize_gid(1203987654321098)  # type: ignore[arg-type]


def test_normalize_gid_rejects_empty():
    with pytest.raises(AsanaFieldError):
        normalize_gid("   ")


def test_normalize_gid_rejects_path_separator():
    with pytest.raises(AsanaFieldError):
        normalize_gid("123/456")


def test_normalize_gid_me_sentinel_refused_by_default():
    with pytest.raises(AsanaFieldError):
        normalize_gid("me")


def test_normalize_gid_me_sentinel_allowed_when_opted_in():
    assert normalize_gid("me", allow_me=True) == ME_SENTINEL


def test_normalize_gid_set_dedupes_and_is_frozenset():
    result = normalize_gid_set(["1", "2", "1"])
    assert result == frozenset({"1", "2"})


def test_normalize_gid_set_rejects_bare_string():
    # A bare string would iterate character-by-character into bogus GIDs.
    with pytest.raises(AsanaFieldError):
        normalize_gid_set("123")


def test_normalize_gid_set_empty_is_legal():
    assert normalize_gid_set([]) == frozenset()


# ── due_on / due_at are distinct, never mixed, never inferred ────────────────


def test_due_date_on_only():
    d = DueDate(on="2026-09-15")
    assert d.on == "2026-09-15"
    assert d.at is None


def test_due_date_at_only():
    d = DueDate(at="2026-09-15T17:00:00.000Z")
    assert d.at == "2026-09-15T17:00:00.000Z"
    assert d.on is None


def test_due_date_neither_is_unset():
    assert DueDate().both_unset is True


def test_due_date_rejects_both_set():
    with pytest.raises(AsanaFieldError):
        DueDate(on="2026-09-15", at="2026-09-15T17:00:00.000Z")


def test_due_on_rejects_a_datetime_value():
    # A datetime supplied where a date was meant must not be silently truncated.
    with pytest.raises(AsanaFieldError):
        DueDate(on="2026-09-15T17:00:00Z")


def test_due_at_rejects_a_date_only_value():
    # A date supplied where a datetime was meant must not be silently promoted.
    with pytest.raises(AsanaFieldError):
        DueDate(at="2026-09-15")


def test_due_on_rejects_malformed_date():
    with pytest.raises(AsanaFieldError):
        DueDate(on="15-09-2026")


def test_start_date_same_mutual_exclusion():
    with pytest.raises(AsanaFieldError):
        StartDate(on="2026-09-15", at="2026-09-15T09:00:00Z")


def test_due_date_never_infers_on_from_at():
    d = DueDate(at="2026-09-15T17:00:00.000Z")
    # Reading `on` yields None, not a truncated date derived from `at`.
    assert d.on is None


# ── task create: destination is explicit, workspace never inferred ──────────


def test_task_create_project_scoped():
    tc = normalize_task_create(name="Ship it", projects=["111", "222"])
    assert tc.projects == frozenset({"111", "222"})
    assert tc.workspace is None


def test_task_create_workspace_rooted():
    tc = normalize_task_create(name="Ship it", workspace="900")
    assert tc.workspace == "900"
    assert tc.projects == frozenset()


def test_task_create_both_workspace_and_projects():
    tc = normalize_task_create(name="Ship it", workspace="900", projects=["111"])
    assert tc.workspace == "900"
    assert tc.projects == frozenset({"111"})


def test_task_create_multi_project_membership_preserved():
    tc = normalize_task_create(name="x", projects=["1", "2", "3"])
    assert tc.projects == frozenset({"1", "2", "3"})


def test_task_create_refuses_neither_workspace_nor_project():
    # The explicit-destination rule: a create with no workspace and no project
    # is refused rather than a workspace being inferred.
    with pytest.raises(AsanaFieldError):
        normalize_task_create(name="orphan")


def test_task_create_refuses_empty_name():
    with pytest.raises(AsanaFieldError):
        normalize_task_create(name="   ", workspace="900")


def test_task_create_rejects_mixed_due():
    with pytest.raises(AsanaFieldError):
        normalize_task_create(
            name="x", workspace="900", due_on="2026-09-15", due_at="2026-09-15T10:00:00Z"
        )


def test_task_create_assignee_accepts_me():
    tc = normalize_task_create(name="x", workspace="900", assignee="me")
    assert tc.assignee == ME_SENTINEL


def test_task_create_parent_rejects_me():
    # parent is a task GID position; 'me' is not valid there.
    with pytest.raises(AsanaFieldError):
        normalize_task_create(name="x", workspace="900", parent="me")


# ── parse_task ──────────────────────────────────────────────────────────────


def test_parse_task_reads_multi_project_membership():
    task = parse_task(
        {
            "gid": "555",
            "name": "Task",
            "projects": [{"gid": "1"}, {"gid": "2"}],
            "due_on": "2026-09-15",
        }
    )
    assert task.projects == frozenset({"1", "2"})
    assert task.due.on == "2026-09-15"
    assert task.due.at is None


def test_parse_task_does_not_infer_workspace_from_projects():
    task = parse_task({"gid": "555", "projects": [{"gid": "1"}]})
    # No workspace stated -> workspace stays None; it is NOT derived from the
    # project membership.
    assert task.workspace is None


def test_parse_task_reads_explicit_workspace():
    task = parse_task({"gid": "555", "workspace": {"gid": "900"}, "projects": []})
    assert task.workspace == "900"


def test_parse_task_keeps_completed_at_independent_of_completed():
    # The model does not assert completed_at is cleared on reopen; it carries
    # whatever Asana returned. Here a reopened task still reports a completed_at.
    task = parse_task(
        {"gid": "555", "completed": False, "completed_at": "2026-09-10T00:00:00.000Z"}
    )
    assert task.completed is False
    assert task.completed_at == "2026-09-10T00:00:00.000Z"


def test_parse_task_rejects_non_list_projects():
    with pytest.raises(AsanaFieldError):
        parse_task({"gid": "555", "projects": "not-a-list"})


def test_parse_task_rejects_missing_gid():
    with pytest.raises(AsanaFieldError):
        parse_task({"name": "no gid"})


def test_parse_task_rejects_due_on_datetime_from_vendor_shape():
    # Even reading a raw object, a datetime in the due_on slot is a shape fault
    # (Asana never emits one there, so it signals a mis-built object).
    with pytest.raises(AsanaFieldError):
        parse_task({"gid": "555", "due_on": "2026-09-15T10:00:00Z"})


def test_parse_task_accepts_asana_due_on_plus_due_at_response():
    # Asana returns due_on populated with the DATE COMPONENT whenever due_at is
    # set. That is a valid response, NOT the caller ambiguity _reject_both
    # guards -- parse_task must accept it: the datetime wins and the derived
    # due_on is dropped (no inference), rather than raising AsanaFieldError.
    task = parse_task(
        {
            "gid": "555",
            "due_on": "2026-09-15",
            "due_at": "2026-09-15T10:00:00Z",
        }
    )
    assert task.due.at == "2026-09-15T10:00:00Z"
    assert task.due.on is None


def test_parse_task_accepts_asana_start_on_plus_start_at_response():
    # start_* has the same read-path rule as due_*.
    task = parse_task(
        {
            "gid": "556",
            "start_on": "2026-09-15",
            "start_at": "2026-09-15T08:00:00Z",
        }
    )
    assert task.start.at == "2026-09-15T08:00:00Z"
    assert task.start.on is None


def test_parse_task_reads_due_on_when_no_due_at():
    # A date-only due (no time) is preserved verbatim on the read path.
    task = parse_task({"gid": "557", "due_on": "2026-09-15"})
    assert task.due.on == "2026-09-15"
    assert task.due.at is None


# ── parse_project ───────────────────────────────────────────────────────────


def test_parse_project_reads_workspace_and_archived():
    proj = parse_project(
        {"gid": "700", "name": "Launch", "workspace": {"gid": "900"}, "archived": True}
    )
    assert proj.gid == "700"
    assert proj.workspace == "900"
    assert proj.archived is True


def test_parse_project_rejects_missing_gid():
    with pytest.raises(AsanaFieldError):
        parse_project({"name": "no gid"})
