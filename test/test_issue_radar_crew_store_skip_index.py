"""A pass is indexed repository-wide, and the first decision on a number stands.

This file pins the shared skip index every crew consults before it investigates: a pass
is part of the update that records it, readable by number and by predicate, folded
across every crew of the repository with the first decision standing whatever the
clocks say, its scope coerced into the closed vocabulary, and listed newest first and
bounded. Neither a pass nor any other sibling file in the crews directory is ever
enumerated as a crew. The update entry lives in ``test_issue_radar_crew_store.py`` and
the carry of a pre-projection ``skipped.json`` in
``test_issue_radar_crew_store_legacy_carry.py``.
"""

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _crew,
    _entries,
    _isolated_crew_log,
    _live_crew,
    _pass,
    _record,
    _tick,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection


def test_a_pass_is_readable_by_number_and_by_predicate(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _pass(
        tmp_path, crew["id"], sid, 42, "needs an owner decision on the data model", "needs-design"
    )
    row = result["skip"]
    assert row["number"] == 42
    assert row["scope"] == "needs-design"
    assert row["crew_id"] == crew["id"]
    assert row["decided_at"] == result["event"]["ts"]
    assert result["item"]["phase"] == "skipped" and result["item"]["finished_at"]
    # Keyed by the STRING form, because that is what a JSON object key is -- a
    # reader that looked up the int would miss every entry.
    assert set(cs.read_skips(OWNER, REPO, tmp_path)) == {"42"}
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is True
    assert cs.is_skipped(OWNER, REPO, 43, tmp_path) is False
    # One entry carried all three: the item, the line and the row.
    (entry,) = _entries(sid)
    assert entry.data["skip"] == {
        "reason": "needs an owner decision on the data model",
        "scope": "needs-design",
    }
    assert entry.data["phase"] == "skipped" and entry.data["event_kind"] == "skip"


def test_a_pass_without_a_phase_change_still_indexes(tmp_path):
    """``skip_reason`` alone is what makes an update a pass; the coupling to the
    item and the line is the entry's, not the phase's."""
    crew, sid = _live_crew(tmp_path)
    result = _record(
        tmp_path,
        crew["id"],
        sid,
        42,
        {},
        "skip",
        "dup of #41",
        skip_reason="duplicate of #41",
        skip_scope="duplicate",
    )
    assert result["skip"]["scope"] == "duplicate"
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is True


def test_re_skipping_keeps_the_first_crews_reason(tmp_path):
    """First-decision-wins, because the first reason is the audit trail.

    Two crews reaching the same pass is normal; the record a human reads when
    asking why an issue keeps being passed over must be the one that was actually
    decided first, not whichever crew wrote most recently.
    """
    first, sid_1 = _live_crew(tmp_path, "Andromeda")
    second, sid_2 = _live_crew(tmp_path, "Whirlpool")
    _pass(tmp_path, first["id"], sid_1, 42, "first reason", "architecture")
    _tick()
    theirs = _pass(tmp_path, second["id"], sid_2, 42, "second reason", "duplicate")
    # The second crew is told what STANDS, not what it sent, so it can see its own
    # reason was not the one kept -- and who decided first.
    assert theirs["skip"]["reason"] == "first reason"
    assert theirs["skip"]["crew_id"] == first["id"]
    # Its own fold still holds its own conclusion: a disagreement to surface on its
    # item rather than a silent edit of someone else's record...
    assert (
        cs.read_ledger(OWNER, REPO, second["id"], tmp_path)["skips"]["42"]["reason"]
        == "second reason"
    )
    # ...while the shared index answers with the decision that stands.
    standing = cs.read_skips(OWNER, REPO, tmp_path)
    assert list(standing) == ["42"]
    assert standing["42"]["reason"] == "first reason"
    assert standing["42"]["scope"] == "architecture"
    assert standing["42"]["crew_id"] == first["id"]


def test_the_skip_index_is_folded_across_the_crews_of_one_repository(tmp_path):
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _crew(tmp_path, name="Pinwheel")  # never ran: contributes nothing and breaks nothing
    _pass(tmp_path, a["id"], sid_a, 7, "architecture call", "architecture")
    _tick()
    _pass(tmp_path, b["id"], sid_b, 9, "duplicate of #7", "duplicate")

    index = cs.read_skips(OWNER, REPO, tmp_path)
    assert sorted(index) == ["7", "9"]
    assert index["7"]["crew_id"] == a["id"] and index["9"]["crew_id"] == b["id"]
    # Each crew's own fold holds only its own passes; the union is made on read.
    assert set(cs.read_ledger(OWNER, REPO, b["id"], tmp_path)["skips"]) == {"9"}
    # A retired crew's passes still stand: the decision outlives the crew.
    cs.retire_crew(OWNER, REPO, a["id"], tmp_path)
    assert cs.is_skipped(OWNER, REPO, 7, tmp_path) is True
    assert [r["number"] for r in cs.recent_skips(OWNER, REPO, tmp_path)] == [9, 7]


@pytest.mark.parametrize("scope", ["needs-decision", "needs-investigation"])
def test_a_needs_human_pass_is_accepted_and_indexed(tmp_path, scope):
    """The two scopes that replace holding an issue for a human.

    They have to survive :func:`coerce_skip_scope` verbatim rather than land in
    ``other``: the scope is how the recent-skip list -- and a person reading the
    index -- tells "this fleet does not do architecture work" from "somebody needs to
    answer a question", and collapsing them loses the only signal that says a human
    owes something back.
    """
    crew, sid = _live_crew(tmp_path)
    assert scope in cs.SKIP_SCOPES
    assert cs.coerce_skip_scope(scope) == scope
    result = _pass(tmp_path, crew["id"], sid, 42, "needs the owner's call", scope)
    assert result["skip"]["scope"] == scope
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["scope"] == scope
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is True
    assert cs.recent_skips(OWNER, REPO, tmp_path)[0]["scope"] == scope


@pytest.mark.parametrize("given", ["needs_design", "NEEDS-DESIGN-ISH", "", None, "vibes", 7])
def test_an_unknown_scope_coerces_to_other(tmp_path, given):
    """Coerced, never refused.

    Refusing would cost the whole skip record for a bad filter label, and a pass
    that fails to record is the exact duplicated investigation this index removes.
    """
    crew, sid = _live_crew(tmp_path)
    result = _pass(tmp_path, crew["id"], sid, 42, "why", given)
    assert result["skip"]["scope"] == "other"
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["scope"] == "other"
    # Coerced BEFORE the entry was built: the closed vocabulary on the entry type
    # never sees the raw label.
    (entry,) = _entries(sid)
    assert entry.data["skip"]["scope"] == "other"


def test_a_known_scope_survives_case_and_padding(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _pass(tmp_path, crew["id"], sid, 42, "why", "  Already-Fixed ")
    assert result["skip"]["scope"] == "already-fixed"


def test_recent_skips_are_newest_first_and_bounded(tmp_path):
    crew, sid = _live_crew(tmp_path)
    for number in range(1, 6):
        _pass(tmp_path, crew["id"], sid, number, f"reason {number}", "other")
        _tick()
    rows = cs.recent_skips(OWNER, REPO, tmp_path, limit=3)
    assert [r["number"] for r in rows] == [5, 4, 3]


def test_recording_a_skip_does_not_create_a_phantom_crew(tmp_path):
    crew, sid = _live_crew(tmp_path)
    before = [c["id"] for c in cs.list_crews(OWNER, REPO, tmp_path)]
    _pass(tmp_path, crew["id"], sid, 12, "duplicate of #11", "duplicate")
    after = [c["id"] for c in cs.list_crews(OWNER, REPO, tmp_path)]
    assert after == before


# ── a file in the crews directory that is not a crew ────────────────────────
#
# The directory holds `settings.json` and `skipped.json` beside the records.
# Excluding siblings BY NAME meant the first recorded skip was read as a crew:
# it carries no `id`, so the watchdog launched a session keyed `crew-None` and,
# because `unattended` defaults on, handed that phantom trust. The gate is the
# crew-id SHAPE now, so any sibling added later is excluded without anyone
# remembering to extend a list.


def test_sibling_files_are_never_enumerated_as_crews(tmp_path):
    crew = _crew(tmp_path)
    d = cs.crews_dir(OWNER, REPO, tmp_path)
    (d / "settings.json").write_text('{"claim_ttl_hours": 48}')
    (d / "skipped.json").write_text('{"12": {"number": 12, "reason": "dup"}}')
    # A plausible future sibling: the point is that nobody has to add it here.
    (d / "index.json").write_text("{}")

    listed = cs.list_crews(OWNER, REPO, tmp_path)

    assert [c["id"] for c in listed] == [crew["id"]]
    assert all(c.get("id") for c in listed), "a record with no id was enumerated"


def test_a_pass_after_another_crews_decision_defers_to_it_whatever_the_clocks_say(tmp_path):
    """The shared index keeps the FIRST decision on a number. Which is first is decided
    by the writer's own observation -- a pass recorded while another crew's decision
    already stood carries ``deferred`` -- not by the recorded time, so a clock stepped
    backward on the later crew cannot put its pass in front."""
    a, sid_a = _live_crew(tmp_path, "Andromeda")
    b, sid_b = _live_crew(tmp_path, "Bode")
    _record(
        tmp_path,
        a["id"],
        sid_a,
        42,
        {"phase": "skipped"},
        "skip",
        "dup",
        skip_reason="A saw it first",
    )
    # B's pass is recorded with a decided_at EARLIER than A's -- a clock behind A's --
    # by emitting the entry the writer would have built, token included.
    stale_clock = {
        "crew_id": b["id"],
        "owner": OWNER,
        "repo": REPO,
        "number": 42,
        "phase": "skipped",
        "skip": {
            "reason": "B, on a slow clock",
            "scope": "duplicate",
            "decided_at": "2000-01-01T00:00:00Z",
            "deferred": True,
        },
        "event": "dup",
        "event_kind": "skip",
    }
    crew_log_emit.on_radar_recorded(sid_b, stale_clock)
    assert crew_log_emit.flush(timeout=5)
    crew_log_projection.forget_slot_folds()
    standing = cs.read_skips(OWNER, REPO, tmp_path)["42"]
    assert (standing["crew_id"], standing["reason"]) == (a["id"], "A saw it first")
    # The control: the same row WITHOUT the token would have stood by its older clock.
    assert cs._skip_precedence({**standing}) < cs._skip_precedence(
        {"decided_at": "2000-01-01T00:00:00Z", "crew_id": b["id"], "deferred": True}
    )
    assert cs._skip_precedence(
        {"decided_at": "2000-01-01T00:00:00Z", "crew_id": b["id"], "deferred": False}
    ) < cs._skip_precedence(standing)
    # And the writer sets the token itself: a live pass by B on a number A decided.
    res = _record(
        tmp_path, b["id"], sid_b, 43, {"phase": "skipped"}, "skip", "dup", skip_reason="B"
    )
    assert res["skip"]["crew_id"] == b["id"], "no one had decided #43: B's row stands"
    _record(tmp_path, a["id"], sid_a, 43, {"phase": "skipped"}, "skip", "dup", skip_reason="A too")
    (entry,) = [e for e in _entries(sid_a) if e.data.get("number") == 43]
    assert entry.data["skip"]["deferred"] is True
    assert cs.read_skips(OWNER, REPO, tmp_path)["43"]["crew_id"] == b["id"]
