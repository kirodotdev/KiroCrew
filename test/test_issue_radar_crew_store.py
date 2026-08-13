"""Tests for the crew store — records, work items, the event ledger and the
shared skip index.

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
  * **Ledger dedupe.** Duplicate lines merge on read, so an append retried after a
    crash does not double-report.
  * **The skip index is repo-wide and first-writer-wins.** It is one file every
    crew writes, so it must take the repo-wide lock — under a per-crew lock two
    crews would each read an index that predates the other and drop a decision,
    and the dropped issue silently goes back to being re-investigated by everyone.
    Behavioural assertions cannot detect that: sequential calls see each other
    whatever lock is held, so the lock test asserts WHICH file is locked.

Every test runs against a ``tmp_path`` root — the store threads ``root`` through
every function for exactly this reason, so nothing here touches a real data home.
"""

import json
import math
import os
import threading
from contextlib import contextmanager

import pytest

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs

OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name


def _crew(root, name="Andromeda", **spec):
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


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


def test_duplicate_name_is_refused(tmp_path):
    _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="already taken"):
        _crew(tmp_path)


@pytest.mark.parametrize(
    "bad_id",
    [
        "/etc/policy",                 # absolute: pathlib DISCARDS the base
        "../../../../etc/policy",      # relative traversal
        "c_1234abcd/../../escape",     # valid prefix, then escapes
        "C_1234ABCD",                  # generator emits lowercase hex only
        "c_12345678901234",            # right prefix, wrong length
        "c_zzzzzzzz",                  # right shape, not hex
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
    assert cs.crews_dir(OWNER, REPO, tmp_path) in cs.crew_path(
        OWNER, REPO, crew["id"], tmp_path
    ).parents


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

    assert locked == ["repo-wide"], (
        f"a rename must take only the repo-wide record lock, took {locked}"
    )
    assert sorted(c["name"] for c in cs.list_crews(OWNER, REPO, tmp_path)) == [
        "Bode", "Cocoon",
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
    stored = cs.write_settings(OWNER, REPO, {"needs_human_label": "  needs: maintainer  "}, tmp_path)
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


# ── work items ──────────────────────────────────────────────────────────────


def test_upsert_stamps_claimed_at_and_merges_per_field(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    first = cs.upsert_work_item(
        OWNER, REPO, cid, 2251, {"phase": "claimed", "next": "read the call sites"}, tmp_path
    )
    assert first["claimed_at"]
    assert first["next"] == "read the call sites"

    # A patch carrying only `decision` must preserve `next`.
    second = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"decision": "fix it"}, tmp_path)
    assert second["decision"] == "fix it"
    assert second["next"] == "read the call sites"
    assert second["claimed_at"] == first["claimed_at"]


def test_the_stored_text_is_exactly_the_serialisation_of_the_returned_record(tmp_path):
    """``serialize_work_item(returned)`` == the bytes on disk.

    One serialisation, pinned from both ends, so a record and the text standing for
    it cannot drift: the rollback in ``commit_work_progress`` restores a snapshot of
    this text, and a second copy of the format is how such a restore silently starts
    writing something a reader coerces differently. Read literally — ``newline=""``
    and utf-8 — because the property is byte equality, and universal-newline
    translation on either side would hide a real mismatch on Windows.
    """
    crew = _crew(tmp_path)
    for patch in (
        {"phase": "claimed", "next": "read the call sites"},
        {"phase": "implementing", "why": "one-line fix", "tried_approach": "reverting"},
        {},
    ):
        item = cs.upsert_work_item(OWNER, REPO, crew["id"], 2251, patch, tmp_path)
        path = cs.work_item_path(OWNER, REPO, crew["id"], 2251, tmp_path)
        with path.open("r", encoding="utf-8", newline="") as fh:
            assert fh.read() == cs.serialize_work_item(item)


def _backdate(tmp_path, cid, number, stamp="2020-01-01T00:00:00Z"):
    """Force a stale ``last_progress_at`` on disk.

    Without this the stamp assertions below are TOOTHLESS: ``_now_iso`` has
    one-second resolution, so two writes inside the same second produce an equal
    stamp whether the code guards it or not, and the "did not renew" test would
    pass even with the guard deleted.
    """
    path = cs.work_item_path(OWNER, REPO, cid, number, tmp_path)
    rec = json.loads(path.read_text())
    rec["last_progress_at"] = stamp
    path.write_text(json.dumps(rec))
    return stamp


def test_no_op_write_does_not_renew_the_claim(tmp_path):
    """The TTL is measured from ``last_progress_at``. A write that carries no
    progress must leave it alone, or a dead crew holds its claim forever."""
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)
    stale = _backdate(tmp_path, cid, 2251)

    assert cs.upsert_work_item(OWNER, REPO, cid, 2251, {}, tmp_path)["last_progress_at"] == stale
    # Re-asserting the SAME phase is not progress either.
    again = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)
    assert again["last_progress_at"] == stale
    # Nor is a field that carries no new information.
    same_next = cs.upsert_work_item(OWNER, REPO, cid, 2251, {"next": ""}, tmp_path)
    assert same_next["last_progress_at"] == stale


def test_real_progress_moves_the_stamp(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "claimed"}, tmp_path)

    for patch in (
        {"phase": "implementing"},
        {"next": "add the Windows branch"},
        {"pr_number": 2271},
        {"ci_state": {"state": "running", "round": 3}},
        {"tried_approach": "pywin32"},
        {"phase": "skipped"},
    ):
        stale = _backdate(tmp_path, cid, 2251)
        got = cs.upsert_work_item(OWNER, REPO, cid, 2251, patch, tmp_path)
        assert got["last_progress_at"] != stale, f"{patch} should count as progress"


def test_second_editing_item_is_refused(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    with pytest.raises(cs.CrewStoreError, match="already editing"):
        cs.upsert_work_item(OWNER, REPO, cid, 2264, {"phase": "implementing"}, tmp_path)


def test_editing_slot_frees_when_the_first_item_parks(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "awaiting-ci"}, tmp_path)
    other = cs.upsert_work_item(OWNER, REPO, cid, 2264, {"phase": "implementing"}, tmp_path)
    assert other["phase"] == "implementing"


def test_staying_in_an_editing_phase_is_not_a_second_editor(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 2251, {"phase": "implementing"}, tmp_path)
    again = cs.upsert_work_item(
        OWNER, REPO, cid, 2251, {"phase": "implementing", "next": "keep going"}, tmp_path
    )
    assert again["next"] == "keep going"


def test_unknown_phase_is_refused(tmp_path):
    crew = _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown phase"):
        cs.upsert_work_item(OWNER, REPO, crew["id"], 1, {"phase": "vibing"}, tmp_path)


def test_every_unfinished_item_consumes_a_slot(tmp_path):
    """No phase is exempt any more. A crew that needs a human records a PASS, which
    is terminal and frees the slot — it does not sit on one holding the issue."""
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "awaiting-ci"}, tmp_path)
    cs.upsert_work_item(OWNER, REPO, cid, 2, {"phase": "awaiting-reply"}, tmp_path)
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 2
    cs.upsert_work_item(OWNER, REPO, cid, 2, {"phase": "skipped"}, tmp_path)
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1


def test_escalated_is_no_longer_a_phase(tmp_path):
    """The store is the choke point every writer passes through, so refusing it here
    is what stops a stale caller — an older nudge, another installation's marker, a
    crew resuming from a pre-change ledger — parking an issue on a human again."""
    crew = _crew(tmp_path)
    assert "escalated" not in cs.PHASES
    with pytest.raises(cs.CrewStoreError, match="unknown phase"):
        cs.upsert_work_item(OWNER, REPO, crew["id"], 1, {"phase": "escalated"}, tmp_path)


def test_escalate_is_no_longer_an_event_kind(tmp_path):
    crew = _crew(tmp_path)
    assert "escalate" not in cs.EVENT_KINDS
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        cs.append_event(OWNER, REPO, crew["id"], 1, "escalate", "asking the owner", tmp_path)


def test_a_stale_escalation_payload_is_dropped_rather_than_stored(tmp_path):
    """The record is assembled field by field, so an unknown key in a patch is
    dropped — the same discipline as every other unknown field.

    Pinned because the write is a MERGE: if the field came back, a crew resuming from
    a pre-change ledger would carry an unanswerable question on its work item, and
    the surface would have something to render a "waiting on a human" card from.
    """
    crew = _crew(tmp_path)
    item = cs.upsert_work_item(
        OWNER, REPO, crew["id"], 1,
        {"phase": "claimed", "escalation": {"question": "which behaviour?"}},
        tmp_path,
    )
    assert "escalation" not in item
    stored = json.loads(cs.work_item_path(OWNER, REPO, crew["id"], 1, tmp_path).read_text())
    assert "escalation" not in stored


def test_terminal_phase_frees_the_slot_and_stamps_finished(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "implementing"}, tmp_path)
    done = cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "resolved"}, tmp_path)
    assert done["finished_at"]
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_reopening_clears_the_finish_stamp_so_the_next_one_counts(tmp_path):
    """A reopened issue's SECOND resolution is stamped at its own time.

    REGRESSION: `finished_at` was written only when it was empty, so it recorded
    the FIRST time this item ever went terminal and nothing cleared it. An issue
    that was resolved, reopened and handled again by the same crew reuses this
    item, so it stayed stamped in the past — putting the new resolution outside
    the `resolved24h` window and under-reporting work the crew had just done.
    """
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "implementing"}, tmp_path)
    first = cs.upsert_work_item(
        OWNER, REPO, cid, 1, {"phase": "resolved", "outcome": "merged in #9"}, tmp_path
    )
    first_stamp = first["finished_at"]
    assert first_stamp
    assert first["outcome"] == "merged in #9"

    # Reopened: back to a live phase. NO field describing a finished result may
    # survive, or the item reads as finished while it is demonstrably being worked
    # again. `outcome` is asserted here as well as `finished_at` because the two
    # were reported as separate defects — clearing one and keeping the other still
    # leaves the ledger asserting a terminal result on active work.
    live = cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "implementing"}, tmp_path)
    assert live["finished_at"] is None, "a reopened item still claims it finished"
    assert live["outcome"] is None, "a reopened item still carries its old outcome"
    # Durable, not just in the returned dict — the stat card reads the file.
    stored = json.loads(cs.work_item_path(OWNER, REPO, cid, 1, tmp_path).read_text())
    assert stored["finished_at"] is None
    assert stored["outcome"] is None

    # The crew's memory of what it already ruled out must NOT be cleared with it:
    # losing that makes it retry approaches it had rejected.
    assert live["next"] == first["next"]

    again = cs.upsert_work_item(OWNER, REPO, cid, 1, {"phase": "resolved"}, tmp_path)
    assert again["finished_at"], "the second resolution was never stamped"
    assert again["finished_at"] >= first_stamp


def test_tried_entries_append_rather_than_replace(tmp_path):
    crew = _crew(tmp_path)
    cid = crew["id"]
    cs.upsert_work_item(
        OWNER, REPO, cid, 1,
        {"tried_approach": "hasattr guard", "tried_rejected_because": "loses the ACL"},
        tmp_path,
    )
    second = cs.upsert_work_item(OWNER, REPO, cid, 1, {"tried_approach": "pywin32"}, tmp_path)
    assert [t["approach"] for t in second["tried"]] == ["hasattr guard", "pywin32"]
    assert second["tried"][0]["rejected_because"] == "loses the ACL"


def test_work_items_are_scoped_per_crew(tmp_path):
    a = _crew(tmp_path, name="Andromeda")
    b = _crew(tmp_path, name="Whirlpool")
    cs.upsert_work_item(OWNER, REPO, a["id"], 2251, {"phase": "implementing"}, tmp_path)
    # Same issue number, different crew — must not collide, and must not trip the
    # one-editor rule, which is per crew.
    cs.upsert_work_item(OWNER, REPO, b["id"], 2251, {"phase": "implementing"}, tmp_path)
    assert cs.read_work_item(OWNER, REPO, a["id"], 2251, tmp_path)["crew_id"] == a["id"]
    assert cs.read_work_item(OWNER, REPO, b["id"], 2251, tmp_path)["crew_id"] == b["id"]


# ── event ledger ────────────────────────────────────────────────────────────


def test_events_read_newest_first_and_filter_by_crew(tmp_path):
    a = _crew(tmp_path, name="Andromeda")
    b = _crew(tmp_path, name="Whirlpool")
    cs.append_event(OWNER, REPO, a["id"], 1, "claim", "claimed", tmp_path)
    cs.append_event(OWNER, REPO, b["id"], 2, "ci", "CI round 3", tmp_path)
    all_events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in all_events] == ["ci", "claim"]
    mine = cs.read_events(OWNER, REPO, tmp_path, crew_id=a["id"])
    assert [e["kind"] for e in mine] == ["claim"]


def test_duplicate_event_lines_collapse_on_read(tmp_path):
    crew = _crew(tmp_path)
    entry = cs.append_event(OWNER, REPO, crew["id"], 1, "claim", "claimed", tmp_path)
    # Simulate an append retried after a crash: the same content-addressed id.
    with open(cs.events_path(OWNER, REPO, tmp_path), "a", encoding="utf-8") as fd:
        fd.write(json.dumps(entry) + "\n")
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1


def test_malformed_line_does_not_hide_the_history_before_it(tmp_path):
    crew = _crew(tmp_path)
    cs.append_event(OWNER, REPO, crew["id"], 1, "claim", "claimed", tmp_path)
    with open(cs.events_path(OWNER, REPO, tmp_path), "a", encoding="utf-8") as fd:
        fd.write("{ this is a torn tail\n")
    events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in events] == ["claim"]


def test_unknown_event_kind_is_refused(tmp_path):
    crew = _crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        cs.append_event(OWNER, REPO, crew["id"], 1, "vibes", "…", tmp_path)


# ── phase classification ────────────────────────────────────────────────────


def test_the_two_phase_classifications_do_not_coincide(tmp_path):
    """This is the point of keeping two separate sets rather than a flag."""
    # awaiting-ci: occupies a slot, is NOT ttl-active, is NOT editing.
    assert "awaiting-ci" not in cs.TTL_ACTIVE_PHASES
    assert "awaiting-ci" not in cs.EDITING_PHASES
    assert "awaiting-ci" not in cs.TERMINAL_PHASES
    # addressing-review: editing, but NOT ttl-active — an open pull request stands
    # in for a heartbeat, so the claim does not age against it.
    assert "addressing-review" in cs.EDITING_PHASES
    assert "addressing-review" not in cs.TTL_ACTIVE_PHASES
    # implementing: both ttl-active and editing, and slot-occupying.
    assert "implementing" in cs.TTL_ACTIVE_PHASES
    assert "implementing" in cs.EDITING_PHASES


# ── shared skip index ───────────────────────────────────────────────────────


def test_a_skip_is_readable_by_number_and_by_predicate(tmp_path):
    crew = _crew(tmp_path)
    entry, created = cs.record_skip(
        OWNER, REPO, 42, "needs an owner decision on the data model",
        "needs-design", crew["id"], tmp_path,
    )
    assert created is True
    assert entry["number"] == 42
    assert entry["scope"] == "needs-design"
    assert entry["crew_id"] == crew["id"]
    assert entry["decided_at"]
    # Keyed by the STRING form, because that is what a JSON object key is — a
    # reader that looked up the int would miss every entry.
    assert set(cs.read_skips(OWNER, REPO, tmp_path)) == {"42"}
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is True
    assert cs.is_skipped(OWNER, REPO, 43, tmp_path) is False


def test_re_skipping_keeps_the_first_crews_reason(tmp_path):
    """First-writer-wins, because the first reason is the audit trail.

    Two crews reaching the same pass is normal; the record a human reads when
    asking why an issue keeps being passed over must be the one that was actually
    decided first, not whichever crew wrote most recently.
    """
    first = _crew(tmp_path, "Andromeda")
    second = _crew(tmp_path, "Whirlpool")
    cs.record_skip(OWNER, REPO, 42, "first reason", "architecture", first["id"], tmp_path)
    returned, created = cs.record_skip(
        OWNER, REPO, 42, "second reason", "duplicate", second["id"], tmp_path
    )
    # The first element is what now STANDS, so the second crew can see its own
    # reason was not the one kept — and `created` says the write was a no-op.
    assert created is False
    assert returned["reason"] == "first reason"
    assert returned["scope"] == "architecture"
    assert returned["crew_id"] == first["id"]
    stored = cs.read_skips(OWNER, REPO, tmp_path)
    assert list(stored) == ["42"]
    assert stored["42"]["reason"] == "first reason"
    assert stored["42"]["crew_id"] == first["id"]


@pytest.mark.parametrize("scope", ["needs-decision", "needs-investigation"])
def test_a_needs_human_pass_is_accepted_and_indexed(tmp_path, scope):
    """The two scopes that replace holding an issue for a human.

    They have to survive :func:`coerce_skip_scope` verbatim rather than land in
    ``other``: the scope is how the recent-skip list — and a person reading the
    index — tells "this fleet does not do architecture work" from "somebody needs to
    answer a question", and collapsing them loses the only signal that says a human
    owes something back.
    """
    crew = _crew(tmp_path)
    assert scope in cs.SKIP_SCOPES
    assert cs.coerce_skip_scope(scope) == scope
    entry, _created = cs.record_skip(
        OWNER, REPO, 42, "needs the owner's call", scope, crew["id"], tmp_path
    )
    assert entry["scope"] == scope
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["scope"] == scope
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is True
    assert cs.recent_skips(OWNER, REPO, tmp_path)[0]["scope"] == scope


@pytest.mark.parametrize(
    "given", ["needs_design", "NEEDS-DESIGN-ISH", "", None, "vibes", 7]
)
def test_an_unknown_scope_coerces_to_other(tmp_path, given):
    """Coerced, never refused.

    Refusing would cost the whole skip record for a bad filter label, and a pass
    that fails to record is the exact duplicated investigation this index removes.
    """
    crew = _crew(tmp_path)
    entry, _created = cs.record_skip(OWNER, REPO, 42, "why", given, crew["id"], tmp_path)
    assert entry["scope"] == "other"
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["scope"] == "other"


def test_a_known_scope_survives_case_and_padding(tmp_path):
    crew = _crew(tmp_path)
    entry, _created = cs.record_skip(
        OWNER, REPO, 42, "why", "  Already-Fixed ", crew["id"], tmp_path
    )
    assert entry["scope"] == "already-fixed"


def test_record_skip_reports_whether_this_call_created_the_entry(tmp_path):
    """``created`` is decided under the lock, and it is the only honest source.

    A caller cannot work this out for itself, and that is the point: the index is
    repo-wide and first-writer-wins, so two crews passing on one number with the
    SAME reason and scope produce a standing entry that both of them would
    recognise as their own. Matching the stored fields against what was supplied —
    or pre-reading the index and finding the number absent — says "I wrote this" to
    both of them, and the loser then un-indexes the winner's committed decision when
    its own request fails. Only the call that actually inserted the entry may report
    creation.
    """
    author = _crew(tmp_path, "Andromeda")
    other = _crew(tmp_path, "Whirlpool")

    entry, created = cs.record_skip(
        OWNER, REPO, 42, "not reproducible on main", "not-reproducible", author["id"], tmp_path
    )
    assert created is True

    # Byte-identical arguments, and still not this call's entry to claim.
    again, created_again = cs.record_skip(
        OWNER, REPO, 42, "not reproducible on main", "not-reproducible", author["id"], tmp_path
    )
    assert created_again is False
    assert again == entry

    # A different crew reaching the same conclusion is the same answer.
    _third, created_third = cs.record_skip(
        OWNER, REPO, 42, "not reproducible on main", "not-reproducible", other["id"], tmp_path
    )
    assert created_third is False

    # A number nobody has passed on yet is a creation again, so the flag tracks the
    # write rather than being pinned False after the first call.
    _fresh, created_fresh = cs.record_skip(
        OWNER, REPO, 43, "duplicate of #42", "duplicate", author["id"], tmp_path
    )
    assert created_fresh is True


def test_recent_skips_are_newest_first_and_bounded(tmp_path):
    crew = _crew(tmp_path)
    for number in range(1, 6):
        cs.record_skip(OWNER, REPO, number, f"reason {number}", "other", crew["id"], tmp_path)
    rows = cs.recent_skips(OWNER, REPO, tmp_path, limit=3)
    assert [r["number"] for r in rows] == [5, 4, 3]


def test_a_malformed_index_reads_as_empty_rather_than_raising(tmp_path):
    # Consulted on the path where a crew decides whether to investigate: a torn
    # file must cost one wasted investigation, not stop the crew.
    _crew(tmp_path)
    cs.skips_path(OWNER, REPO, tmp_path).write_text("{ not json")
    assert cs.read_skips(OWNER, REPO, tmp_path) == {}
    assert cs.is_skipped(OWNER, REPO, 42, tmp_path) is False


def test_an_entry_whose_number_was_stored_as_a_string_still_reads_back_as_an_int(tmp_path):
    # `skipped_numbers` is built from this field, so a string here would drop the
    # issue out of the membership test every crew runs before investigating.
    _crew(tmp_path)
    cs.skips_path(OWNER, REPO, tmp_path).write_text(
        json.dumps({"42": {"number": "42", "reason": "r", "scope": "other", "crew_id": "c"}})
    )
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["number"] == 42


def _inode(path):
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


def test_the_skip_index_write_takes_the_repo_wide_lock(tmp_path, monkeypatch):
    """WHICH file is locked, not merely that the write works.

    A behavioural assertion cannot detect the wrong lock here: two sequential
    ``record_skip`` calls in one test see each other's writes whatever lock is
    held, so a per-crew lock passes every functional test and only loses a
    decision under real concurrency. So this spies on the fd handed to
    ``file_lock`` and identifies the file by inode.
    """
    crew = _crew(tmp_path)
    real_lock = cs.platform_compat.file_lock
    locked: list[int] = []

    @contextmanager
    def _spy(fd, exclusive=False):
        locked.append(os.fstat(fd).st_ino)
        with real_lock(fd, exclusive=exclusive):
            yield

    monkeypatch.setattr(cs.platform_compat, "file_lock", _spy)
    cs.record_skip(OWNER, REPO, 42, "why", "other", crew["id"], tmp_path)

    records_lock = _inode(cs._records_lock_path(OWNER, REPO, tmp_path))
    assert records_lock is not None
    # Exactly the repo-wide lock: not the crew's, and not nothing.
    assert locked == [records_lock]
    assert _inode(cs._crew_lock_path(OWNER, REPO, crew["id"], tmp_path)) not in locked


# ── one crew's pass cannot be erased by another crew's rollback ─────────────
#
# `record_skip` taking the repo-wide lock makes ONE write of the index atomic. It
# does NOT make a crew's OWNERSHIP of an entry outlive that write, and
# `commit_work_progress` needs exactly that: it may un-index the entry later, when
# its ledger append fails.
#
# The crew lock cannot supply it. The index is repo-wide and the crew lock is
# per-crew, so two crews passing on ONE issue hold two DIFFERENT crew locks and are
# serialised against each other by nothing: the second finds the first's entry,
# reports no creation of its own, commits its item and its ledger line against it,
# and the first's rollback then deletes it. The fleet is left with an issue that
# reads as un-passed while a crew's own item and log say it passed on it — so every
# crew re-investigates an issue somebody decided about, which is the most expensive
# mistake this store can make.
#
# So `commit_work_progress` holds `_skip_lock_path` for the ONE issue number from
# before the index write until after the rollback. Per-number and not repo-wide
# because only two transactions contending for the SAME entry can do this to each
# other; the tests below assert both halves of that — the same number blocks, a
# different number does not.

#: Every wait is bounded, so a lock that cannot be got FAILS a test rather than
#: hanging it.
_JOIN_TIMEOUT = 15.0

#: How long a write that is expected to be BLOCKED is given to prove it is not.
#: A pass that is not serialised lands in milliseconds, so this is three orders of
#: magnitude of headroom, and it is only ever paid on the assertion path.
_BLOCKED_FOR = 0.5

#: How long a write that is expected to go STRAIGHT THROUGH is given to land.
_UNBLOCKED_WITHIN = 5.0

#: How long a parked transaction waits to be released. It MUST exceed every probe
#: budget above: a park that expires on its own commits, drops the lock, and lets a
#: probe that was genuinely blocked complete just inside its own deadline — which
#: passes the test against the very bug it is there to catch. Proven, not guessed:
#: with both budgets at 15s a repo-wide mutant of the hold passed this file.
_PARK_FOR = 60.0


def _pass_body(**over):
    """The arguments for one crew's pass, so two calls can be made byte-identical."""
    body = {
        "patch": {"phase": "skipped"},
        "event_kind": "skip",
        "event_text": "passing on it",
        "skip_reason": "needs an owner decision",
        "skip_scope": "needs-decision",
    }
    body.update(over)
    return body


def _pass(root, crew_id, number, **over):
    body = _pass_body(**over)
    return cs.commit_work_progress(
        OWNER, REPO, crew_id, number,
        body["patch"], body["event_kind"], body["event_text"],
        skip_reason=body["skip_reason"], skip_scope=body["skip_scope"], root=root,
    )


def test_a_lock_that_cannot_be_acquired_still_rolls_the_item_back(tmp_path, monkeypatch):
    """Acquiring the skip lock is a step, so its failure must roll back like one.

    REGRESSION: the acquisition sat OUTSIDE the rollback-protected ``try``. By then
    the work item is already written, so an exception here — the lock is one more
    open descriptor, and fd exhaustion or a permission fault raises — escaped past
    the rollback and left the item changed with neither its skip-index entry nor its
    ledger event. That torn state is the single outcome this transaction exists to
    prevent, and it was reachable without any concurrency at all.

    Asserted on the DURABLE item, because the rollback's whole purpose is what the
    next reader sees on disk.
    """
    crew = _crew(tmp_path, "Andromeda")
    cid = crew["id"]
    cs.upsert_work_item(OWNER, REPO, cid, 7, {"phase": "investigating"}, tmp_path)

    def _cannot_open_another_descriptor(owner, repo, number, root=None):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(cs, "_skip_lock", _cannot_open_another_descriptor)

    with pytest.raises(OSError):
        cs.commit_work_progress(
            OWNER, REPO, cid, 7,
            {"phase": "skipped"},
            skip_reason="needs a design decision",
            skip_scope="needs-design",
            event_kind="skip",
            event_text="passed on #7",
            root=tmp_path,
        )

    # The phase must be what it was, not the half-applied "skipped".
    item = json.loads(cs.work_item_path(OWNER, REPO, cid, 7, tmp_path).read_text())
    assert item["phase"] == "investigating", (
        "the item kept a skipped phase whose index entry and ledger line never landed"
    )
    # And nothing partial anywhere else: no index entry, no event.
    assert 7 not in cs.read_skips(OWNER, REPO, tmp_path).get("skipped_numbers", [])


def test_a_rollback_cannot_erase_a_pass_another_crew_committed_on_the_same_issue(
    tmp_path, monkeypatch
):
    """The interleaving in full: two DIFFERENT crews, ONE issue, first ledger fails.

    The author indexes #7 and then fails at its ledger line. Whirlpool passes on the
    SAME issue inside that window. Under a per-crew hold Whirlpool's ``record_skip``
    finds the author's entry, reports no creation, and its item and ledger line
    commit against it — and then the author's rollback deletes that entry, because
    the entry is still byte-for-byte the one the author inserted. Nothing readable
    distinguishes an adopter from an absence, so no comparison can catch this; only
    making Whirlpool WAIT can.

    Asserts the surviving entry is the COMMITTED one and that it is still there.
    Both halves matter: an empty index is the bug, and an index still naming the
    crew whose transaction failed would be a different one.
    """
    author = _crew(tmp_path, "Andromeda")
    other = _crew(tmp_path, "Whirlpool")
    real_append = cs.append_event
    indexed = threading.Event()
    other_committed = threading.Event()

    def _fail_only_the_authors_ledger_line(owner, repo, crew_id, number, kind, text, root=None):
        if crew_id != author["id"]:
            return real_append(owner, repo, crew_id, number, kind, text, root)
        indexed.set()
        # Bounded: with the hold in place the other crew CANNOT commit here, so this
        # times out and the transaction goes on to fail either way.
        other_committed.wait(timeout=_BLOCKED_FOR)
        raise OSError("no space left on device")

    monkeypatch.setattr(cs, "append_event", _fail_only_the_authors_ledger_line)

    def _the_other_crews_pass():
        # Only after the author's entry is in the index, so this is strictly the
        # second writer — the position the bug needs.
        if not indexed.wait(timeout=_JOIN_TIMEOUT):
            return
        _pass(tmp_path, other["id"], 7, skip_reason="already fixed upstream",
              skip_scope="already-fixed", event_text="passing on #7")
        other_committed.set()

    thread = threading.Thread(target=_the_other_crews_pass, daemon=True)
    thread.start()
    with pytest.raises(OSError):
        _pass(tmp_path, author["id"], 7, event_text="passing on #7")
    thread.join(timeout=_JOIN_TIMEOUT)
    assert not thread.is_alive(), "the other crew's pass never completed"

    standing = cs.read_skips(OWNER, REPO, tmp_path)
    assert list(standing) == ["7"], "the committed pass was un-indexed by the failed one"
    assert standing["7"]["crew_id"] == other["id"]
    assert standing["7"]["reason"] == "already fixed upstream"
    # And it really did COMMIT — the entry above is backed by an item and a line.
    item = cs.read_work_item(OWNER, REPO, other["id"], 7, tmp_path)
    assert item is not None and item["phase"] == "skipped"
    assert [e["crew_id"] for e in cs.read_events(OWNER, REPO, tmp_path)] == [other["id"]]
    # The failed transaction left nothing of its own behind.
    assert cs.read_work_item(OWNER, REPO, author["id"], 7, tmp_path) is None


def test_a_failed_pass_does_not_erase_the_entry_it_adopted(tmp_path, monkeypatch):
    """A re-skip that fails must leave the FIRST crew's entry alone.

    The mirror of the test above, and the reason ``created`` gates the un-index at
    all. Both calls supply byte-identical reason and scope, so the standing entry
    matches what the second crew would have written and a field comparison reads as
    "mine"; only the flag ``record_skip`` returns from inside its own lock says
    otherwise.
    """
    author = _crew(tmp_path, "Andromeda")
    other = _crew(tmp_path, "Whirlpool")
    _pass(tmp_path, author["id"], 7)

    monkeypatch.setattr(
        cs, "append_event",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
    )
    with pytest.raises(OSError):
        _pass(tmp_path, other["id"], 7)   # byte-identical arguments

    standing = cs.read_skips(OWNER, REPO, tmp_path)
    assert list(standing) == ["7"], "a re-skip's rollback removed somebody else's entry"
    assert standing["7"]["crew_id"] == author["id"]
    assert standing["7"]["reason"] == "needs an owner decision"
    assert cs.read_work_item(OWNER, REPO, other["id"], 7, tmp_path) is None


def test_the_skip_hold_spans_the_ledger_write_and_is_scoped_to_the_one_issue(
    tmp_path, monkeypatch
):
    """Both halves of the hold, from the outside: same issue blocks, another does not.

    The outcome alone cannot show either one — a sequential pair of passes sees each
    other's writes whatever is held — so this parks a transaction at its ledger write
    and drives two competing passes from the test's own threads.

    A pass on the SAME issue must not be able to commit, because the parked
    transaction can still withdraw the entry it would commit against. A pass on a
    DIFFERENT issue must go straight through: it contends for no entry this
    transaction owns, and parking it would put every crew in the repo behind one
    transaction's slowest write.
    """
    author = _crew(tmp_path, "Andromeda")
    other = _crew(tmp_path, "Whirlpool")
    third = _crew(tmp_path, "Bode")
    real_append = cs.append_event
    inside = threading.Event()
    release = threading.Event()

    def _park_at_the_ledger_write(owner, repo, crew_id, number, kind, text, root=None):
        if crew_id != author["id"]:
            return real_append(owner, repo, crew_id, number, kind, text, root)
        inside.set()
        release.wait(timeout=_PARK_FOR)
        return real_append(owner, repo, crew_id, number, kind, text, root)

    monkeypatch.setattr(cs, "append_event", _park_at_the_ledger_write)

    def _probe(crew_id, number, started, done):
        started.set()
        _pass(tmp_path, crew_id, number, event_text=f"passing on #{number}")
        done.set()

    same = {"started": threading.Event(), "done": threading.Event()}
    elsewhere = {"started": threading.Event(), "done": threading.Event()}
    parked = threading.Thread(
        target=lambda: _pass(tmp_path, author["id"], 7, event_text="passing on #7"),
        daemon=True,
    )
    parked.start()
    assert inside.wait(timeout=_JOIN_TIMEOUT), "the transaction never reached its ledger write"

    same_thread = threading.Thread(
        target=_probe, args=(other["id"], 7, same["started"], same["done"]), daemon=True
    )
    elsewhere_thread = threading.Thread(
        target=_probe, args=(third["id"], 99, elsewhere["started"], elsewhere["done"]),
        daemon=True,
    )
    try:
        same_thread.start()
        assert same["started"].wait(timeout=_JOIN_TIMEOUT)
        assert not same["done"].wait(timeout=_BLOCKED_FOR), (
            "a pass on the SAME issue committed while the parked transaction could "
            "still roll the entry back — the hold does not span the ledger write"
        )

        elsewhere_thread.start()
        assert elsewhere["done"].wait(timeout=_UNBLOCKED_WITHIN), (
            "a pass on a DIFFERENT issue was parked behind this one — the hold is "
            "repo-wide rather than per-issue"
        )
    finally:
        # A failing assertion must not leave the parked transaction holding a lock
        # for the rest of this worker's run.
        release.set()

    for thread in (parked, same_thread, elsewhere_thread):
        thread.join(timeout=_JOIN_TIMEOUT)
        assert not thread.is_alive()

    # Everything committed once the hold was released, and #7 kept the first
    # decision: the hold ORDERS the passes, it does not lose any of them.
    standing = cs.read_skips(OWNER, REPO, tmp_path)
    assert sorted(standing) == ["7", "99"]
    assert standing["7"]["crew_id"] == author["id"]
    assert standing["99"]["crew_id"] == third["id"]


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


def test_recording_a_skip_does_not_create_a_phantom_crew(tmp_path):
    crew = _crew(tmp_path)
    before = [c["id"] for c in cs.list_crews(OWNER, REPO, tmp_path)]
    cs.record_skip(OWNER, REPO, 12, "duplicate of #11", "duplicate", crew["id"], tmp_path)
    after = [c["id"] for c in cs.list_crews(OWNER, REPO, tmp_path)]
    assert after == before


# ── non-finite numbers ──────────────────────────────────────────────────────
#
# Python's `json` decodes `Infinity`, `-Infinity` and `NaN` by default, and it
# also produces them from a literal that looks ordinary: `1e309` overflows to
# `inf` SILENTLY. `int()` accepts neither (`OverflowError` / `ValueError`), so
# every numeric coercion in this store is a crash the store's contract does not
# allow — a malformed STORED value must read as the default.
#
# Storing one instead of crashing would be worse, not better, which is why none of
# these tests asserts a clamp: `json.dumps` writes a bare `Infinity`, which is not
# JSON, so a single poisoned record makes the whole payload unparseable for the
# dashboard — and a bound compared against it (`open_count >= inf`) is simply False
# for every count, so the cap disappears without raising anything.

#: Every literal a JSON body or a stored file can carry that decodes non-finite.
#: `1e309` is here because it is the one that arrives without anybody writing
#: `Infinity`: it is the shape a real overflow takes.
NON_FINITE_LITERALS = ("1e309", "-1e309", "Infinity", "-Infinity", "NaN")


@pytest.mark.parametrize("literal", ["47.9", "0.5", "-1.5", "2.000001"])
def test_a_fractional_ttl_is_refused_rather_than_truncated(tmp_path, literal):
    """A value the operator never asked for must not be stored silently.

    REGRESSION: ``_finite_int`` ended in a bare ``int(value)``, which TRUNCATES, so
    ``47.9`` stored as ``47`` and the form reported success. That is the same silent
    substitution the frontend's ``Number.isInteger`` guard refuses one layer up —
    refusing in both places means neither can invent a value on its own.

    Asserted through the PATCH path: the previous value has to stand, exactly as it
    does for a non-finite number or an over-long ``commit_trailer``.
    """
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 48}, tmp_path)

    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": float(literal)}, tmp_path)

    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 48, "a fractional TTL was truncated into the record"


def test_an_integral_float_is_still_accepted(tmp_path):
    """``48.0`` carries no fraction, so refusing it would reject valid JSON.

    The bound is "not an integer", not "not an int": a JSON number round-tripped
    through a float is the ordinary shape a browser sends.
    """
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 12}, tmp_path)

    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 36.0}, tmp_path)

    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 36, "an integral float was refused"


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_stored_ttl_reads_as_the_default(tmp_path, literal):
    """The store's contract: a malformed stored value reads as the default.

    Reachable without a request — a settings file is an ordinary JSON file in the
    data home, so it can be hand-edited or restored from a backup.
    """
    path = cs.settings_path(OWNER, REPO, tmp_path)
    path.write_text('{"schema": 1, "claim_ttl_hours": %s}' % literal)
    assert json.loads(path.read_text())["claim_ttl_hours"] != 48, "literal decoded finite"

    got = cs.read_settings(OWNER, REPO, tmp_path)

    assert got["claim_ttl_hours"] == cs.DEFAULT_SETTINGS["claim_ttl_hours"]


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_ttl_patch_leaves_the_stored_value_alone(tmp_path, literal):
    """Same discipline as an over-long ``commit_trailer``: the value is dropped and
    the previous one stands, rather than the write raising."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 12}, tmp_path)

    stored = cs.write_settings(
        OWNER, REPO, {"claim_ttl_hours": json.loads(literal)}, tmp_path
    )

    assert stored["claim_ttl_hours"] == 12
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 12
    # And the file it wrote is still JSON — `json.dumps` would have emitted a bare
    # `Infinity`, which no strict parser reads.
    _assert_strict_json(cs.settings_path(OWNER, REPO, tmp_path))


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_max_open_is_ignored_like_any_out_of_range_value(tmp_path, literal):
    crew = _crew(tmp_path)
    updated = cs.update_crew(
        OWNER, REPO, crew["id"], {"max_open": json.loads(literal)}, tmp_path
    )
    assert updated["max_open"] == cs._DEFAULT_CREW["max_open"]


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_avatar_variant_stores_as_none(tmp_path, literal):
    """``None`` is what this field already stores for a non-number, so a non-finite
    number joins that case rather than getting a rule of its own."""
    crew = _crew(tmp_path, avatar_variant=json.loads(literal))
    assert crew["avatar_variant"] is None
    updated = cs.update_crew(
        OWNER, REPO, crew["id"], {"avatar_variant": json.loads(literal)}, tmp_path
    )
    assert updated["avatar_variant"] is None


@pytest.mark.parametrize("field", ("pr_number", "claim_comment_id"))
@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_work_item_number_stores_as_none(tmp_path, field, literal):
    crew = _crew(tmp_path)
    item = cs.upsert_work_item(
        OWNER, REPO, crew["id"], 2251, {field: json.loads(literal)}, tmp_path
    )
    assert item[field] is None
    _assert_strict_json(cs.work_item_path(OWNER, REPO, crew["id"], 2251, tmp_path))


def test_a_hand_edited_crew_record_cannot_defeat_the_slot_cap(tmp_path):
    """The read side, and the reason it matters more than the write side.

    Nothing in the app COMPARES against ``max_open`` with an exception to raise:
    the crew's brief renders it as prose and the page tests ``open >= max_open``,
    which is False for every count once the value is ``inf``. So an unchecked read
    does not crash — it silently removes the cap.
    """
    crew = _crew(tmp_path)
    _poison(cs.crew_path(OWNER, REPO, crew["id"], tmp_path), "max_open", "1e309")

    got = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)

    assert got is not None
    assert got["max_open"] == cs._DEFAULT_CREW["max_open"]
    assert 99 >= got["max_open"], "the cap must be a number a count can exceed"
    assert [c["max_open"] for c in cs.list_crews(OWNER, REPO, tmp_path)] == [
        cs._DEFAULT_CREW["max_open"]
    ]


def test_a_hand_edited_crew_record_stays_serialisable(tmp_path):
    """One poisoned record must not take the page down for every crew: ``GET /crews``
    returns them all in one body, and a bare ``Infinity`` in it is not JSON."""
    crew = _crew(tmp_path)
    _poison(cs.crew_path(OWNER, REPO, crew["id"], tmp_path), "avatar_variant", "NaN")

    got = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)

    assert got is not None
    assert got["avatar_variant"] is None
    # Strict, because `json.loads` would accept the `NaN` this asserts is gone.
    json.loads(json.dumps(got), parse_constant=_reject_constant)


def test_a_hand_edited_pr_number_is_not_written_back(tmp_path):
    """A carried-forward value is re-serialised by the next write, so leaving it
    alone would make the corruption permanent rather than transient."""
    crew = _crew(tmp_path)
    cs.upsert_work_item(OWNER, REPO, crew["id"], 2251, {"pr_number": 2271}, tmp_path)
    path = cs.work_item_path(OWNER, REPO, crew["id"], 2251, tmp_path)
    _poison(path, "pr_number", "1e309")

    item = cs.upsert_work_item(OWNER, REPO, crew["id"], 2251, {"next": "rebase"}, tmp_path)

    assert item["pr_number"] is None
    _assert_strict_json(path)


def test_legitimate_numbers_still_round_trip(tmp_path):
    """The guard must not cost a valid value. Every field hardened above, with a
    number a real caller sends."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 72

    crew = _crew(tmp_path, max_open=5, avatar_variant=2)
    assert (crew["max_open"], crew["avatar_variant"]) == (5, 2)
    reread = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)
    assert reread is not None
    assert (reread["max_open"], reread["avatar_variant"]) == (5, 2)

    item = cs.upsert_work_item(
        OWNER, REPO, crew["id"], 2251,
        {"pr_number": 2271, "claim_comment_id": 9911}, tmp_path,
    )
    assert (item["pr_number"], item["claim_comment_id"]) == (2271, 9911)
    # The carry-forward path preserves them too — a later write must not blank a
    # field it was not given.
    carried = cs.upsert_work_item(OWNER, REPO, crew["id"], 2251, {"next": "rebase"}, tmp_path)
    assert (carried["pr_number"], carried["claim_comment_id"]) == (2271, 9911)
    # The bound still rejects a finite out-of-range value, unchanged.
    assert cs.update_crew(OWNER, REPO, crew["id"], {"max_open": 21}, tmp_path)["max_open"] == 5


def _reject_constant(name: str):
    raise AssertionError(f"non-finite constant {name} survived into the payload")


def _poison(path, field: str, literal: str) -> None:
    """Rewrite *field* in the JSON at *path* to the raw non-finite *literal*.

    Through the FILE, because that is the only way such a value gets onto a record:
    nothing in the store can write one. Through a sentinel rather than a textual
    substitution on the old value, so the edit cannot land on another field that
    happens to hold the same digits.
    """
    raw = {**json.loads(path.read_text()), field: "__poison__"}
    path.write_text(json.dumps(raw).replace('"__poison__"', literal))
    assert not math.isfinite(json.loads(path.read_text())[field]), "fixture is finite"


def _assert_strict_json(path) -> None:
    """The file parses under a decoder that refuses ``Infinity``/``NaN``.

    ``json.loads`` ACCEPTS all three by default, so a plain re-read would pass on a
    file no strict parser — including the dashboard's ``JSON.parse`` — can read.
    """
    json.loads(path.read_text(), parse_constant=_reject_constant)
