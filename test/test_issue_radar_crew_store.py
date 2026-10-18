"""Tests for the Issue Radar crew store: crew records, per-repo settings, and the
crew LEDGER -- work items, progress lines and the repository-shared skip index --
which is a fold of ``radar/recorded`` crew log entries rather than files of its own.

The coverage here is deliberately weighted toward the invariants whose failure is
SILENT, because those are the ones that corrupt the claim protocol rather than
raising:

  * **Name reuse.** A retired crew's name still appears in the check-in comments
    it left on the forge, so reusing it makes an old comment look like a live
    claim. Uniqueness is checked in the store because the name field is free text.
  * **``last_progress_at``.** The claim TTL is measured from this field, so a
    read-back or a no-op write must not renew a claim. If it did, a dead crew
    would hold an issue forever and nothing would report it.
  * **One editing item.** Two worktrees with uncommitted changes is how a fix for
    one issue gets committed onto another issue's branch. The store refuses the
    second one; a warning would not.
  * **Slot accounting.** Every unfinished item consumes a work slot, because a
    crew never holds an issue waiting for a human: one it cannot progress alone is
    recorded as a pass and its claim released.
  * **One entry per update.** The item's delta, the line that explains it and the
    skip row that indexes a pass ride on ONE appended entry, so no reader can see
    one without the others and a crash cannot separate them.
  * **The skip index is repo-wide and first-decision-wins.** It is a fold across
    every crew of the repository, and the EARLIEST recorded decision on a number
    stands whichever crew's log it sits in.

Every test runs against a ``tmp_path`` root -- the store threads ``root`` through
every crew-record function -- and against its own crew log data home, so nothing
here touches a real data home. A crew's ledger lives in the crew log of the
session its slot runs on, so a test that records creates that unit first
(:func:`_unit`) and passes its id as ``session_id``, exactly as the write route does.

This file pins the crew records, the per-repository settings, the work-item rules and
the one entry an update appends: what it carries, the refusals that name why nothing
was appended, the read-back that answers it, the fold across every unit a crew's slot
ran under, a cleared field and a collapsed repeat. The fold's bounds live in
``test_issue_radar_crew_store_fold_bounds.py``, progress lines and crew-level sweeps in
``test_issue_radar_crew_store_progress_lines.py``, the shared skip index (the
repo-wide, first-decision-wins invariant above, and the skip row a pass's one entry
carries) in ``test_issue_radar_crew_store_skip_index.py``, the pre-projection carry in
``test_issue_radar_crew_store_legacy_carry.py``, the writes refused against a record
that cannot be proven in ``test_issue_radar_crew_store_write_refusal.py``, the
unit-order file in ``test_issue_radar_crew_store_unit_order.py`` and non-finite numbers
in ``test_issue_radar_crew_store_non_finite.py``; the shared crew and ledger helpers
live in ``issue_radar_crew_store_helpers.py``.
"""

import json

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _crew,
    _entries,
    _isolated_crew_log,
    _item,
    _live_crew,
    _record,
    _tick,
    _unit,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log_projection

# ── crews ───────────────────────────────────────────────────────────────────


def test_create_assigns_id_slot_key_and_seed(tmp_path):
    crew = _crew(tmp_path)
    assert crew["id"].startswith("c_")
    assert crew["slot_key"] == f"crew-{crew['id']}"
    # The seed defaults to the name but is a SEPARATE field, so a later rename
    # keeps the face.
    assert crew["avatar_seed"] == "Andromeda"
    assert crew["schema"] == cs.CREW_SCHEMA
    assert crew["max_open"] == 3
    assert "max_escalated" not in crew, "a crew never holds work for a human"
    assert crew["auto_merge"] is True and crew["unattended"] is True


def test_a_crew_id_in_use_anywhere_is_never_minted_again(tmp_path, monkeypatch):
    """A crew's id names its slot, and a slot is a data-home-wide name the crew log
    lists units by: two crews in different repositories with one id would fold each
    other's ledgers into one record. Creation checks every repository's crew records
    and the crew log's slot listing before an id is taken, so a clash cannot happen."""
    other = cs.create_crew("someone", "elsewhere", {"name": "Twin"}, root=tmp_path)
    from kiro_crew.apps.builtins.issue_radar.backend import store as radar_store

    gitlab_root = radar_store.provider_root(
        root=tmp_path, provider="gitlab", host="gitlab.example.org"
    )
    foreign = cs.create_crew("group", "project", {"name": "Far"}, root=gitlab_root)
    assert gitlab_root != tmp_path and "@providers" in str(gitlab_root)
    _unit("c_deadbeef")  # a slot the crew log knows, with no crew record anywhere
    minted = iter([other["id"][2:], foreign["id"][2:], "deadbeef", "0badf00d"])
    monkeypatch.setattr(cs.secrets, "token_hex", lambda n: next(minted))
    crew = cs.create_crew(OWNER, REPO, {"name": "Third"}, root=tmp_path)
    assert crew["id"] == "c_0badf00d", "the three ids in use were passed over"
    # And the check runs from a provider subtree the same way: the legacy root's
    # crews are seen from there.
    minted = iter([other["id"][2:], "0badf00e"])
    twin = cs.create_crew("group", "other", {"name": "Fourth"}, root=gitlab_root)
    assert twin["id"] == "c_0badf00e"


def test_duplicate_name_is_refused(tmp_path):
    _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="already taken"):
        _crew(tmp_path)


@pytest.mark.parametrize(
    "bad_id",
    [
        "/etc/policy",  # absolute: pathlib DISCARDS the base
        "../../../../etc/policy",  # relative traversal
        "c_1234abcd/../../escape",  # valid prefix, then escapes
        "C_1234ABCD",  # generator emits lowercase hex only
        "c_12345678901234",  # right prefix, wrong length
        "c_zzzzzzzz",  # right shape, not hex
        "",
    ],
)
def test_a_crew_id_cannot_escape_the_store(tmp_path, bad_id):
    """Every path constructor must refuse an id it did not mint.

    `Path(store) / "/etc/policy"` evaluates to `/etc/policy` — the base is thrown
    away — so an unchecked id turns `GET /crew` into an arbitrary-file read and
    `PUT /crew` into an arbitrary-file write. `work_item_path` also mkdirs the
    joined path, so a traversal would create directories outside the store.
    """
    for build in (
        lambda: cs.crew_path(OWNER, REPO, bad_id, tmp_path),
        lambda: cs._crew_lock_path(OWNER, REPO, bad_id, tmp_path),
        lambda: cs.work_item_path(OWNER, REPO, bad_id, 1, tmp_path),
    ):
        with pytest.raises(cs.CrewStoreError, match="invalid crew id"):
            build()


def test_a_minted_id_is_accepted_by_every_constructor(tmp_path):
    """The gate must not reject what `create_crew` actually produces."""
    crew = _crew(tmp_path)
    assert cs.crew_path(OWNER, REPO, crew["id"], tmp_path).name.endswith(".json")
    assert cs._crew_lock_path(OWNER, REPO, crew["id"], tmp_path).name.endswith(".lock")
    assert cs.work_item_path(OWNER, REPO, crew["id"], 7, tmp_path).is_absolute()
    # And the store's own directory is the parent — nothing escaped.
    assert (
        cs.crews_dir(OWNER, REPO, tmp_path)
        in cs.crew_path(OWNER, REPO, crew["id"], tmp_path).parents
    )


def test_a_rename_takes_the_repo_wide_record_lock(tmp_path):
    """A rename must serialise on the REPO-WIDE lock, not this crew's.

    Asserted by watching which lock file the call opens, because the outcome alone
    cannot show it: run two renames sequentially and the second sees the first's
    write whatever lock is held, so a sequential test passes against the bug. The
    race needs two renames of DIFFERENT crews to overlap, each reading a
    ``taken_names()`` that predates the other — and the only thing preventing that
    is both calls contending on one lock.
    """
    a = _crew(tmp_path, name="Andromeda")
    _crew(tmp_path, name="Bode")

    locked: list[str] = []
    real_records = cs._records_lock_path
    real_per_crew = cs._crew_lock_path

    def spy_records(owner, repo, root=None):
        locked.append("repo-wide")
        return real_records(owner, repo, root)

    def spy_per_crew(owner, repo, crew_id, root=None):
        locked.append("per-crew")
        return real_per_crew(owner, repo, crew_id, root)

    cs._records_lock_path = spy_records
    cs._crew_lock_path = spy_per_crew
    try:
        cs.update_crew(OWNER, REPO, a["id"], {"name": "Cocoon"}, tmp_path)
    finally:
        cs._records_lock_path = real_records
        cs._crew_lock_path = real_per_crew

    assert locked == [
        "repo-wide"
    ], f"a rename must take only the repo-wide record lock, took {locked}"
    assert sorted(c["name"] for c in cs.list_crews(OWNER, REPO, tmp_path)) == [
        "Bode",
        "Cocoon",
    ]


def test_retire_takes_the_repo_wide_record_lock(tmp_path):
    """Retire writes the whole record too, so it must share the rename lock.

    On separate locks a retire and an update read-modify-write the same record
    concurrently and one silently drops the other's field.
    """
    crew = _crew(tmp_path)
    locked: list[str] = []
    real_records = cs._records_lock_path
    real_per_crew = cs._crew_lock_path
    cs._records_lock_path = lambda o, r, root=None: (
        locked.append("repo-wide") or real_records(o, r, root)
    )
    cs._crew_lock_path = lambda o, r, cid, root=None: (
        locked.append("per-crew") or real_per_crew(o, r, cid, root)
    )
    try:
        retired = cs.retire_crew(OWNER, REPO, crew["id"], tmp_path)
    finally:
        cs._records_lock_path = real_records
        cs._crew_lock_path = real_per_crew

    assert "per-crew" not in locked, f"retire took a per-crew lock: {locked}"
    assert retired["retired_at"] is not None


def test_retired_crew_keeps_its_name_reserved(tmp_path):
    crew = _crew(tmp_path)
    cs.retire_crew(OWNER, REPO, crew["id"], tmp_path)
    assert cs.list_crews(OWNER, REPO, tmp_path) == []
    assert len(cs.list_crews(OWNER, REPO, tmp_path, include_retired=True)) == 1
    with pytest.raises(cs.CrewStoreError, match="already taken"):
        _crew(tmp_path)


def test_rename_keeps_the_avatar_seed(tmp_path):
    crew = _crew(tmp_path)
    renamed = cs.update_crew(OWNER, REPO, crew["id"], {"name": "Whirlpool"}, tmp_path)
    assert renamed["name"] == "Whirlpool"
    assert renamed["avatar_seed"] == "Andromeda"


def test_unknown_patch_fields_are_dropped(tmp_path):
    crew = _crew(tmp_path)
    updated = cs.update_crew(
        OWNER, REPO, crew["id"], {"max_open": 5, "not_a_field": "x", "unattended": False}, tmp_path
    )
    assert updated["max_open"] == 5
    assert updated["unattended"] is False
    assert "not_a_field" not in updated


def test_out_of_range_limits_are_ignored(tmp_path):
    crew = _crew(tmp_path)
    updated = cs.update_crew(OWNER, REPO, crew["id"], {"max_open": 0}, tmp_path)
    assert updated["max_open"] == 3


def test_suggest_names_skips_taken_and_degrades_when_pool_is_spent(tmp_path):
    _crew(tmp_path, name="Andromeda")
    assert "Andromeda" not in cs.suggest_names(OWNER, REPO, tmp_path)
    for name in cs.NAME_POOL:
        if name != "Andromeda":
            _crew(tmp_path, name=name)
    # Pool exhausted — the degraded form is astronomically correct (Leo II etc.)
    suggestions = cs.suggest_names(OWNER, REPO, tmp_path, limit=2)
    assert len(suggestions) == 2
    assert all(s.endswith(" II") for s in suggestions)


# ── settings ────────────────────────────────────────────────────────────────


def test_settings_default_and_merge(tmp_path):
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 48
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 72
    # Untouched keys keep their default rather than disappearing.
    assert got["needs_human_label"] == cs.DEFAULT_SETTINGS["needs_human_label"]


def test_settings_rejects_nonsense(tmp_path):
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": -5, "commit_trailer": "  "}, tmp_path)
    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 48
    assert got["commit_trailer"] == cs.DEFAULT_SETTINGS["commit_trailer"]


def test_the_needs_human_label_is_configurable_and_trimmed(tmp_path):
    """A repo with its own triage vocabulary configures it, and the stored value is
    trimmed — a label with a leading space is a DIFFERENT label on the forge, so the
    person watching the queue would never see the issues the crew filed."""
    stored = cs.write_settings(
        OWNER, REPO, {"needs_human_label": "  needs: maintainer  "}, tmp_path
    )
    assert stored["needs_human_label"] == "needs: maintainer"
    assert cs.read_settings(OWNER, REPO, tmp_path)["needs_human_label"] == "needs: maintainer"


@pytest.mark.parametrize(
    "bad", ["", "   ", "\t\n", None, 42, True, ["crew: needs human"], {"a": 1}]
)
def test_a_blank_or_wrong_typed_needs_human_label_falls_back_to_the_default(tmp_path, bad):
    """A crew must always have a usable label. The write is dropped rather than
    stored, so the previous value stands and the default is what a fresh repo reads
    — never an empty string that would ask the forge to create a nameless label."""
    got = cs.write_settings(OWNER, REPO, {"needs_human_label": bad}, tmp_path)
    assert got["needs_human_label"] == cs.DEFAULT_SETTINGS["needs_human_label"]
    assert (
        cs.read_settings(OWNER, REPO, tmp_path)["needs_human_label"]
        == cs.DEFAULT_SETTINGS["needs_human_label"]
    )


def test_an_over_long_needs_human_label_falls_back_to_the_default(tmp_path):
    """It is written to the forge as a label and read back into a crew's prompt, so
    it is bounded rather than trusted because a settings form produced it."""
    got = cs.write_settings(
        OWNER, REPO, {"needs_human_label": "x" * (cs.MAX_SETTING_TEXT + 1)}, tmp_path
    )
    assert got["needs_human_label"] == cs.DEFAULT_SETTINGS["needs_human_label"]
    at_the_bound = "y" * cs.MAX_SETTING_TEXT
    assert (
        cs.write_settings(OWNER, REPO, {"needs_human_label": at_the_bound}, tmp_path)[
            "needs_human_label"
        ]
        == at_the_bound
    )


@pytest.mark.parametrize("stored", ["", "  ", 17, None, "z" * (cs.MAX_SETTING_TEXT + 1)])
def test_a_hand_edited_settings_file_cannot_blank_the_needs_human_label(tmp_path, stored):
    """``settings.json`` is an ordinary file in the data home, so it can be
    hand-edited or restored from a backup written by another version. Validation on
    READ is what stops that deciding which label a crew writes to someone's tracker.
    """
    path = cs.settings_path(OWNER, REPO, tmp_path)
    path.write_text(json.dumps({"schema": cs.CREW_SCHEMA, "needs_human_label": stored}))
    assert (
        cs.read_settings(OWNER, REPO, tmp_path)["needs_human_label"]
        == cs.DEFAULT_SETTINGS["needs_human_label"]
    )


def test_a_settings_file_missing_the_needs_human_label_reads_the_default(tmp_path):
    """The key postdates the first release of this store, so a settings file written
    before it exists must still answer with a usable label."""
    path = cs.settings_path(OWNER, REPO, tmp_path)
    path.write_text(json.dumps({"schema": cs.CREW_SCHEMA, "claim_ttl_hours": 24}))
    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 24
    assert got["needs_human_label"] == cs.DEFAULT_SETTINGS["needs_human_label"]


# ── the ledger: work items ──────────────────────────────────────────────────
#
# Every write below is one ``radar/recorded`` entry appended to the crew's live
# unit, and every read is the ``radar`` fold of that unit. The properties are the
# ones the file store had; what changed is that they now hold by construction of
# one append rather than by three files and three locks.


def test_a_record_stamps_claimed_at_and_merges_per_field(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    first = _item(
        tmp_path, cid, sid, 2251, {"phase": "claimed", "next": "read the call sites"}, "claim"
    )
    assert first["claimed_at"]
    assert first["next"] == "read the call sites"

    # A patch carrying only `decision` must preserve `next`.
    second = _item(tmp_path, cid, sid, 2251, {"decision": "fix it"})
    assert second["decision"] == "fix it"
    assert second["next"] == "read the call sites"
    assert second["claimed_at"] == first["claimed_at"]


def test_a_no_op_write_does_not_renew_the_claim(tmp_path):
    """The TTL is measured from ``last_progress_at``. A write that carries no
    progress must leave it alone, or a dead crew holds its claim forever."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    stamp = _item(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim")["last_progress_at"]

    _tick()
    polled = _record(tmp_path, cid, sid, 2251, {}, "ci", "polled")
    assert polled["item"]["last_progress_at"] == stamp
    # The line itself carries a LATER stamp, which is what makes the assertion above
    # bite: the write was distinguishable from the first and still did not renew.
    assert polled["event"]["ts"] > stamp
    # Re-asserting the SAME phase is not progress either.
    _tick()
    assert (
        _item(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim")["last_progress_at"] == stamp
    )
    # Nor is a field that carries no new information.
    _tick()
    assert _item(tmp_path, cid, sid, 2251, {"next": ""})["last_progress_at"] == stamp


def test_real_progress_moves_the_stamp(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    stamp = _item(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim")["last_progress_at"]

    for patch in (
        {"phase": "implementing"},
        {"next": "add the Windows branch"},
        {"pr_number": 2271},
        {"ci_state": {"state": "running", "round": 3}},
        {"tried_approach": "pywin32"},
        {"phase": "skipped"},
    ):
        _tick()
        got = _item(tmp_path, cid, sid, 2251, patch)
        assert got["last_progress_at"] != stamp, f"{patch} should count as progress"
        stamp = got["last_progress_at"]


def test_second_editing_item_is_refused_and_nothing_is_appended(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 2251, {"phase": "implementing"})
    before = len(_entries(sid))
    with pytest.raises(cs.CrewStoreError, match="already editing"):
        _item(tmp_path, cid, sid, 2264, {"phase": "implementing"})
    # An append-only log cannot take a line back, so the refusal happens BEFORE the
    # append: the refused update left no entry behind.
    assert len(_entries(sid)) == before
    assert cs.read_work_item(OWNER, REPO, cid, 2264, tmp_path) is None


def test_editing_slot_frees_when_the_first_item_parks(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 2251, {"phase": "implementing"})
    _item(tmp_path, cid, sid, 2251, {"phase": "awaiting-ci"}, "ci")
    other = _item(tmp_path, cid, sid, 2264, {"phase": "implementing"})
    assert other["phase"] == "implementing"


def test_staying_in_an_editing_phase_is_not_a_second_editor(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 2251, {"phase": "implementing"})
    again = _item(tmp_path, cid, sid, 2251, {"phase": "implementing", "next": "keep going"})
    assert again["next"] == "keep going"


def test_unknown_phase_is_refused(tmp_path):
    crew, sid = _live_crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown phase"):
        _item(tmp_path, crew["id"], sid, 1, {"phase": "vibing"})
    assert _entries(sid) == []


def test_every_unfinished_item_consumes_a_slot(tmp_path):
    """No phase is exempt any more. A crew that needs a human records a PASS, which
    is terminal and frees the slot -- it does not sit on one holding the issue."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 1, {"phase": "awaiting-ci"}, "ci")
    _item(tmp_path, cid, sid, 2, {"phase": "awaiting-reply"}, "reply")
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 2
    _item(tmp_path, cid, sid, 2, {"phase": "skipped"}, "skip")
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1


def test_escalated_is_no_longer_a_phase(tmp_path):
    """The store is the choke point every writer passes through, so refusing it here
    is what stops a stale caller -- an older nudge, another installation's marker, a
    crew resuming from a pre-change ledger -- parking an issue on a human again."""
    crew, sid = _live_crew(tmp_path)
    assert "escalated" not in cs.PHASES
    with pytest.raises(cs.CrewStoreError, match="unknown phase"):
        _item(tmp_path, crew["id"], sid, 1, {"phase": "escalated"})


def test_escalate_is_no_longer_an_event_kind(tmp_path):
    crew, sid = _live_crew(tmp_path)
    assert "escalate" not in cs.EVENT_KINDS
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        _record(tmp_path, crew["id"], sid, 1, {}, "escalate", "asking the owner")


def test_a_stale_escalation_payload_is_dropped_rather_than_recorded(tmp_path):
    """The entry is assembled field by field, so an unknown key in a patch is
    dropped -- the same discipline as every other unknown field.

    Pinned because the fold is a MERGE: if the field came back, a crew resuming
    from a pre-change ledger would carry an unanswerable question on its work item,
    and the surface would have something to render a "waiting on a human" card from.
    """
    crew, sid = _live_crew(tmp_path)
    item = _item(
        tmp_path,
        crew["id"],
        sid,
        1,
        {"phase": "claimed", "escalation": {"question": "which behaviour?"}},
        "claim",
    )
    assert "escalation" not in item
    (entry,) = _entries(sid)
    assert "escalation" not in entry.data


def test_terminal_phase_frees_the_slot_and_stamps_finished(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 1, {"phase": "implementing"})
    done = _item(tmp_path, cid, sid, 1, {"phase": "resolved"}, "merge")
    assert done["finished_at"]
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_reopening_clears_the_finish_stamp_so_the_next_one_counts(tmp_path):
    """A reopened issue's SECOND resolution is stamped at its own time.

    REGRESSION (file store): `finished_at` was written only when it was empty, so it
    recorded the FIRST time this item ever went terminal and nothing cleared it. An
    issue that was resolved, reopened and handled again by the same crew reuses
    this item, so it stayed stamped in the past -- putting the new resolution outside
    the `resolved24h` window and under-reporting work the crew had just done.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 1, {"phase": "implementing", "next": "land it"})
    first = _item(tmp_path, cid, sid, 1, {"phase": "resolved", "outcome": "merged in #9"}, "merge")
    first_stamp = first["finished_at"]
    assert first_stamp
    assert first["outcome"] == "merged in #9"

    # Reopened: back to a live phase. NO field describing a finished result may
    # survive, or the item reads as finished while it is demonstrably being worked
    # again. `outcome` is asserted here as well as `finished_at` because the two
    # were reported as separate defects -- clearing one and keeping the other still
    # leaves the ledger asserting a terminal result on active work.
    _tick()
    live = _item(tmp_path, cid, sid, 1, {"phase": "implementing"})
    assert live["finished_at"] is None, "a reopened item still claims it finished"
    assert live["outcome"] is None, "a reopened item still carries its old outcome"
    # Folded, not just in the returned dict -- the stat card reads the fold.
    stored = cs.read_work_item(OWNER, REPO, cid, 1, tmp_path)
    assert stored["finished_at"] is None
    assert stored["outcome"] is None

    # The crew's memory of what it already ruled out must NOT be cleared with it:
    # losing that makes it retry approaches it had rejected.
    assert live["next"] == first["next"] == "land it"

    _tick()
    again = _item(tmp_path, cid, sid, 1, {"phase": "resolved"}, "merge")
    assert again["finished_at"], "the second resolution was never stamped"
    assert again["finished_at"] > first_stamp


def test_tried_entries_append_rather_than_replace(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(
        tmp_path,
        cid,
        sid,
        1,
        {"tried_approach": "hasattr guard", "tried_rejected_because": "loses the ACL"},
    )
    second = _item(tmp_path, cid, sid, 1, {"tried_approach": "pywin32"})
    assert [t["approach"] for t in second["tried"]] == ["hasattr guard", "pywin32"]
    assert second["tried"][0]["rejected_because"] == "loses the ACL"


def test_work_items_are_scoped_per_crew(tmp_path):
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _item(tmp_path, a["id"], sid_a, 2251, {"phase": "implementing"})
    # Same issue number, different crew -- must not collide, and must not trip the
    # one-editor rule, which is per crew.
    _item(tmp_path, b["id"], sid_b, 2251, {"phase": "implementing"})
    assert cs.read_work_item(OWNER, REPO, a["id"], 2251, tmp_path)["crew_id"] == a["id"]
    assert cs.read_work_item(OWNER, REPO, b["id"], 2251, tmp_path)["crew_id"] == b["id"]


# ── the ledger: one entry per update ────────────────────────────────────────


def test_one_call_appends_exactly_one_entry_carrying_only_what_it_set(tmp_path):
    """The whole update is ONE line, which is what makes it crash-atomic."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(
        tmp_path,
        cid,
        sid,
        2251,
        {
            "phase": "implementing",
            "next": "add the Windows branch",
            "tried_approach": "hasattr guard",
            "tried_rejected_because": "loses the ACL",
            "branch": "fix/2251",
            "pr_number": 2271,
            "ci_state": {"state": "running", "round": 3},
            "labels_applied": ["claimed", 7, "auto-fix"],
        },
        "implement",
        "started",
    )

    (entry,) = _entries(sid)
    data = entry.data
    assert data["crew_id"] == cid and data["owner"] == OWNER and data["repo"] == REPO
    assert data["number"] == 2251 and data["phase"] == "implementing"
    assert data["tried"] == {"approach": "hasattr guard", "rejected_because": "loses the ACL"}
    assert data["ci_state"] == {"state": "running", "round": 3}
    assert data["labels_applied"] == ["claimed", "auto-fix"], "a non-string label is dropped"
    assert (data["event"], data["event_kind"]) == ("started", "implement")
    # Only the fields this call SET: an omitted field means "unchanged", and writing
    # it as null or empty would make the fold clear a value the caller never touched.
    for absent in ("why", "decision", "worktree", "base_sha", "outcome", "skip", "carried"):
        assert absent not in data


def test_record_then_fold_is_a_round_trip(tmp_path):
    """Every field a call sets comes back out of the fold that reads the entry."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    result = _record(
        tmp_path,
        cid,
        sid,
        2251,
        {
            "phase": "awaiting-ci",
            "decision": "fix the guard",
            "why": "the test already fails",
            "next": "watch round 3",
            "worktree": "/w/2251",
            "branch": "fix/2251",
            "base_sha": "abc123",
            "pr_number": 2271,
            "claim_comment_id": 9911,
            "ci_state": {"state": "running", "round": 3},
            "labels_applied": ["claimed"],
        },
        "ci",
        "opened the pull request",
    )
    item = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert item == result["item"]
    assert item["phase"] == "awaiting-ci"
    assert (item["decision"], item["why"], item["next"]) == (
        "fix the guard",
        "the test already fails",
        "watch round 3",
    )
    assert (item["worktree"], item["branch"], item["base_sha"]) == ("/w/2251", "fix/2251", "abc123")
    assert (item["pr_number"], item["claim_comment_id"]) == (2271, 9911)
    assert item["ci_state"] == {"state": "running", "round": 3}
    assert item["labels_applied"] == ["claimed"]
    assert item["claimed_at"] == item["last_progress_at"]
    assert item["finished_at"] is None and item["outcome"] is None
    line = result["event"]
    assert (line["kind"], line["text"], line["number"], line["phase"]) == (
        "ci",
        "opened the pull request",
        2251,
        "awaiting-ci",
    )
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["phase_lines"]["2251"] == [
        {"phase": "awaiting-ci", "at": line["ts"]}
    ]


def test_the_answer_is_the_line_the_log_holds(tmp_path):
    """The write answers with the entry AS THE FILE HOLDS IT, not an approximation.

    The line's id is content-addressed over its timestamp, and that timestamp comes
    off the log's own clock when the writer appends -- so an answer stamped on this
    side would name an id no reader could find. The writer is drained first and the
    landed entry read back, which ``durable`` reports.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    result = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert result["durable"] is True
    (stored,) = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
    assert stored == result["event"]
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) == result["item"]


def test_a_write_without_a_live_session_is_refused_by_name(tmp_path):
    """No unit, no record -- and the refusal names the condition, so the route can
    answer 409 ``crew_log_unavailable`` instead of a generic conflict."""
    crew = _crew(tmp_path)
    cid = crew["id"]
    with pytest.raises(cs.CrewLedgerUnavailable, match="no live session"):
        _record(tmp_path, cid, "", 7, {"phase": "claimed"}, "claim", "took #7")
    # A live session whose crew log does not exist yet (created on its first turn).
    with pytest.raises(cs.CrewLedgerUnavailable, match="first turn"):
        _record(tmp_path, cid, "acp-never-ran", 7, {"phase": "claimed"}, "claim", "took #7")
    with pytest.raises(cs.CrewLedgerUnavailable):
        cs.record_crew_checkpoint(OWNER, REPO, cid, "queue empty", tmp_path, session_id="")
    assert isinstance(cs.CrewLedgerUnavailable("x"), cs.CrewStoreError)


def test_a_crew_log_that_is_switched_off_refuses_the_write(tmp_path, monkeypatch):
    crew, sid = _live_crew(tmp_path)
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "0")
    crew_log_emit.reset_caches()
    with pytest.raises(cs.CrewLedgerUnavailable, match=crew_log_emit.CREW_LOG_ENV):
        _record(tmp_path, crew["id"], sid, 7, {"phase": "claimed"}, "claim", "took #7")


def test_an_update_that_cannot_fit_one_entry_is_refused_before_anything_is_appended(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")
    before = _entries(sid)
    with pytest.raises(cs.CrewLedgerEntryTooLarge):
        _item(tmp_path, cid, sid, 7, {"next": "x" * (100 * 1024)})
    assert _entries(sid) == before
    assert isinstance(cs.CrewLedgerEntryTooLarge("x"), cs.CrewStoreError)


def test_a_crew_with_no_crew_log_reads_as_the_empty_record(tmp_path):
    """The read path fails closed to NOTHING, never raises: it runs on every cycle."""
    crew = _crew(tmp_path)
    cid = crew["id"]
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path) == ()
    ledger = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert (ledger["items"], ledger["events"], ledger["skips"]) == ([], [], {})
    assert ledger["counts"] == {"open": 0, "evicted_items": 0, "evicted_skips": 0}
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []
    assert cs.read_events(OWNER, REPO, tmp_path) == []
    assert cs.read_skips(OWNER, REPO, tmp_path) == {}
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is False


def test_a_crew_folds_across_every_unit_its_slot_ran_under(tmp_path):
    """A slot owns one ACP session id at a time, so a crew that restarted has its
    record spread over units; a read that did not join them would answer with
    whichever part of the work happened to land in the newest session."""
    crew, first = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, first, 7, {"phase": "claimed", "next": "read it"}, "claim")
    _tick()
    second = _unit(cid)
    later = _item(tmp_path, cid, second, 7, {"phase": "implementing"})

    assert later["next"] == "read it", "the earlier unit's fields were not joined"
    assert later["phase"] == "implementing"
    assert [e["kind"] for e in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)] == [
        "implement",
        "claim",
    ]
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path) == (first, second)
    # The LIVE unit applies last whatever the header clocks say.
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path, live_session_id=first) == (second, first)
    # One entry per unit: the second write did not re-state the first unit's record.
    assert len(_entries(first)) == 1 and len(_entries(second)) == 1


def test_a_read_continued_from_its_checkpoint_equals_a_cold_fold(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    for number, phase in ((7, "claimed"), (9, "investigating"), (7, "implementing")):
        _item(tmp_path, cid, sid, number, {"phase": phase}, "claim")
        warm = cs.read_ledger(OWNER, REPO, cid, tmp_path)
        crew_log_projection.forget_slot_folds()
        assert cs.read_ledger(OWNER, REPO, cid, tmp_path) == warm


def test_the_clearable_fields_are_one_list_everywhere():
    """The entry type declares the list; the store re-exports it; validation spells
    it out for the tool schema's enum. Three spellings, one pin."""
    from kiro_crew import validation
    from kiro_crew.crew_log.entry_types import RADAR_CLEARABLE_FIELDS

    assert tuple(cs.CLEARABLE_FIELDS) == tuple(RADAR_CLEARABLE_FIELDS)
    assert frozenset(cs.CLEARABLE_FIELDS) == validation._ISSUE_RADAR_CREW_CLEARABLE_FIELDS
    assert "phase" not in cs.CLEARABLE_FIELDS, "a phase moves, it is never emptied"


def test_an_append_the_writer_refused_raises_instead_of_answering(tmp_path, monkeypatch):
    """The writer drains and the entry is not in the log: it was refused. A refusal
    is not a commit -- the write raises, the caller is told, and the fold shows
    nothing. Synthesizing an answer is reserved for a writer still holding the
    entry past the flush budget."""
    from kiro_crew.crew_log import emit as crew_log_emit

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", lambda *_a, **_k: None)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took it")
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []
    assert _entries(sid) == []


def test_a_log_gone_at_read_back_is_a_refusal_not_a_record(tmp_path, monkeypatch):
    """The writer drains, but the session's log cannot be opened when the entry is
    read back -- deleted under the write. The write cannot show the entry exists, so
    it raises rather than handing the caller a synthetic record; the synthetic
    answer is made only while the writer still holds the entry."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    projection = cs._projection()
    monkeypatch.setattr(projection, "open_session_log", lambda unit: None)  # the log vanished
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took it")


def test_a_read_back_that_fails_once_does_not_turn_a_landed_entry_into_a_refusal(
    tmp_path, monkeypatch
):
    """A transient failure reading the entry back is retried once; the second read
    finds the landed entry and the write answers it as durable."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    projection = cs._projection()
    real_open = projection.open_session_log
    real_recorded = crew_log_emit.on_radar_recorded
    failures = {"left": 0}

    def recorded(*a, **k):
        real_recorded(*a, **k)
        failures["left"] = 1  # armed AFTER the append: the next read is the read-back

    class _FlakyHandle:
        def __init__(self, inner):
            self._inner = inner

        def iter_from(self, *a, **k):
            if failures["left"]:
                failures["left"] -= 1
                raise OSError("transient")
            return self._inner.iter_from(*a, **k)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", recorded)
    monkeypatch.setattr(projection, "open_session_log", lambda unit: _FlakyHandle(real_open(unit)))
    res = _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took it")
    assert res["durable"] is True and failures["left"] == 0
    assert len(_entries(sid)) == 1


def test_an_explicit_null_clears_a_field_instead_of_being_dropped(tmp_path):
    """``pr_number: null`` has always meant "no pull request any more". A typed
    entry field cannot hold a null, so the writer carries the cleared names in
    ``clear`` and the fold empties them -- an omitted field still means unchanged."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(
        tmp_path,
        cid,
        sid,
        2251,
        {
            "pr_number": 2271,
            "claim_comment_id": 9911,
            "decision": "fix it",
            "ci_state": {"round": 3},
            "labels_applied": ["claimed"],
            "outcome": "merged",
        },
    )
    cleared = _item(
        tmp_path,
        cid,
        sid,
        2251,
        {
            "pr_number": None,
            "claim_comment_id": None,
            "decision": None,
            "ci_state": None,
            "labels_applied": None,
            "outcome": None,
        },
    )
    assert cleared["pr_number"] is None and cleared["claim_comment_id"] is None
    assert cleared["decision"] == ""
    assert cleared["ci_state"] == {} and cleared["labels_applied"] == []
    assert cleared["outcome"] is None
    # Durable: the fold re-reads the same answer.
    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)["pr_number"] is None
    # Carried as names, not as nulls -- the entry type refuses a typed null.
    last = _entries(sid)[-1].data
    assert sorted(last["clear"]) == sorted(
        ["pr_number", "claim_comment_id", "decision", "ci_state", "labels_applied", "outcome"]
    )
    assert "pr_number" not in last
    # An update that names nothing to clear carries no ``clear`` at all.
    _item(tmp_path, cid, sid, 2251, {"next": "rebase"})
    assert "clear" not in _entries(sid)[-1].data
    # A clear and a set in ONE update: the set wins.
    both = _item(tmp_path, cid, sid, 2251, {"pr_number": 2300})
    assert both["pr_number"] == 2300


def test_two_updates_with_the_same_line_but_different_fields_both_fold(tmp_path):
    """The collapse of a repeated entry keys on the whole update, not on the line's
    display identity: two same-millisecond calls with equal event text but
    different patches must both apply, while an exact repeat applies once."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    def entry(seq, data):
        return Entry(
            type=cs.LEDGER_ENTRY_TYPE, seq=seq, time=1_789_000_000_000, src="gateway", data=data
        )

    base = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "progress",
        "event_kind": "ci",
    }
    first = {**base, "pr_number": 100}
    second = {**base, "next": "watch round 2"}  # same ms, same text, other fields
    value = crew_log.fold("radar", [entry(1, first), entry(2, second), entry(3, dict(second))])
    (item,) = value["items"]
    assert item["pr_number"] == 100, "the first update folded"
    assert item["next"] == "watch round 2", "the second update folded too"
    # The exact repeat (entry 3) was applied once: one line for the two identical ones.
    assert len(value["events"]) == 2
    # And a repeated ``tried`` is the tell that a repeat was applied twice.
    tried = {**base, "tried": {"approach": "hasattr guard", "rejected_because": "loses the ACL"}}
    value = crew_log.fold("radar", [entry(1, tried), entry(2, dict(tried))])
    assert [t["approach"] for t in value["items"][0]["tried"]] == ["hasattr guard"]


def test_a_retried_update_stamped_later_is_still_applied_once():
    """A write whose append landed but whose read-back was refused is answered 503 and
    sent again; the retry is the same update stamped seconds later. Its line id
    carries the stamp, so the repeat check keys on the payload alone: the update and
    its event apply once, and a re-applied ``tried`` does not list twice."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    data = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "pywin32 is out",
        "event_kind": "implement",
        "tried": {"approach": "pywin32", "rejected_because": "no wheel"},
    }
    first = Entry(
        type=cs.LEDGER_ENTRY_TYPE, seq=1, time=1_789_000_000_000, src="gateway", data=data
    )
    retry = Entry(
        type=cs.LEDGER_ENTRY_TYPE, seq=2, time=1_789_000_007_000, src="gateway", data=dict(data)
    )
    value = crew_log.fold("radar", [first, retry])
    assert len(value["events"]) == 1, "one line for the update and its retry"
    assert [t["approach"] for t in value["items"][0]["tried"]] == ["pywin32"]


def test_an_identical_update_after_an_intervening_one_is_a_new_update():
    """The repeat check compares against the item's LAST applied update only. An
    item that returns to an earlier state with identical fields after other updates
    is recording a new transition, and it applies -- a history-wide check would
    silently drop it. A retry is the next update for its item, so the narrow check
    still catches it."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "number": 7}
    blocked = {**base, "phase": "awaiting-ci", "event": "waiting on CI", "event_kind": "ci"}
    working = {**base, "phase": "implementing", "event": "CI green, back on it", "event_kind": "ci"}

    def entry(seq, data):
        return Entry(
            type=cs.LEDGER_ENTRY_TYPE,
            seq=seq,
            time=1_789_000_000_000 + seq * 1000,
            src="gateway",
            data=dict(data),
        )

    value = crew_log.fold("radar", [entry(1, blocked), entry(2, working), entry(3, blocked)])
    (item,) = value["items"]
    assert item["phase"] == "awaiting-ci", "the return to awaiting-ci applied"
    assert [e["text"] for e in value["events"]] == [
        "waiting on CI",
        "CI green, back on it",
        "waiting on CI",
    ]
    # And a retry of that last update (same payload, next in line) is still one update.
    value = crew_log.fold(
        "radar", [entry(1, blocked), entry(2, working), entry(3, blocked), entry(4, blocked)]
    )
    assert len(value["events"]) == 3
