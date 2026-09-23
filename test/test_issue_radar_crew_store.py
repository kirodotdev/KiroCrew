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
"""

import hashlib
import inspect
import itertools
import json
import math
import os
import time
from pathlib import Path

import pytest

from conftest import make_dir_link
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.schema import KIND_SESSION
from kiro_crew.crew_log.store import CrewLog

OWNER, REPO = "kirodotdev", "KiroCrew"  # brand-ok: the repository name

_SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
def _isolated_crew_log(tmp_path, monkeypatch):
    """Own crew log data home, crew log on, and no writer state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(crew_log_emit.CREW_LOG_ENV, "1")
    crew_log_emit.reset_caches()
    cs._fold_cache.clear()
    cs._undrained.clear()
    yield
    crew_log_emit.drain_for_shutdown(timeout=2.0)
    crew_log_emit.reset_caches()
    cs._fold_cache.clear()
    cs._undrained.clear()


def _crew(root, name="Andromeda", **spec):
    return cs.create_crew(OWNER, REPO, {"name": name, **spec}, root)


def _unit(crew_id, session_id=None):
    """Create the crew log unit *crew_id*'s slot is serving on; answer its id.

    The handle is dropped: the emitter opens its own when the ledger appends, and a
    handle this test kept would own the write lease it needs.
    """
    session_id = session_id or f"acp-{next(_SEQ)}"
    CrewLog.create(
        KIND_SESSION, session_id, owner="owner", agent="kirocrew", slot=cs.slot_key_for(crew_id)
    )
    return session_id


def _live_crew(root, name="Andromeda", **spec):
    """A crew and the id of the live unit its slot runs on."""
    crew = _crew(root, name, **spec)
    return crew, _unit(crew["id"])


def _record(root, cid, sid, number, patch, kind="implement", text="progress", **kw):
    return cs.commit_work_progress(
        OWNER, REPO, cid, number, patch, kind, text, root=root, session_id=sid, **kw
    )


def _item(root, cid, sid, number, patch, kind="implement", text="progress"):
    return _record(root, cid, sid, number, patch, kind, text)["item"]


def _sweep(root, cid, sid, text="queue empty"):
    return cs.record_crew_checkpoint(OWNER, REPO, cid, text, root, session_id=sid)


def _entries(session_id):
    """The ``radar/recorded`` entries *session_id*'s crew log holds, oldest first."""
    handle = CrewLog.open(KIND_SESSION, session_id)
    try:
        return [e for e in handle.iter_from(1) if e.type == cs.LEDGER_ENTRY_TYPE]
    finally:
        del handle


def _tick():
    """Let the writer's millisecond clock move, so two stamps can differ.

    Without this the stamp assertions are TOOTHLESS: two appends inside one
    millisecond carry an equal stamp whether the code guards it or not, and a "did
    not renew" test would pass with the guard deleted.
    """
    time.sleep(0.004)


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
        cs._fold_cache.clear()
        assert cs.read_ledger(OWNER, REPO, cid, tmp_path) == warm


class _GuardedOnly(dict):
    """A fold cache that refuses any access while the guard is NOT held.

    Reads and writes run on worker threads, so the cache's get, set and eviction
    must all happen under ``_fold_cache_guard``; an eviction is two steps (pick the
    oldest key, pop it) and two threads taking them unguarded can pop the same key
    or resize the dict under the other's iterator. This dict makes an unguarded
    access a failure the test sees, not a race a crew sees.
    """

    def _held(self):
        assert cs._fold_cache_guard.locked(), "fold cache touched without the guard"

    def get(self, key, default=None):
        self._held()
        return super().get(key, default)

    def __setitem__(self, key, value):
        self._held()
        super().__setitem__(key, value)

    def pop(self, key, *default):
        self._held()
        return super().pop(key, *default)

    def __iter__(self):
        self._held()
        return super().__iter__()


def test_every_fold_cache_access_holds_the_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_fold_cache", _GuardedOnly())
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")  # write path: fold + remember
    read = cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)  # read path: the cached get
    assert read is not None and read["phase"] == "claimed"
    # Fill past the slot count so the eviction loop runs under the guard too.
    for n in range(cs._FOLD_CACHE_SLOTS + 3):
        cs._remember_fold((str(tmp_path), f"crew-{n}"), ("u",), (1,), None)
    assert len(cs._fold_cache) == cs._FOLD_CACHE_SLOTS


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


def test_ci_members_and_labels_read_off_bytes_are_re_bounded_by_the_fold():
    """The entry type admits an object for ``ci_state`` and a list for labels, and the
    fold reads bytes off a file: every member is re-bounded to the record tool's own
    type and ceiling, and the label count is capped, so an oversized reading cannot
    make every retained item hold it."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    data = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "ci",
        "event_kind": "ci",
        "ci_state": {
            "state": "x" * 5000,
            "passed": 10**9,  # past the tool's ceiling
            "total": "9",  # a string: not a counter
            "round": 3,
            "stray": {"deep": ["not", "a", "member"]},
        },
        "labels_applied": [f"crew:l{i}" for i in range(1000)],
    }
    value = crew_log.fold(
        "radar",
        [Entry(type=cs.LEDGER_ENTRY_TYPE, seq=1, time=1_789_000_000_000, src="gateway", data=data)],
    )
    (item,) = value["items"]
    assert item["ci_state"] == {"state": "x" * 32, "round": 3}
    assert len(item["labels_applied"]) == entry_types.RADAR_LABELS_LIMIT == 20


def test_rejected_approaches_are_bounded_per_item_newest_kept():
    """Every list the fold retains has a named bound; ``tried`` keeps the newest
    RADAR_TRIED_LIMIT rows of one item, which is what a resume reads."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.crew_log.schema import Entry

    base = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event": "progress",
        "event_kind": "implement",
    }
    n = crew_log.RADAR_TRIED_LIMIT + 5
    entries = [
        Entry(
            type=cs.LEDGER_ENTRY_TYPE,
            seq=i + 1,
            time=1_789_000_000_000 + i,
            src="gateway",
            data={**base, "tried": {"approach": f"approach {i}", "rejected_because": "no"}},
        )
        for i in range(n)
    ]
    (item,) = crew_log.fold("radar", entries)["items"]
    assert len(item["tried"]) == crew_log.RADAR_TRIED_LIMIT
    assert item["tried"][-1]["approach"] == f"approach {n - 1}"
    assert item["tried"][0]["approach"] == "approach 5"


def _fold_entries(crew_log, datas):
    from kiro_crew.crew_log.schema import Entry

    return crew_log.fold(
        "radar",
        [
            Entry(
                type=cs.LEDGER_ENTRY_TYPE,
                seq=i + 1,
                time=1_789_000_000_000 + i,
                src="gateway",
                data=data,
            )
            for i, data in enumerate(datas)
        ],
    )


def test_work_items_are_bounded_finished_ones_evicted_first_and_counted(monkeypatch):
    """Past RADAR_ITEM_LIMIT the fold evicts: a finished item before any open one,
    oldest finish first, never the item just written; an evicted item takes its
    phase history along; and the count is reported so a bounded record is told
    from a complete one."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 4)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    datas = [
        {**base, "number": 1, "phase": "resolved", "event": "done 1"},  # finished, oldest
        {**base, "number": 2, "phase": "awaiting-ci", "event": "on 2"},  # open, not editing
        {**base, "number": 3, "phase": "resolved", "event": "done 3"},  # finished
        {**base, "number": 4, "phase": "awaiting-ci", "event": "on 4"},  # open, not editing
        {**base, "number": 5, "phase": "claimed", "event": "took 5"},  # open, 5th: evicts #1
        {**base, "number": 6, "phase": "claimed", "event": "took 6"},  # 5th again: evicts #3
    ]
    value = _fold_entries(crew_log, datas)
    kept = sorted(item["number"] for item in value["items"])
    assert kept == [2, 4, 5, 6], "the finished items went first, every open one stayed"
    assert value["counts"]["evicted_items"] == 2
    assert set(value["phase_lines"]) == {"2", "4", "5", "6"}, "an evicted item's history goes too"
    assert value["counts"]["open"] == 4

    # Only open items left: the one longest without progress goes, never the newest.
    value = _fold_entries(
        crew_log, datas + [{**base, "number": 7, "phase": "claimed", "event": "took 7"}]
    )
    assert sorted(item["number"] for item in value["items"]) == [4, 5, 6, 7]
    assert value["counts"]["evicted_items"] == 3


def test_a_retry_after_eviction_puts_the_item_back_rather_than_deduping_away(monkeypatch):
    """The dedup map and the item bound evict on different rules over different key
    sets, so an evicted item's digest can outlive the item. A retry matching that stale
    digest must APPLY: returning early would leave the crew told its update landed while
    the row is gone."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    first = {**base, "number": 1, "phase": "resolved", "event": "done 1"}
    datas = [
        first,
        {**base, "number": 2, "phase": "implementing", "event": "on 2"},
        {**base, "number": 3, "phase": "implementing", "event": "on 3"},
    ]
    value = _fold_entries(crew_log, datas)
    assert sorted(item["number"] for item in value["items"]) == [2, 3], "#1 was evicted"

    # The SAME update again -- a retry of the line the crew was told landed.
    value = _fold_entries(crew_log, datas + [first])
    assert 1 in {item["number"] for item in value["items"]}, "the retry did not put #1 back"


def test_this_crews_passes_are_bounded_earliest_decided_evicted_and_counted(monkeypatch):
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_SKIP_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "skip"}
    datas = [
        {
            **base,
            "number": n,
            "phase": "skipped",
            "event": f"pass {n}",
            "skip": {"reason": f"r{n}", "scope": "duplicate"},
        }
        for n in (11, 12, 13)
    ]
    value = _fold_entries(crew_log, datas)
    assert sorted(value["skips"]) == ["12", "13"], "the earliest decided pass went"
    assert value["counts"]["evicted_skips"] == 1


def test_a_ci_reading_keeps_only_the_declared_member_keys():
    """``ci_state`` is merged key by key; an undeclared key would grow the item by
    key without bound, so the fold keeps the declared members and no other."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.crew_log import projection as crew_log

    base = {
        "crew_id": "c_0a1b2c3d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "event_kind": "ci",
        "event": "ci",
    }
    datas = [
        {**base, "ci_state": {"state": "pending", "passed": 3, "total": 9, "shard_17": "red"}},
        {**base, "ci_state": {"round": 2, "shard_18": "red", "inherited_reds": 1}},
    ]
    (item,) = _fold_entries(crew_log, datas)["items"]
    assert item["ci_state"] == {
        "state": "pending",
        "passed": 3,
        "total": 9,
        "round": 2,
        "inherited_reds": 1,
    }
    assert set(item["ci_state"]) <= set(entry_types.RADAR_CI_KEYS)


def test_a_crews_writes_are_serialized_so_two_editors_cannot_both_pass(tmp_path, monkeypatch):
    """The one-editor refusal is checked against the folded record, so two requests
    validating against the same fold could both pass and both append. Every write
    holds the crew's lock from fold to answer; the second request folds AFTER the
    first landed and is refused."""
    import threading

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    release = threading.Event()
    entered = threading.Event()
    real_fold = cs._fold_checkpoint

    def slow_fold(crew_id, units):
        checkpoint = real_fold(crew_id, units)
        if not entered.is_set():
            entered.set()
            release.wait(5)  # hold the first write inside its critical section
        return checkpoint

    monkeypatch.setattr(cs, "_fold_checkpoint", slow_fold)
    outcomes: dict[int, object] = {}

    def write(number):
        try:
            outcomes[number] = _record(tmp_path, cid, sid, number, {"phase": "implementing"})
        except cs.CrewStoreError as exc:
            outcomes[number] = exc

    first = threading.Thread(target=write, args=(2251,))
    first.start()
    assert entered.wait(5), "the first write never reached its fold"
    # The second write starts while the first is inside its critical section. Without
    # the lock it folds the same editor-less record, passes the one-editor check and
    # appends -- two editing items. With the lock it waits, folds after the first
    # landed, and is refused.
    second = threading.Thread(target=write, args=(2264,))
    second.start()
    time.sleep(0.2)
    release.set()
    first.join(10)
    second.join(10)
    assert isinstance(outcomes[2251], dict) and outcomes[2251]["item"]["phase"] == "implementing"
    assert isinstance(outcomes[2264], cs.CrewStoreError)
    assert "already editing" in str(outcomes[2264])
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [2251]
    assert cs._crew_write_lock(cid) is cs._crew_write_lock(cid)


def test_the_carry_marks_its_files_only_after_every_entry_landed(tmp_path, monkeypatch):
    """A drained writer is not proof: it can refuse an entry and drain quietly. The
    marker is written only after each carried entry is read back, so a carry that
    did not land is run again on the next write instead of being lost for good."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    from kiro_crew.crew_log import emit as crew_log_emit

    real_recorded = crew_log_emit.on_radar_recorded
    calls = {"n": 0}

    def drop_the_first_carry(session_id, data):
        calls["n"] += 1
        if data.get("carried") and calls["n"] == 1:
            return  # the writer "refused" it: nothing appended, nothing raised
        real_recorded(session_id, data)

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", drop_the_first_carry)
    # The write that triggered the carry is REFUSED: the record it would fold is
    # short of the row the writer refused, and folding it could admit a second editor.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    marker = item_file.parent / cs._ITEMS_CARRIED_MARKER
    assert not marker.exists(), "a carry that did not land must not be marked done"
    assert not [
        e for e in _entries(sid) if e.data.get("carried") is not True
    ], "the refused write appended nothing of its own"
    # An item's history is several entries, so a refusal can take one and leave
    # another; each entry re-states the whole record, so what DID land folds with its
    # real phase rather than as a bare placeholder the crew would read as live work.
    landed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert landed is not None and landed["phase"] == "implementing"

    # The next write carries it again; this time it lands, the marker appears and
    # the write itself goes through.
    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", real_recorded)
    cs._fold_cache.clear()
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert marker.is_file()
    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    # Every approach is recovered, including the one the writer refused the first
    # time. Compared as a SET: a row recovered by a later run is appended when it
    # arrives, so the file's order survives a clean carry (pinned in the carry test)
    # but not a refused one -- and having the approach in the wrong place beats not
    # having it, since the files are never read again once the carry is marked done.
    assert {t["approach"] for t in carried["tried"]} == {"hasattr guard", "pywin32"}
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is not None


def test_a_carry_still_queued_in_the_writer_refuses_the_write_that_triggered_it(
    tmp_path, monkeypatch
):
    """The carry's rows are emitted, the flush times out: the refold this write would
    take is missing them -- a carried editing item among them -- so the write is
    refused, the crew is marked undrained, and the files stay unmarked. Once the
    writer drains, the same write goes through against the carried record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)  # phase implementing: an EDITING item
    from kiro_crew.crew_log import emit as crew_log_emit

    real_flush = crew_log_emit.flush
    behind = {"on": True}

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        return False if behind["on"] else drained

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    # Issue 7 entering an editing phase would be a SECOND editor beside carried 2251.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert not (item_file.parent / cs._ITEMS_CARRIED_MARKER).exists()
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"

    behind["on"] = False
    with pytest.raises(cs.CrewStoreError, match="already editing #2251"):
        _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    assert not any(key[1] == cid for key in cs._undrained)


def test_an_entry_naming_another_crew_is_left_out_of_the_fold(tmp_path):
    """Every unit of one slot belongs to ONE crew, so a line naming another is a
    planted or damaged one and must not patch a record it does not own -- nor may
    it break the reads that follow it."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")
    planted = {
        "crew_id": "c_0badf00d",
        "owner": OWNER,
        "repo": REPO,
        "number": 7,
        "phase": "resolved",
        "event": "planted",
        "event_kind": "merge",
    }
    crew_log_emit.on_radar_recorded(sid, planted)
    assert crew_log_emit.flush(timeout=5.0)
    assert len(_entries(sid)) == 2, "the planted line is in the file"

    item = cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)
    assert item["phase"] == "claimed" and item["finished_at"] is None
    assert [e["text"] for e in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)] == ["progress"]
    # And the crew keeps writing past it.
    assert _item(tmp_path, cid, sid, 7, {"phase": "implementing"})["phase"] == "implementing"


# ── the ledger: progress lines ──────────────────────────────────────────────


def test_events_read_newest_first_and_filter_by_crew(tmp_path):
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _record(tmp_path, a["id"], sid_a, 1, {}, "claim", "claimed")
    _tick()
    _record(tmp_path, b["id"], sid_b, 2, {}, "ci", "CI round 3")
    all_events = cs.read_events(OWNER, REPO, tmp_path)
    assert [e["kind"] for e in all_events] == ["ci", "claim"]
    mine = cs.read_events(OWNER, REPO, tmp_path, crew_id=a["id"])
    assert [e["kind"] for e in mine] == ["claim"]


def test_require_phase_keeps_only_the_entries_into_a_phase(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "awaiting-ci"}, "ci", "round 1")
    _record(tmp_path, cid, sid, 7, {"ci_state": {"round": 2}}, "ci", "round 2")
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid, require_phase=True)
    assert [(e["text"], e["phase"]) for e in lines] == [("round 1", "awaiting-ci")]
    assert cs.read_events(OWNER, REPO, tmp_path, crew_id=cid, limit=1)[0]["text"] == "round 2"


def test_unknown_event_kind_is_refused(tmp_path):
    crew, sid = _live_crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="unknown event kind"):
        _record(tmp_path, crew["id"], sid, 1, {}, "vibes", "...")
    assert _entries(sid) == []


# ── crew-level lines (a step that belongs to no issue) ──────────────────────
#
# The invariant these protect is the one the feature exists for: a crew that
# checked the queue and took nothing must be able to SAY so. Before this, the
# only way to record the cycle was to attribute it to an issue the crew never
# acted on, so the ledger either lied or stayed silent. Both directions of the
# pairing are asserted, because a crew-level kind carrying a number is the same
# false attribution written the other way round.


def test_a_crew_level_line_omits_the_number_entirely(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _sweep(tmp_path, crew["id"], sid)
    entry = result["event"]
    # Absent, NOT zero: a `0` is indistinguishable from a real issue number in
    # every filter and join that keys on this field.
    assert "number" not in entry
    stored = cs.read_events(OWNER, REPO, tmp_path)
    assert len(stored) == 1
    assert "number" not in stored[0]
    assert (stored[0]["kind"], stored[0]["text"]) == ("sweep", "queue empty")
    # And the appended entry itself carries no number, so no reader can invent one.
    (appended,) = _entries(sid)
    assert "number" not in appended.data


def test_a_crew_level_kind_with_a_number_is_refused(tmp_path):
    crew, sid = _live_crew(tmp_path)
    with pytest.raises(cs.CrewStoreError, match="takes no issue number"):
        _record(tmp_path, crew["id"], sid, 7, {}, "sweep", "queue empty")
    with pytest.raises(cs.CrewStoreError, match="takes no issue number"):
        _record(tmp_path, crew["id"], sid, 1, {}, cs.CREW_LEVEL_EVENT_KIND, "x")
    assert _entries(sid) == []


def test_a_numbered_line_keeps_its_historical_id(tmp_path):
    """The id formula for a NUMBERED line must not have changed.

    It is content-addressed and drives merge-on-read dedupe, so a new formula would
    give every existing line a fresh id and silently defeat the dedupe for the whole
    ledger. Asserted against the literal formula rather than against a golden string
    so the test does not need a frozen clock.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    entry = _record(tmp_path, cid, sid, 4, {}, "claim", "claimed")["event"]

    def _formula(ts, crew_id, number, kind, text):
        shown = "" if number is None else int(number)
        return hashlib.sha256(f"{ts}|{crew_id}|{shown}|{kind}|{text}".encode()).hexdigest()[:16]

    assert entry["id"] == _formula(entry["ts"], cid, 4, "claim", "claimed")
    # And the two families cannot collide: the numberless variant renders the
    # number as the empty string, which no real number produces.
    sweep = _sweep(tmp_path, cid, sid, "x")["event"]
    assert sweep["id"] == _formula(sweep["ts"], cid, None, "sweep", "x")
    assert _formula(sweep["ts"], cid, None, "sweep", "x") != _formula(
        sweep["ts"], cid, 0, "sweep", "x"
    )


def test_record_crew_checkpoint_writes_one_line_and_no_item(tmp_path):
    crew, sid = _live_crew(tmp_path)
    result = _sweep(tmp_path, crew["id"], sid, "checked 42 open issues, took none")
    # Same envelope as commit_work_progress, so the write route answers one shape.
    assert {"item", "event", "skip"} <= set(result)
    assert result["item"] is None and result["skip"] is None
    assert result["event"]["kind"] == "sweep"
    assert result["coalesced"] is False
    assert set(result["event"]) == {"id", "ts", "crew_id", "kind", "text"}
    # No work item was created, so nothing consumes a work slot.
    assert cs.list_work_items(OWNER, REPO, crew["id"], tmp_path) == []
    assert len(_entries(sid)) == 1


def test_consecutive_sweeps_coalesce_instead_of_running_away(tmp_path):
    """An idle crew must not bury its own work log.

    A sweep is a recurring latest-value fact and the crew is nudged on a timer, so
    one line per cycle would be an unbounded run. Every ledger read is capped and
    drops the OLDEST line first, so a few hundred idle cycles would push the real
    history out of the log the feature exists to make honest.
    """
    crew, sid = _live_crew(tmp_path)
    first = _sweep(tmp_path, crew["id"], sid, "queue empty")
    _tick()
    again = _sweep(tmp_path, crew["id"], sid, "still empty")

    assert first["coalesced"] is False
    assert again["coalesced"] is True
    # The SECOND call is answered with the FIRST line, so the timestamp marks when
    # the idle stretch began rather than when it was last observed.
    assert again["event"]["id"] == first["event"]["id"]
    assert again["event"]["text"] == "queue empty"
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1
    # Coalesced BEFORE the append: the second sweep wrote nothing at all.
    assert len(_entries(sid)) == 1


def test_a_sweep_after_real_work_is_written(tmp_path):
    # Coalescing keys on the crew's newest line, so the transition back into idle
    # is recorded -- otherwise the guard would swallow every sweep after the first.
    crew, sid = _live_crew(tmp_path)
    _sweep(tmp_path, crew["id"], sid, "queue empty")
    _tick()
    _record(tmp_path, crew["id"], sid, 7, {}, "claim", "took #7")
    _tick()
    third = _sweep(tmp_path, crew["id"], sid, "empty again")

    assert third["coalesced"] is False
    assert [e["kind"] for e in cs.read_events(OWNER, REPO, tmp_path)] == ["sweep", "claim", "sweep"]


def test_a_pause_does_not_break_the_fold_because_liveness_is_not_evidenced(tmp_path):
    """Coalescing is deliberately blind to pause state.

    An earlier revision stamped ``paused_at`` and refused to fold across it, to stop
    the page claiming an unbroken "checking since" run over a stop. That claim is
    gone: the row now renders as a past instant, so the fold has nothing to protect,
    and the field was removed rather than kept to cover one of the three stop modes
    while a crash and a lost timer stayed uncovered. This pins the simpler rule so
    it is not re-complicated without a reason.
    """
    crew, sid = _live_crew(tmp_path)
    first = _sweep(tmp_path, crew["id"], sid, "queue empty")
    cs.set_crew_paused(OWNER, REPO, crew["id"], True, "operator stopped it", tmp_path)
    cs.set_crew_paused(OWNER, REPO, crew["id"], False, "", tmp_path)
    _tick()
    after = _sweep(tmp_path, crew["id"], sid, "empty again")

    assert first["coalesced"] is False
    assert after["coalesced"] is True
    assert after["event"]["id"] == first["event"]["id"]
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 1
    # The record carries no pause timestamp: the field is gone, not merely unused.
    assert "paused_at" not in cs.read_crew(OWNER, REPO, crew["id"], tmp_path)


def test_one_crews_sweep_does_not_coalesce_anothers(tmp_path):
    # Each crew folds its own log, so one crew's idle line cannot swallow another's.
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _sweep(tmp_path, a["id"], sid_a, "a is empty")
    theirs = _sweep(tmp_path, b["id"], sid_b, "b is empty")

    assert theirs["coalesced"] is False
    assert len(cs.read_events(OWNER, REPO, tmp_path)) == 2


def test_the_checkpoint_writer_takes_no_kind_so_a_wrong_one_is_unrepresentable(tmp_path):
    """The refusal is structural: the constant is written directly and a caller has
    nothing to get wrong. The number/kind pairing is still enforced, in
    ``commit_work_progress`` and again by the fold, which the tests above cover."""
    crew, sid = _live_crew(tmp_path)
    assert "event_kind" not in inspect.signature(cs.record_crew_checkpoint).parameters
    written = _sweep(tmp_path, crew["id"], sid)
    assert written["event"]["kind"] == cs.CREW_LEVEL_EVENT_KIND


# ── phase classification ────────────────────────────────────────────────────


def test_the_two_phase_classifications_do_not_coincide(tmp_path):
    """This is the point of keeping two separate sets rather than a flag."""
    # awaiting-ci: occupies a slot, is NOT ttl-active, is NOT editing.
    assert "awaiting-ci" not in cs.TTL_ACTIVE_PHASES
    assert "awaiting-ci" not in cs.EDITING_PHASES
    assert "awaiting-ci" not in cs.TERMINAL_PHASES
    # addressing-review: editing, but NOT ttl-active -- an open pull request stands
    # in for a heartbeat, so the claim does not age against it.
    assert "addressing-review" in cs.EDITING_PHASES
    assert "addressing-review" not in cs.TTL_ACTIVE_PHASES
    # implementing: both ttl-active and editing, and slot-occupying.
    assert "implementing" in cs.TTL_ACTIVE_PHASES
    assert "implementing" in cs.EDITING_PHASES


def test_the_vocabularies_are_the_ones_the_entry_type_declares(tmp_path):
    """The store re-exports the crew log's constants rather than owning a copy:
    the entry type's closed enums and the writer's refusals must be ONE set."""
    from kiro_crew.crew_log import entry_types as et

    assert cs.PHASES is et.RADAR_PHASES
    assert cs.EVENT_KINDS is et.RADAR_EVENT_KINDS
    assert cs.SKIP_SCOPES is et.RADAR_SKIP_SCOPES
    assert cs.CREW_LEVEL_EVENT_KIND == et.RADAR_CREW_LEVEL_EVENT_KIND == "sweep"
    assert cs.LEDGER_ENTRY_TYPE == et.RADAR_ENTRY_TYPE == "radar/recorded"


# ── the shared skip index ───────────────────────────────────────────────────
#
# A pass is recorded as part of the update that skips the issue: the same entry
# carries the phase, the line and the skip row. The index every crew consults
# before investigating is the fold of those rows across every crew of the
# repository.


def _pass(root, cid, sid, number, reason, scope, **patch):
    return _record(
        root,
        cid,
        sid,
        number,
        {"phase": "skipped", **patch},
        "skip",
        f"passed on #{number}",
        skip_reason=reason,
        skip_scope=scope,
    )


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


# ── carrying the pre-projection files forward ───────────────────────────────
#
# The pre-projection ledger files -- ``crews/<crew_id>/<n>.json``, ``events.jsonl``,
# ``skipped.json`` -- are read ONCE more, on a crew's first write after the
# upgrade, and re-stated into its crew log. They are never written again.


def _legacy_item(root, cid, number, **fields):
    path = cs.work_item_path(OWNER, REPO, cid, number, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 1,
        "crew_id": cid,
        "owner": OWNER,
        "repo": REPO,
        "number": number,
        "phase": "implementing",
        "next": "add the Windows branch",
        "tried": [
            {
                "approach": "hasattr guard",
                "rejected_because": "loses the ACL",
                "at": "2026-01-01T00:00:00Z",
            },
            {"approach": "pywin32", "rejected_because": "", "at": "2026-01-02T00:00:00Z"},
        ],
        "worktree": "/w/2251",
        "branch": "fix/2251",
        "pr_number": 2271,
        "claimed_at": "2026-01-01T00:00:00Z",
        "last_progress_at": "2026-01-02T00:00:00Z",
        "finished_at": None,
        **fields,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _legacy_skips(root, rows):
    path = cs.skips_path(OWNER, REPO, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_a_crews_first_write_carries_its_items_and_the_repositorys_passes_forward(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    skips_file = _legacy_skips(
        tmp_path,
        {
            "42": {
                "number": "42",
                "reason": "dup of #41",
                "scope": "duplicate",
                "crew_id": "c_deadbeef",
                "decided_at": "2025-12-31T00:00:00Z",
            },
        },
    )
    item_bytes, skips_bytes = item_file.read_bytes(), skips_file.read_bytes()

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None, "the pre-projection item was not carried"
    assert carried["phase"] == "implementing" and carried["next"] == "add the Windows branch"
    assert carried["pr_number"] == 2271 and carried["branch"] == "fix/2251"
    # Its own stamps, not fresh ones: re-stamping would make every carried claim
    # look freshly made and reset the TTL the record had already aged against.
    assert carried["claimed_at"] == "2026-01-01T00:00:00Z"
    assert carried["last_progress_at"] == "2026-01-02T00:00:00Z"
    # EVERY rejected approach is carried, in the file's own order, so the newest is
    # still last -- the field exists so the crew does not repeat what it ruled out.
    assert [t["approach"] for t in carried["tried"]] == ["hasattr guard", "pywin32"]
    assert [t["rejected_because"] for t in carried["tried"]] == ["loses the ACL", ""]
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
    carry_line = next(line for line in lines if line.get("number") == 2251)
    assert "carried forward" in carry_line["text"]
    assert "not carried" not in carry_line["text"], "nothing was left behind to say"
    # The repository's passes, with the crew and time that decided them.
    row = cs.read_skips(OWNER, REPO, tmp_path)["42"]
    assert row == {
        "number": 42,
        "reason": "dup of #41",
        "scope": "duplicate",
        "crew_id": "c_deadbeef",
        "decided_at": "2025-12-31T00:00:00Z",
        "deferred": False,
    }
    # A carried pass creates NO work item for the carrying crew.
    assert {i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)} == {7, 2251}
    # The files are left exactly as they were, and marked so they are never read twice.
    assert (item_file.read_bytes(), skips_file.read_bytes()) == (item_bytes, skips_bytes)
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    assert (skips_file.parent / cs._SKIPS_CARRIED_MARKER).is_file()
    # The crew's own write landed too, after the carry.
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["phase"] == "claimed"


def test_the_passes_are_carried_by_one_crew_only(tmp_path):
    """Whichever crew of the repository writes first carries the shared index; the
    marker stops a second crew re-carrying it, so the row stays one row."""
    a, sid_a = _live_crew(tmp_path, name="Andromeda")
    b, sid_b = _live_crew(tmp_path, name="Whirlpool")
    _legacy_skips(
        tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other", "crew_id": "c_deadbeef"}}
    )

    _record(tmp_path, a["id"], sid_a, 7, {"phase": "claimed"}, "claim", "took #7")
    _record(tmp_path, b["id"], sid_b, 9, {"phase": "claimed"}, "claim", "took #9")

    assert "42" in cs.read_ledger(OWNER, REPO, a["id"], tmp_path)["skips"]
    assert "42" not in cs.read_ledger(OWNER, REPO, b["id"], tmp_path)["skips"]
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["crew_id"] == "c_deadbeef"
    # One carried entry, in the first crew's log; the second appended only its own.
    assert len(_entries(sid_a)) == 2 and len(_entries(sid_b)) == 1


def test_a_crew_whose_fold_is_not_empty_does_not_carry(tmp_path):
    """The carry runs on a crew's FIRST write only. A file appearing later -- a
    restore from a backup -- is not silently merged over a live record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    _legacy_item(tmp_path, cid, 2251)

    _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "fixing")

    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is None
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    ).exists()


def test_a_crew_level_sweep_as_the_first_write_carries_the_legacy_items(tmp_path):
    """An idle sweep is a routine first write after the upgrade. It carries the
    pre-projection files exactly as an item write would, so a crew that only sweeps
    is not read as owing nothing while its legacy open items sit unread."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)

    _sweep(tmp_path, cid, sid)

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1
    marker = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    assert marker.is_file()


def test_reads_apply_units_in_the_order_they_recorded_not_header_order(tmp_path, monkeypatch):
    """A read has no caller inside a unit to pin the live one last, so it orders the
    units by the order the crew RECORDED into them. Header clock order is what a
    backward clock step inverts; here the store is made to list the units in that
    inverted order, and the read still applies the later write last."""
    from kiro_crew.crew_log import store as crew_log_store

    crew = _crew(tmp_path)
    cid = crew["id"]
    first = _unit(cid)
    _item(tmp_path, cid, first, 7, {"phase": "claimed"}, "claim")
    second = _unit(cid)
    _item(tmp_path, cid, second, 7, {"phase": "implementing"}, "implement")
    real = crew_log_store.session_units_for_slot
    assert real(cs.slot_key_for(cid)) == (first, second), "the control: header order agrees"

    monkeypatch.setattr(
        crew_log_store,
        "session_units_for_slot",
        lambda slot, **kw: tuple(reversed(real(slot, **kw))),
    )
    cs._fold_cache.clear()
    assert cs.crew_log_units(OWNER, REPO, cid, tmp_path) == (first, second)
    item = cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)
    assert item is not None and item["phase"] == "implementing"
    # The order file lives beside the crew record, not in the legacy items dir.
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    assert order.read_text().split() == [first, second]
    assert order.parent == cs.crews_dir(OWNER, REPO, tmp_path)


def test_the_app_root_strips_only_the_provider_subtree_it_appended(tmp_path):
    """``_app_root`` strips the EXACT trailing subtree, not the first reserved segment.

    The id-uniqueness check globs every repository of the data home from this root, and
    the mint lock lives under it. Matching the first ``@providers`` component anywhere in
    the path truncated an operator base path that carried that segment, moving both above
    the data home -- where the glob matches no crew record at all, so ``_crew_id_in_use``
    answers False for an id that IS held and two crews fold one ledger.
    """
    # NEGATIVE -- the reserved segment sits in the operator's own base path. Nothing is
    # stripped: this is the data home, not a subtree of one.
    operator_base = tmp_path / "@providers" / "radar-home"
    operator_base.mkdir(parents=True, exist_ok=True)
    assert cs._app_root(operator_base) == operator_base

    # POSITIVE -- the exact three components ``provider_root`` appends ARE stripped, even
    # though the base above them also carries the segment.
    subtree = cs.store.provider_root(
        root=operator_base, provider="gitlab", host="gitlab.internal:8443"
    )
    assert subtree != operator_base, "the fixture did not build a provider subtree"
    assert cs._app_root(subtree) == operator_base


@pytest.mark.skipif(
    os.name != "posix",
    reason=(
        "plants a REAL file symlink, which needs a privilege an unelevated Windows shell "
        "does not have. Windows is not left without the boundary: the NAME screen this "
        "exercises is pinned there by "
        "test_the_name_screen_refuses_a_linked_order_file_on_every_platform, and the "
        "DIRECTORY screen by test_the_unit_order_write_refuses_a_linked_directory, which "
        "plants a junction."
    ),
)
def test_the_unit_order_write_never_follows_a_planted_link(tmp_path):
    """The unit-order file is written under the gateway's data home, where a sandboxed
    agent may be able to plant a link. A link at the file's NAME is replaced, not
    written through: the file is staged under a unique name and renamed over the
    link. A link at the crews DIRECTORY is refused (the parent is pinned before the
    write), and the crew falls back to header order -- the write itself still goes
    through. In neither case does a byte land in the file the link points at."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    victim = tmp_path / "operator-file.txt"
    victim.write_text("operator data\n", encoding="utf-8")
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    # Not guarded: on POSIX this must succeed, and a skip here would score as a pass
    # for the very branch the test exists to check.
    os.symlink(victim, order)

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert (
        victim.read_text(encoding="utf-8") == "operator data\n"
    ), "nothing written through the link"
    from kiro_crew import atomic_write as atomic_write_module

    if atomic_write_module.pinned_parent_replace_supported():
        # The staged file is renamed over the link: the link is replaced, never followed.
        assert not order.is_symlink() and order.read_text().split() == [sid], "link replaced"
    else:
        # No descriptor-relative rename here: a link at the name is refused and left
        # alone, and the crew falls back to header order.
        assert order.is_symlink(), "the link was refused, not written through"

    # A link at the DIRECTORY has its own test below, which reaches Windows too, and
    # the NAME screen's own refusal is pinned on every platform by the test that follows.


def test_the_name_screen_refuses_a_linked_order_file_on_every_platform(tmp_path, monkeypatch):
    """The NAME half of the fallback screen refuses the write, on every platform.

    The test above plants a REAL symlink, so it is POSIX-only -- and Windows is exactly
    the platform that TAKES this fallback, having no descriptor-relative rename. A skip
    there would score as a pass for the one boundary that platform has, so the screen's
    own decision is driven here instead: the fallback is forced, and
    ``is_link_or_junction`` answers True for the order path ALONE (the real function
    still answers for every other path, so a linked ancestor cannot be what refuses).

    What the POSIX test proves is that a real link makes that function answer True; what
    this pins is that the writer ACTS on the answer rather than writing through it.
    """
    from kiro_crew import atomic_write as atomic_write_module
    from kiro_crew import platform_compat

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    order.parent.mkdir(parents=True, exist_ok=True)
    order.write_text("operator data\n", encoding="utf-8")

    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)
    real_screen = platform_compat.is_link_or_junction
    monkeypatch.setattr(
        platform_compat,
        "is_link_or_junction",
        lambda p, _real=real_screen, _target=order: True if Path(p) == _target else _real(p),
    )

    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,))
    assert (
        order.read_text(encoding="utf-8") == "operator data\n"
    ), "the writer wrote through a name the screen refused"


def test_the_unit_order_write_refuses_a_linked_directory(tmp_path):
    """A link at the crews DIRECTORY is refused and the crew falls back to header
    order; the write itself still goes through and nothing lands in the target.

    The link is made with the conftest helper, which uses a JUNCTION on Windows: a
    directory symlink needs a privilege an unelevated shell does not have, so a test
    that planted one with ``os.symlink`` skipped on the one platform where this
    matters -- it is the platform without descriptor-relative writes, so the explicit
    screen is the only boundary there."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    victim_dir = tmp_path / "operator-dir"
    victim_dir.mkdir()
    crews = cs.crews_dir(OWNER, REPO, tmp_path)
    keep = crews.with_name(crews.name + ".keep")
    crews.rename(keep)
    make_dir_link(crews, victim_dir)
    try:
        cs._fold_cache.clear()
        _record(tmp_path, cid, sid, 7, {"next": "read it"}, "claim", "progress")
        assert list(victim_dir.iterdir()) == [], "nothing written into the linked directory"
    finally:
        cs.platform_compat.unlink_link_or_junction(crews)
        keep.rename(crews)
    assert cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["next"] == "read it"


def test_the_unit_order_fallback_screen_answers_for_a_junction_not_only_a_symlink(
    tmp_path, monkeypatch
):
    """The fallback screen consults the junction-aware predicate, not ``is_symlink``.

    ``os.path.islink`` is False for a Windows directory junction, so an
    ``islink``-only screen leaves the platform that HAS junctions -- the one taking
    this fallback, since it has no descriptor-relative rename -- with no boundary.
    Pinned by reporting a link through the predicate while the filesystem holds a
    plain directory: a screen that consulted ``is_symlink`` would write anyway."""
    from kiro_crew import atomic_write as atomic_write_module

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    order = cs._unit_order_path(OWNER, REPO, cid, tmp_path)
    monkeypatch.setattr(atomic_write_module, "pinned_parent_replace_supported", lambda: False)

    monkeypatch.setattr(
        cs.platform_compat, "is_link_or_junction", lambda p: os.fspath(p) == os.fspath(order)
    )
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,))

    monkeypatch.setattr(cs.platform_compat, "is_link_or_junction", lambda p: False)
    monkeypatch.setattr(cs.platform_compat, "first_linked_ancestor", lambda p: str(order.parent))
    with pytest.raises(OSError, match="reached through a link"):
        cs._write_unit_order(order, (sid,))
    assert not order.exists(), "neither refusal wrote the file"

    monkeypatch.setattr(cs.platform_compat, "first_linked_ancestor", lambda p: None)
    cs._write_unit_order(order, (sid,))
    assert order.read_text(encoding="utf-8").split() == [sid], "the control: it writes otherwise"


def test_a_unit_listing_that_fails_refuses_the_write_and_empties_the_read(tmp_path, monkeypatch):
    """The store's slot listing fails soft for a READ (it runs on every cycle, and an
    empty record says nothing false about what could be read). A WRITE lists strictly:
    folded over an empty record because the scan failed, the one-editor rule would
    admit a second editor, so the write is refused instead."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")

    # The store's own listing: soft for a read, raised for a strict caller.
    def scan_fails(kind):
        raise OSError("the store could not be scanned")

    with monkeypatch.context() as scan:
        scan.setattr(crew_log_store, "_checked_crew_log_root", scan_fails)
        assert crew_log_store.session_units_for_slot(cs.slot_key_for(cid)) == ()
        with pytest.raises(OSError):
            crew_log_store.session_units_for_slot(cs.slot_key_for(cid), strict=True)

    # Through the crew store: a read empties, a write is refused.
    def listing_fails(slot, **kw):
        raise OSError("the store could not be scanned")

    monkeypatch.setattr(crew_log_store, "session_units_for_slot", listing_fails)
    cs._fold_cache.clear()
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == [], "a read answers the empty record"
    with pytest.raises(cs.CrewLedgerNotRecorded):
        # Issue 8 entering an editing phase would be a SECOND editor beside 7.
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")


def test_a_unit_holding_entries_it_cannot_prove_refuses_the_write(tmp_path, monkeypatch):
    """A listing comes back INCOMPLETE, not failed: the root scan succeeds and one
    unit's header will not read. That unit's entries hold the editing item, so a write
    folded over the shorter listing passes the one-editor rule and admits a second
    editor -- the same loss as a scan that failed, reached without any failure."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, retired = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, retired, 7, {"phase": "implementing"}, "implement", "on #7")
    live = _unit(cid)
    slot = cs.slot_key_for(cid)
    retired_dir = crew_log_store.unit_dir_for(KIND_SESSION, retired)
    assert retired_dir is not None, "the retired unit's directory is there to break"

    real_read = crew_log_store._read_header_line

    def header_will_not_read(path):
        if path.parent == retired_dir:
            raise OSError("the header will not read right now")
        return real_read(path)

    monkeypatch.setattr(crew_log_store, "_read_header_line", header_will_not_read)
    monkeypatch.setattr(crew_log_store, "_slot_index", None)
    cs._fold_cache.clear()

    # A READ takes the shorter listing: it says nothing false about what it could read.
    assert crew_log_store.session_units_for_slot(slot) == (live,)
    # A validating caller is refused instead, naming the unit it could not account for.
    monkeypatch.setattr(crew_log_store, "_slot_index", None)
    with pytest.raises(crew_log_store.CrewLogError, match="cannot prove"):
        crew_log_store.session_units_for_slot(slot, strict=True)

    # Through the crew store: issue 8 entering an editing phase is the second editor.
    monkeypatch.setattr(crew_log_store, "_slot_index", None)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, live, 8, {"phase": "implementing"}, "implement", "on #8")


def test_a_unit_directory_with_nothing_in_it_yet_does_not_refuse_the_write(tmp_path):
    """The control, and the reason the refusal is not simply "any unprovable child":
    ``create`` makes the directory and publishes the header as a second step, so a
    scan landing in that window sees a child with no content. It holds no entries, so
    leaving it out loses nothing -- and refusing on it would make every crew write
    fail whenever any session anywhere happened to be starting its log."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    root = crew_log_store._checked_crew_log_root(KIND_SESSION)
    (root / "acp-mid-create").mkdir()
    crew_log_store._slot_index = None
    cs._fold_cache.clear()

    assert crew_log_store.session_units_for_slot(cs.slot_key_for(cid), strict=True) == (sid,)
    item = _item(tmp_path, cid, sid, 11, {"phase": "implementing"})
    assert item["phase"] == "implementing", "the write went through"


def test_a_cached_listing_that_is_short_a_unit_still_refuses_the_write(tmp_path, monkeypatch):
    """The half a scan-only check misses. An unprovable child does not move the root's
    fingerprint, so once a READ has cached the shorter map the next strict caller takes
    a cache HIT and never reaches the scan. The refusal has to be re-derived on the hit
    or the validating caller is served exactly the listing the scan refused."""
    from kiro_crew.crew_log import store as crew_log_store

    crew, retired = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, retired, 7, {"phase": "implementing"}, "implement", "on #7")
    live = _unit(cid)
    slot = cs.slot_key_for(cid)
    retired_dir = crew_log_store.unit_dir_for(KIND_SESSION, retired)
    real_read = crew_log_store._read_header_line

    def header_will_not_read(path):
        if path.parent == retired_dir:
            raise OSError("the header will not read right now")
        return real_read(path)

    monkeypatch.setattr(crew_log_store, "_read_header_line", header_will_not_read)
    monkeypatch.setattr(crew_log_store, "_slot_index", None)
    cs._fold_cache.clear()

    # The read scans and CACHES the map, naming the child it could not prove.
    assert crew_log_store.session_units_for_slot(slot) == (live,)
    cached = crew_log_store._slot_index
    assert cached is not None and cached[2] == (retired_dir.name,), "cached, short a unit"

    # No scan runs now: same fingerprint, still unprovable. The refusal must still fire.
    with pytest.raises(crew_log_store.CrewLogError, match="cannot prove"):
        crew_log_store.session_units_for_slot(slot, strict=True)
    assert crew_log_store._slot_index is cached, "served from the cache, not re-scanned"


def test_a_begun_marker_that_cannot_be_written_refuses_the_write_before_any_carry(
    tmp_path, monkeypatch
):
    """The begun marker is what makes a half-done carry run again; a partial carry
    without it leaves a non-empty fold that never carries the rest. So a marker that
    cannot be written refuses the write before a single row is emitted."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    real_touch = Path.touch

    def touch(self, *a, **k):
        if self.name == cs._ITEMS_CARRY_BEGUN_MARKER:
            raise OSError("read-only")
        return real_touch(self, *a, **k)

    monkeypatch.setattr(Path, "touch", touch)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert _entries(sid) == [], "nothing was emitted"
    assert not (item_file.parent / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    # Nothing was carried, and the read says exactly that by previewing the file
    # rather than answering that the item does not exist -- the pre-projection record
    # is still on disk and still owed a carry.
    previewed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert previewed is not None and previewed["phase"] == "implementing"


def test_an_append_the_buffer_rejected_at_its_ceiling_is_a_refusal_not_a_record(
    tmp_path, monkeypatch
):
    """The writer's buffer can reject an append at SUBMISSION for crossing its memory
    ceiling; that is counted per session and apart from a storage refusal. With the
    writer also not draining inside the budget, a rejected entry is neither in the
    file nor queued -- it will never land -- so the write is refused instead of
    answering the entry as state. ANOTHER session's rejection in the same window is
    not this append's: an accepted append is answered as the queued state, never as a
    refusal that would have the caller re-send an update that lands."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_recorded = crew_log_emit.on_radar_recorded
    counts: dict[str, int] = {}
    monkeypatch.setattr(
        crew_log_emit, "overflow_writes", lambda session_id=None: counts.get(session_id, 0)
    )
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)  # saturated writer

    def rejected(session_id, data):
        counts[session_id] = counts.get(session_id, 0) + 1  # the buffer refused to hold it

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", rejected)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []

    def queued_while_another_overflows(session_id, data):
        real_recorded(session_id, data)
        counts["some-other-session"] = counts.get("some-other-session", 0) + 1

    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", queued_while_another_overflows)
    res = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert res["durable"] is False and res["item"]["phase"] == "claimed"


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
    cs._fold_cache.clear()
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


def test_an_oversized_legacy_item_is_shrunk_to_fit_and_carried(tmp_path):
    """A row of long non-ASCII text can serialize past the entry ceiling at the text
    clamp the fold applies; the carry shrinks its free text until it fits rather
    than dropping the record."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    wide = "\u4e2d" * 4000  # 4000 chars, 12-24 KB serialized -- six fields of it overflow
    _legacy_item(
        tmp_path,
        cid,
        2251,
        why=wide,
        decision=wide,
        next=wide,
        worktree=wide,
        branch=wide,
        base_sha=wide,
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert 0 < len(carried["why"]) < 4000, "shrunk, not dropped"
    marker = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    assert marker.is_file()


def test_a_number_outside_the_tools_range_is_not_retained_from_the_log(tmp_path):
    """A line the tool would have refused on magnitude is not retained on the way back
    in. A thousand-digit ``number`` read off a file is dropped exactly as a number of
    the wrong type is, so it cannot be kept as an item key, a skip key, or a
    ``pr_number``, and cannot ride in every later checkpoint and response."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    huge = int("9" * 4000)
    crew_log_emit.on_radar_recorded(
        sid,
        {
            "crew_id": cid,
            "owner": OWNER,
            "repo": REPO,
            "number": huge,
            "phase": "implementing",
            "event": "planted",
            "event_kind": "implement",
        },
    )
    # A second line whose NUMBER is legitimate, so the oversized ``pr_number`` reaches
    # the item path: without this the first line folds as a crew-level one and no item
    # is there to carry a pr_number at all.
    crew_log_emit.on_radar_recorded(
        sid,
        {
            "crew_id": cid,
            "owner": OWNER,
            "repo": REPO,
            "number": 7,
            "event": "planted a pr number",
            "event_kind": "implement",
            "pr_number": huge,
        },
    )
    assert crew_log_emit.flush(timeout=10.0)
    cs._fold_cache.clear()

    ledger = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in ledger["items"]} == {7}, "the oversized number is not an item"
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path)["pr_number"] is None
    ), "the oversized pr_number is not retained"
    assert str(huge) not in ledger["skips"]


def test_the_folds_text_clamp_is_the_cap_the_record_tool_itself_accepts(tmp_path):
    """The fold's clamp is a shape gate on bytes a reader does not control, so it must
    sit AT the largest value the record tool accepts, never below it. Below it, an
    ordinary field the tool took is shortened on every read -- silent corruption of
    state a resume acts on, not a bound."""
    from kiro_crew.crew_log import projection as crew_log
    from kiro_crew.validation import MAX_MEDIUM_STRING

    assert (
        crew_log.RADAR_TEXT_LIMIT == MAX_MEDIUM_STRING
    ), "the fold would truncate a next/decision/why/tried the tool accepted"
    assert (
        cs._MAX_CARRIED_TEXT == crew_log.RADAR_TEXT_LIMIT
    ), "the one-shot carry would permanently lose a legacy tail the fold would have kept"

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took #2251")
    long_next = "n" * MAX_MEDIUM_STRING
    item = _item(tmp_path, cid, sid, 2251, {"next": long_next})
    assert item["next"] == long_next, "a field at the tool's own cap came back whole"


def test_an_out_of_range_number_does_not_erase_the_one_already_stored(tmp_path):
    """A number the tool would refuse on magnitude is left OUT of the entry rather than
    written into it. Written, it would append fine and then be dropped by the fold, so
    the update would land and silently erase the association the item already held --
    an omitted field reads as "unchanged", which is the honest answer."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 2251, {"phase": "claimed"}, "claim", "took #2251")
    held = _item(tmp_path, cid, sid, 2251, {"pr_number": 2271, "claim_comment_id": 9911})
    assert (held["pr_number"], held["claim_comment_id"]) == (2271, 9911)

    # 0 is below the tool's floor of 1; the comment id is past its ceiling.
    after = _item(tmp_path, cid, sid, 2251, {"pr_number": 0, "claim_comment_id": 10**18 + 1})
    assert (after["pr_number"], after["claim_comment_id"]) == (
        2271,
        9911,
    ), "an out-of-range number erased the association already stored"
    last = _entries(sid)[-1].data
    assert (
        "pr_number" not in last and "claim_comment_id" not in last
    ), "the out-of-range number was written into the entry instead of left out"


def test_a_legacy_number_outside_the_tools_range_is_unfit_not_a_lost_row(tmp_path):
    """The carry bounds a number the same way, so a pre-projection file cannot introduce
    one the fold would then drop -- which on an item row would fold it as a crew-level
    line, losing the row in silence. Such a file is unfit: named, left unmarked, and
    the write refused."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    over = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path)
    record = json.loads(over.read_text(encoding="utf-8"))
    record["number"] = 10**12  # above the record tool's own max_val
    over.write_text(json.dumps(record), encoding="utf-8")
    renamed = over.with_name("1000000000000.json")
    over.rename(renamed)

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert renamed.name in str(refusal.value), "the out-of-range row is named"
    assert not (
        renamed.parent / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an out-of-range row leaves the carry unfinished"


def test_a_crew_whose_log_cannot_be_folded_reads_as_empty_and_not_as_a_crash(tmp_path, monkeypatch):
    """The repository's skip memory and its briefing read ACROSS crews, so one crew's
    damaged log must not raise into them: the pre-projection reader answered empty on
    an index it could not parse, and this keeps that contract. The write path is the
    other half -- it folds strictly, because a write validated against an empty record
    could admit a second editor."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    _pass(tmp_path, cid, sid, 41, "already fixed on main", "already-fixed")
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path), "the control: the pass is readable"

    def raising(*a, **kw):
        raise OSError("the log cannot be read")

    real_fold = cs._projection().fold_slot_checkpoint
    cs._fold_cache.clear()
    monkeypatch.setattr(cs._projection(), "fold_slot_checkpoint", raising)

    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == [], "reads as the empty record"
    assert cs.read_skips(OWNER, REPO, tmp_path) == {}, "the repository's read still answers"
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path) is False
    # The write half: it folds strictly, so a fold it cannot make RAISES here rather
    # than validating this update against an empty record.
    with pytest.raises((OSError, cs.CrewLedgerNotRecorded)):
        _record(tmp_path, cid, sid, 9, {"phase": "claimed"}, "claim", "took #9")

    monkeypatch.setattr(cs._projection(), "fold_slot_checkpoint", real_fold)
    cs._fold_cache.clear()
    assert cs.is_skipped(OWNER, REPO, 41, tmp_path), "nothing was lost while it read as empty"


def test_a_legacy_row_that_cannot_fit_refuses_the_write(tmp_path, monkeypatch):
    """A valid row the log will not take is not carried, the carry is NOT marked
    finished -- the finished marker would discard stored state for good -- and the
    write that triggered the carry is REFUSED: folding a record missing that row
    could admit a second editor. The rows that fit land; the next write, once the
    row can be carried, appends only what is still missing."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    _legacy_item(tmp_path, cid, 2252)
    real_fits = crew_log_emit.radar_entry_fits
    monkeypatch.setattr(
        crew_log_emit, "radar_entry_fits", lambda d: d.get("number") != 2252 and real_fits(d)
    )

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert "2252.json" in str(refusal.value), "the refusal names the file"

    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is not None, "the fitting row landed"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is None
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is None
    ), "the refused write did not fold"
    assert not (
        crew_dir / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an unfit row leaves the carry unfinished"
    assert (crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER).exists()

    monkeypatch.setattr(crew_log_emit, "radar_entry_fits", real_fits)
    _record(tmp_path, cid, sid, 7, {"next": "still on it"}, "claim", "progress")
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()
    assert (
        len([r for r in cs.list_work_items(OWNER, REPO, cid, tmp_path) if r["number"] == 2251]) == 1
    )


def test_an_unreadable_legacy_row_refuses_the_write_and_is_named(tmp_path, caplog):
    """A legacy file the reader cannot decode is not skipped: skipping it would let
    the carry finish with that record left behind for good, and folding without it
    would leave an item that holds an editing phase out of the record the one-editor
    rule reads. The readable row lands, the unreadable one is named, the write is
    refused, and once the file reads again the next write carries it and finishes."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    torn = _legacy_item(tmp_path, cid, 2252)
    good_bytes = torn.read_bytes()
    torn.write_bytes(good_bytes[: len(good_bytes) // 2])

    with caplog.at_level("WARNING", logger=cs.logger.name):
        with pytest.raises(cs.CrewLedgerNotRecorded):
            _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    crew_dir = torn.parent
    assert (
        cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is not None
    ), "the readable row landed"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is None
    assert (
        cs.read_work_item(OWNER, REPO, cid, 7, tmp_path) is None
    ), "the refused write did not fold"
    assert not (
        crew_dir / cs._ITEMS_CARRIED_MARKER
    ).exists(), "an unreadable row leaves the carry unfinished"
    assert (crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    assert any("2252.json" in rec.getMessage() for rec in caplog.records), "the row is named"

    torn.write_bytes(good_bytes)
    _record(tmp_path, cid, sid, 7, {"next": "still on it"}, "claim", "progress")
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_legacy_row_without_a_number_takes_it_from_the_file_name(tmp_path):
    """The pre-projection writer named each item file by its number, so a record
    whose number field is missing still carries under the name it was filed as."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    path = _legacy_item(tmp_path, cid, 2251)
    record = json.loads(path.read_text(encoding="utf-8"))
    del record["number"]
    path.write_text(json.dumps(record), encoding="utf-8")

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried is not None and carried["phase"] == "implementing"
    assert (path.parent / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_skip_index_that_is_not_a_record_leaves_the_carry_unfinished(tmp_path):
    """The shared index file decoding to something other than a mapping is a row
    that could not be carried, not an empty index: the write is refused and the passes
    marker withheld, so the file is read again once it holds records."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    skips_file = _legacy_skips(tmp_path, {})
    skips_file.write_text("[]", encoding="utf-8")

    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    assert not (skips_file.parent / cs._SKIPS_CARRIED_MARKER).exists()
    assert (skips_file.parent / cs._SKIPS_CARRY_BEGUN_MARKER).exists()
    _legacy_skips(tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other"}})
    _record(tmp_path, cid, sid, 7, {"next": "on it"}, "claim", "progress")
    assert cs.read_skips(OWNER, REPO, tmp_path)["42"]["reason"] == "r"
    assert (skips_file.parent / cs._SKIPS_CARRIED_MARKER).is_file()


def test_a_crew_that_only_swept_does_not_carry_a_file_that_appears_later(tmp_path):
    """The carry is keyed on whether ANY radar entry has folded into the crew's
    record, not on whether the record holds items or passes: a crew whose every
    write was an idle sweep holds none for good, and a legacy file present after
    its first write is not merged over the record it has moved on from."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _sweep(tmp_path, cid, sid)
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == []
    _legacy_item(tmp_path, cid, 2251)

    _sweep(tmp_path, cid, sid, text="queue still empty")
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path) is None
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRIED_MARKER
    ).exists()
    assert not (
        cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent / cs._ITEMS_CARRY_BEGUN_MARKER
    ).exists()


def test_every_rejected_approach_is_carried_in_the_files_own_order(tmp_path):
    """``tried`` is the crew's memory of what it already ruled out, so carrying only
    the newest deleted the field's purpose for every pre-upgrade item -- permanently,
    since the files are never read again once the carry is marked finished. Each
    approach is its own entry, in the file's order, so the folded list still ends with
    the newest."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[
            {"approach": "first", "rejected_because": "a", "at": "2026-01-01T00:00:00Z"},
            {"approach": "second", "rejected_because": "b", "at": "2026-01-02T00:00:00Z"},
            {"approach": "third", "rejected_because": "", "at": "2026-01-03T00:00:00Z"},
        ],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert [t["approach"] for t in carried["tried"]] == ["first", "second", "third"]
    assert [t["rejected_because"] for t in carried["tried"]] == ["a", "b", ""]
    # One entry per approach, each re-stating the record, so a refusal of one leaves
    # the others folding with the item's real phase instead of a placeholder.
    rows = [e for e in _entries(sid) if e.data.get("carried") is True]
    assert len(rows) == 3
    assert [r.data["tried"]["approach"] for r in rows] == ["first", "second", "third"]
    assert {r.data["phase"] for r in rows} == {"implementing"}
    # The item ENTERS its phase once, however long its history is.
    lines = cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
    with_phase = [line for line in lines if line.get("number") == 2251 and "phase" in line]
    assert len(with_phase) == 1


def test_an_approach_the_fold_would_evict_is_counted_not_silently_dropped(tmp_path):
    """The fold keeps an item's newest ``RADAR_TRIED_LIMIT`` approaches, so a longer
    history cannot be carried whole. The rows that cannot survive the read are left
    out at the source and SAID in the event, rather than costing an entry each to be
    discarded on the way in."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    limit = cs._projection().RADAR_TRIED_LIMIT
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[{"approach": f"a{n}", "rejected_because": ""} for n in range(limit + 3)],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    approaches = [t["approach"] for t in carried["tried"]]
    assert len(approaches) == limit
    assert approaches[-1] == f"a{limit + 2}", "the newest survived"
    assert approaches[0] == "a3", "the three oldest were the ones left behind"
    line = next(
        line
        for line in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if line.get("number") == 2251 and "not carried" in line["text"]
    )
    assert "3" in line["text"]


def test_a_tried_row_the_fold_would_drop_is_counted_not_carried(tmp_path):
    """A ``tried`` row without a usable approach is not a row the fold would keep
    either, so it is counted rather than emitted as an entry that folds to nothing."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        tried=[
            {"approach": "real", "rejected_because": "r"},
            {"approach": "   "},
            "not a row",
            {"rejected_because": "no approach at all"},
        ],
    )

    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")

    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert [t["approach"] for t in carried["tried"]] == ["real"]
    assert len([e for e in _entries(sid) if e.data.get("carried") is True]) == 1
    line = next(
        line
        for line in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if line.get("number") == 2251
    )
    assert "not carried: 3" in line["text"]


def test_the_reads_an_upgraded_crews_first_turn_makes_answer_the_pending_carry(tmp_path):
    """THE READ HALF OF THE CARRY. A carry can only run from a write, but the reads
    that happen first are the ones the crew acts on: the pre-investigate nudge
    snapshot and the repository's shared skip index. Answering the empty record there
    tells a crew it has no open items and that no issue has been passed on, and it
    acts on that with forge comments that cannot be taken back. So a read previews
    what the pending carry would fold to, and appends nothing."""
    crew, sid = _live_crew(tmp_path, max_open=3)
    cid = crew["id"]
    item_file = _legacy_item(tmp_path, cid, 2251)
    skips_file = _legacy_skips(
        tmp_path, {"42": {"number": 42, "reason": "dup of #41", "scope": "duplicate"}}
    )
    before = (item_file.read_bytes(), skips_file.read_bytes())

    # No write has happened: the log is empty and the carry has not run.
    assert _entries(sid) == []

    # The two nudge-snapshot reads the reviewer named.
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [2251]
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1
    # The shared skip index, which a crew is told to check BEFORE it investigates.
    assert sorted(cs.read_skips(OWNER, REPO, tmp_path)) == ["42"]
    # And the snapshot itself, assembled from them.
    snapshot = crew_runtime.build_snapshot(OWNER, REPO, crew, tmp_path)
    assert snapshot["open_count"] == 1
    assert [i["number"] for i in snapshot["items"]] == [2251]

    # A READ, all of it: nothing appended, no marker written, the files untouched.
    assert _entries(sid) == []
    assert not (item_file.parent / cs._ITEMS_CARRIED_MARKER).exists()
    assert not (item_file.parent / cs._ITEMS_CARRY_BEGUN_MARKER).exists()
    assert not (skips_file.parent / cs._SKIPS_CARRIED_MARKER).exists()
    assert (item_file.read_bytes(), skips_file.read_bytes()) == before

    # The preview is the post-carry record BY CONSTRUCTION: the write's own carry
    # answers the same, and from then on the preview stops.
    previewed = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    _record(tmp_path, cid, sid, 2251, {"phase": "implementing"}, "implement", "on it")
    assert (item_file.parent / cs._ITEMS_CARRIED_MARKER).is_file()
    landed = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    for field in ("phase", "next", "branch", "pr_number", "claimed_at", "last_progress_at"):
        assert landed[field] == previewed["items"][0][field], field
    assert [t["approach"] for t in landed["tried"]] == [
        t["approach"] for t in previewed["items"][0]["tried"]
    ]


def test_a_crew_that_only_swept_is_not_previewed_for_a_file_that_appears_later(tmp_path):
    """The preview is reached on the write path's OWN carry trigger, so the two agree
    on when a carry is owed. A crew that has recorded -- even only an idle sweep -- has
    moved onto the log, and a file appearing beside it afterwards is not an upgrade to
    preview: previewing it would re-state retired work as live on every read."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _sweep(tmp_path, cid, sid, "queue empty")
    # The control: the crew has RECORDED, which is the write path's own carry trigger
    # -- the fold's crew_id is set by the first entry naming the crew, sweep included.
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["crew_id"] == cid
    assert not cs._carry_pending(OWNER, REPO, cid, tmp_path)

    _legacy_item(tmp_path, cid, 4242)
    cs._fold_cache.clear()

    assert cs.read_work_item(OWNER, REPO, cid, 4242, tmp_path) is None
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == []
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_a_preview_that_cannot_be_built_reads_as_the_folded_record(tmp_path, monkeypatch):
    """A read must not raise into the crew page or the pre-investigate briefing, so a
    preview that fails answers the record as folded rather than propagating."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)

    def explode(*a, **k):
        raise RuntimeError("the pre-projection files cannot be read")

    monkeypatch.setattr(cs, "_legacy_carry_rows", explode)
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path)["items"] == []
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 0


def test_the_item_bound_never_evicts_the_item_that_holds_the_edit(monkeypatch):
    """The one-editor rule is decided by scanning the items the record still HOLDS, so
    evicting the item in an editing phase makes the rule answer "no editor" and admit a
    second one -- and an append-only log cannot retract the lines the two then write. So
    an editing item is never the victim even when no terminal item is available."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 3)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "implement"}
    # The editing item is the OLDEST, which is what an eviction rule that ranks by age
    # alone picks: every item is open, so there is no terminal victim and a pool that
    # falls back to all candidates lands on exactly it.
    datas = [
        {**base, "number": 1, "phase": "implementing", "event": "editing 1"},
        {**base, "number": 2, "phase": "claimed", "event": "took 2"},
        {**base, "number": 3, "phase": "claimed", "event": "took 3"},
        {**base, "number": 4, "phase": "claimed", "event": "took 4"},
    ]
    value = _fold_entries(crew_log, datas)
    kept = sorted(item["number"] for item in value["items"])
    assert 1 in kept, "the editing item survived the bound"
    assert kept == [1, 3, 4], "an ordinary open item went instead"
    assert value["counts"]["evicted_items"] == 1
    # And the record the one-editor rule reads still names it, which is the invariant
    # the eviction was breaking.
    editing = [i for i in value["items"] if i["phase"] in cs.RADAR_EDITING_PHASES]
    assert [i["number"] for i in editing] == [1]


def test_a_retry_whose_item_was_evicted_is_not_vouched_for_by_a_surviving_skip(monkeypatch):
    """Items and passes are bounded on SEPARATE rules and evict independently, so a
    pass can outlive the work item of the very update that wrote both. Asking whether
    EITHER survived let the surviving pass stand in for the evicted item, and the
    same-digest retry -- the one thing that would put the item back -- returned early
    and left it missing."""
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setattr(crew_log, "RADAR_ITEM_LIMIT", 2)
    base = {"crew_id": "c_0a1b2c3d", "owner": OWNER, "repo": REPO, "event_kind": "skip"}
    # One update writing BOTH an item and a pass on #1. Not carried, so it is the
    # ordinary path: the item is recorded and the pass with it.
    both = {
        **base,
        "number": 1,
        "phase": "skipped",
        "event": "passed on 1",
        "skip": {"reason": "duplicate of #0", "scope": "duplicate"},
    }
    later = [
        {**base, "number": n, "phase": "claimed", "event": f"took {n}", "event_kind": "claim"}
        for n in (2, 3, 4)
    ]
    # The item bound evicts #1; the pass bound (5000) keeps its pass.
    value = _fold_entries(crew_log, [both, *later])
    assert 1 not in {item["number"] for item in value["items"]}, "the control: item evicted"
    assert "1" in value["skips"], "the control: its pass survived"

    # The retry of that same update. Three updates follow it here, so its digest is not
    # the item's last applied one; drive the exact reported case instead -- the update
    # is replayed as the newest one and then retried.
    replayed = _fold_entries(crew_log, [both, *later, both, *later, both])
    restored = [item for item in replayed["items"] if item["number"] == 1]
    assert len(restored) == 1, "the retry restored the item the skip had vouched for"
    assert restored[0]["phase"] == "skipped"
    assert "1" in replayed["skips"], "and its pass is still there"


def test_an_append_that_landed_after_the_flush_budget_publishes_its_unit_precedence(
    tmp_path, monkeypatch
):
    """Precedence is earned by an append that LANDED, so the append path notes it only
    for an entry it saw inside the flush budget. An append the writer still held when
    that budget expired lands later, and by then its unit can be retired -- it has no
    next write of its own to make the claim. The crew's next write does: the receipt
    that proves the late append landed is where the precedence is published, before
    that write's own append moves the live unit after it."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    from kiro_crew.crew_log import emit as crew_log_emit

    real_flush = crew_log_emit.flush
    monkeypatch.setattr(crew_log_emit, "flush", lambda *a, **k: False)
    # The append is made and the flush budget expires: the write answers "not durable"
    # and leaves the crew marked undrained with a receipt for that exact entry.
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert (str(cs.data_home()), cid) in cs._undrained, "the control: a receipt is held"
    assert sid not in cs._recorded_unit_order(
        OWNER, REPO, cid, tmp_path
    ), "the control: precedence was NOT published, because nothing had landed"

    # The append lands, late -- and the crew's slot has meanwhile moved to a SUCCESSOR
    # unit, which is the case with no self-correction: the old unit is retired and has
    # no next write of its own to claim precedence.
    monkeypatch.setattr(crew_log_emit, "flush", real_flush)
    assert real_flush(timeout=10.0)
    cs._fold_cache.clear()
    successor = _unit(cid)
    assert successor != sid
    _record(tmp_path, cid, successor, 8, {"phase": "claimed"}, "claim", "took #8")

    order = cs._recorded_unit_order(OWNER, REPO, cid, tmp_path)
    assert sid in order, "the retired unit's late-landed append published its precedence"
    # And in the right place: the retired unit's entry landed BEFORE the successor's, so
    # it must sort first. Publishing it AFTER the successor would be the very inversion
    # that has the retired unit's phase and next step read as the current ones.
    assert order.index(sid) < order.index(successor)
    assert cs._unit_order_path(OWNER, REPO, cid, tmp_path).is_file()
    assert (str(cs.data_home()), cid) not in cs._undrained, "the receipt is released"


def test_a_re_run_carry_does_not_re_emit_a_row_the_record_already_holds(tmp_path):
    """A carried entry re-states the file's fields as an update. A carry run again --
    its rows landed but the finished marker did not, which is what an interrupted
    carry leaves on disk -- therefore emits only rows the record LACKS: re-emitting a
    row that landed would set an item the crew has since worked back to its
    pre-projection state."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(tmp_path, cid, 2251)
    _legacy_item(tmp_path, cid, 2252)
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file(), "the control: the carry finished"
    assert cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)["phase"] == "implementing"

    # The state an interrupted carry leaves: the rows are folded, the BEGUN marker
    # stands and the finished one does not, so the next write carries again over a
    # record that already holds them.
    (crew_dir / cs._ITEMS_CARRIED_MARKER).unlink()
    cs._mark_carry_begun(crew_dir / cs._ITEMS_CARRY_BEGUN_MARKER)
    assert cs._carry_pending(OWNER, REPO, cid, tmp_path), "the control: it will carry again"

    # The crew works the carried item: live state past the file's.
    _record(
        tmp_path,
        cid,
        sid,
        2251,
        {"phase": "awaiting-ci", "next": "watch CI"},
        "implement",
        "pushed",
    )

    live = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert live["phase"] == "awaiting-ci" and live["next"] == "watch CI", "live state kept"
    assert cs.read_work_item(OWNER, REPO, cid, 2252, tmp_path) is not None, "the other row stands"
    carried_2251 = [
        e for e in _entries(sid) if e.data.get("carried") is True and e.data.get("number") == 2251
    ]
    # One entry per rejected approach the file held, and the re-run added none: the
    # count is the file's `tried` length, not the number of carry runs.
    assert len(carried_2251) == 2, "the row that landed was emitted once, once per approach"
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_a_unit_recreated_under_its_id_folds_cold_however_far_its_seq_climbed(tmp_path):
    """The checkpoint cache is keyed by each unit's log identity, not by its seq
    alone: a log removed and created again under the same id, whose new entries take
    its seq PAST the cached one, is a different log, and the read must fold it from
    the start rather than continue the retired log's checkpoint.

    The new entries are appended straight through the emitter, because a store write
    folds before it appends -- so writing them would refresh the cache while the new
    log's seq was still below the cached one, and the stale checkpoint would never be
    offered to the comparison this pins.
    """
    from kiro_crew.crew_log.store import segment_paths

    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _item(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim")
    warm = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in warm["items"]} == {7}, "the retired log's item is cached"
    retired_seq = cs._unit_last_seq(sid)

    crew_log_emit.reset_caches()
    for path in segment_paths(KIND_SESSION, sid):
        path.unlink()
    _tick()
    _unit(cid, session_id=sid)
    for number in (11, 13, 15, 17):
        built = cs._legacy_item_entries(OWNER, REPO, cid, {"number": number, "phase": "claimed"})
        assert len(built) == 1
        crew_log_emit.on_radar_recorded(sid, built[0])
    assert crew_log_emit.flush(timeout=10.0)
    assert cs._unit_last_seq(sid) > retired_seq, "the control: the new log outran the cached seq"

    fresh = cs.read_ledger(OWNER, REPO, cid, tmp_path)
    assert {i["number"] for i in fresh["items"]} == {11, 13, 15, 17}, "no item of the retired log"
    cs._fold_cache.clear()
    assert cs.read_ledger(OWNER, REPO, cid, tmp_path) == fresh


def test_an_oversized_legacy_ci_member_is_bounded_not_a_lost_row(tmp_path):
    """A legacy file's ``ci_state`` can hold anything. One oversized member would push
    an otherwise valid row past the entry ceiling and leave it uncarried; the carry
    bounds each member the way the record tool bounds it on the way in, so the row
    lands with the member clipped and the ill-typed members dropped."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        ci_state={
            "state": "x" * 300_000,  # far past the entry ceiling on its own
            "passed": 3.7,  # fractional: not a counter
            "total": "9",  # a string: not a counter
            "round": 10**9,  # past the tool's ceiling
            "inherited_reds": 2,
            "stray": {"deep": ["not", "a", "member"]},
        },
    )
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    item = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert item is not None, "the row was carried"
    assert item["ci_state"] == {"state": "x" * 32, "inherited_reds": 2}
    crew_dir = cs.work_item_path(OWNER, REPO, cid, 2251, tmp_path).parent
    assert (crew_dir / cs._ITEMS_CARRIED_MARKER).is_file()


def test_the_radar_ci_bounds_and_label_limit_are_the_record_tools_own():
    """The fold's per-member CI bounds and label count are copied from the record
    tool's field specs; this pins the copy so the two cannot drift apart silently."""
    from kiro_crew.crew_log import entry_types
    from kiro_crew.validation import ISSUE_RADAR_CREW_RECORD_SCHEMA

    specs = {f.name: f for f in ISSUE_RADAR_CREW_RECORD_SCHEMA.fields}
    assert set(entry_types.RADAR_CI_BOUNDS) == set(entry_types.RADAR_CI_KEYS)
    for key, (kind, bound) in entry_types.RADAR_CI_BOUNDS.items():
        spec = specs[f"ci_{key}"]
        assert spec.type is kind
        assert bound == (spec.max_len if kind is str else spec.max_val)
    assert entry_types.RADAR_LABELS_LIMIT == specs["labels_applied"].max_items
    for field, (low, high) in entry_types.RADAR_NUMBER_BOUNDS.items():
        spec = specs[field]
        assert spec.type is int
        assert (low, high) == (spec.min_val, spec.max_val), field


def test_an_undrained_append_is_drained_before_the_crews_next_write_folds(tmp_path, monkeypatch):
    """A write whose append the writer had not drained answers ``durable: false``; the
    crew's next write drains again BEFORE it folds, so it does not validate against
    a record missing that entry."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_flush = crew_log_emit.flush
    calls: list[bool] = []

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        calls.append(drained)
        return False if len(calls) == 1 else drained  # the first write's drain "times out"

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    first = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert first["durable"] is False
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"
    _record(tmp_path, cid, sid, 7, {"next": "read it"}, "claim", "progress")
    assert len(calls) == 3, "the second write drained once before folding and once after its append"
    assert not any(key[1] == cid for key in cs._undrained)


def test_a_write_that_never_landed_does_not_claim_the_newest_entry(tmp_path, monkeypatch):
    """The order file's claim is "among the units that recorded, this one holds the
    newest entry", so a unit whose append never reached the file must not be moved
    ahead of one whose did.

    A unit pinned ahead of its successor is the inversion the order exists to
    prevent: a later unit resuming with no entries of its own folds the pinned
    unit's phase and next step as current and acts on them. The note therefore
    belongs AFTER the append, gated on this unit's own log having grown -- not in
    ``_prepare_write``, which runs before anything is appended at all.
    """
    crew, first = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, first, 7, {"phase": "claimed"}, "claim", "took #7")
    second = _unit(cid)
    _record(tmp_path, cid, second, 7, {"next": "read it"}, "claim", "progress")
    before = cs._recorded_unit_order(OWNER, REPO, cid, tmp_path)
    assert before == (first, second), "a landed write orders its unit last"

    # The writer never drains, so nothing this write appends is in the file.
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)
    answer = _record(tmp_path, cid, first, 7, {"next": "queued"}, "claim", "not durable")
    assert answer["durable"] is False
    assert (
        cs._recorded_unit_order(OWNER, REPO, cid, tmp_path) == before
    ), "a write that did not land earned no precedence"


def test_a_writer_still_behind_at_the_next_write_refuses_it_rather_than_fold_stale(
    tmp_path, monkeypatch
):
    """The crew's last append is still queued and the pre-write drain times out too:
    a fold taken now would be missing that entry, and the one-editor rule checked
    against it could admit a second editor. The write is refused (503, nothing
    changed, send it again); once the writer drains the same write goes through."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    real_flush = crew_log_emit.flush
    behind = {"on": True}

    def flush(timeout=None):
        drained = real_flush(timeout=timeout)
        return False if behind["on"] else drained

    monkeypatch.setattr(crew_log_emit, "flush", flush)
    first = _record(tmp_path, cid, sid, 7, {"phase": "implementing"}, "implement", "on #7")
    assert first["durable"] is False
    appended = len(_entries(sid))
    # #8 would be a SECOND editor: with the fold missing #7's entry it would pass.
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")
    assert len(_entries(sid)) == appended, "the refused write appended nothing"
    assert any(key[1] == cid for key in cs._undrained), "the mark stays until a drain succeeds"

    behind["on"] = False
    with pytest.raises(cs.CrewStoreError, match="already editing #7"):
        _record(tmp_path, cid, sid, 8, {"phase": "implementing"}, "implement", "on #8")
    assert not any(key[1] == cid for key in cs._undrained)


def test_a_quiet_writer_whose_entry_never_landed_refuses_the_next_write(tmp_path, monkeypatch):
    """A DROP empties the writer's buffer exactly as a successful write does, so
    ``flush`` answers True either way. Releasing the undrain mark on that alone would
    fold a record short one entry and hand the one-editor rule a record that permits a
    second editor -- silently, because the earlier call already answered. The mark
    carries a receipt for the queued entry, so it is released only once that entry is
    found in its unit's own log by content."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    appended = len(_entries(sid))

    # This append is dropped on the floor and its drain reports a timeout, so the crew
    # is marked undrained for an entry that is not in the log and never will be.
    monkeypatch.setattr(crew_log_emit, "on_radar_recorded", lambda *a, **k: None)
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: False)
    answer = _record(tmp_path, cid, sid, 7, {"next": "dropped"}, "claim", "queued")
    assert answer["durable"] is False
    assert any(key[1] == cid for key in cs._undrained), "the crew is marked undrained"
    assert len(_entries(sid)) == appended, "nothing of that write reached the log"

    # The writer is now QUIET -- but the entry still is not there.
    monkeypatch.setattr(crew_log_emit, "flush", lambda timeout=None: True)
    with pytest.raises(cs.CrewLedgerNotRecorded):
        _record(tmp_path, cid, sid, 7, {"next": "after"}, "claim", "next write")
    assert any(key[1] == cid for key in cs._undrained), "a quiet writer did not clear the mark"


def test_a_carried_item_that_was_finished_stays_finished(tmp_path):
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    _legacy_item(
        tmp_path,
        cid,
        2251,
        phase="resolved",
        outcome="merged in #2271",
        finished_at="2026-01-03T00:00:00Z",
    )
    _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    carried = cs.read_work_item(OWNER, REPO, cid, 2251, tmp_path)
    assert carried["phase"] == "resolved"
    assert carried["finished_at"] == "2026-01-03T00:00:00Z"
    assert carried["outcome"] == "merged in #2271"
    assert cs.open_slot_count(OWNER, REPO, cid, tmp_path) == 1


def test_a_malformed_pre_projection_file_refuses_the_write_and_is_named(tmp_path):
    """A malformed pre-projection file is neither skipped nor fatal to the STORE: the
    write is refused, retryably, with the file named, and nothing of the record is
    lost. Skipping such a file would discard its stored state for good once the carry
    was marked finished, and folding without it could let a second item into an
    editing phase the omitted row still holds."""
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]
    bad = cs.work_item_path(OWNER, REPO, cid, 1, tmp_path)
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not json", encoding="utf-8")
    (bad.parent / "2.json").write_text('"just a string"', encoding="utf-8")
    _legacy_skips(tmp_path, {"x": {"reason": "no number"}, "42": "not a row"})

    with pytest.raises(cs.CrewLedgerNotRecorded) as refusal:
        _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert "1.json" in str(refusal.value), "the malformed file is named"

    assert cs.read_skips(OWNER, REPO, tmp_path) == {}
    assert cs.list_work_items(OWNER, REPO, cid, tmp_path) == [], "the refused write did not fold"

    # Repaired, the same update goes through and the carry finishes.
    bad.unlink()
    (bad.parent / "2.json").unlink()
    _legacy_skips(tmp_path, {"42": {"number": 42, "reason": "r", "scope": "other"}})
    result = _record(tmp_path, cid, sid, 7, {"phase": "claimed"}, "claim", "took #7")
    assert result["item"]["phase"] == "claimed"
    assert [i["number"] for i in cs.list_work_items(OWNER, REPO, cid, tmp_path)] == [7]


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

    stored = cs.write_settings(OWNER, REPO, {"claim_ttl_hours": json.loads(literal)}, tmp_path)

    assert stored["claim_ttl_hours"] == 12
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 12
    # And the file it wrote is still JSON — `json.dumps` would have emitted a bare
    # `Infinity`, which no strict parser reads.
    _assert_strict_json(cs.settings_path(OWNER, REPO, tmp_path))


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_max_open_is_ignored_like_any_out_of_range_value(tmp_path, literal):
    crew = _crew(tmp_path)
    updated = cs.update_crew(OWNER, REPO, crew["id"], {"max_open": json.loads(literal)}, tmp_path)
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
def test_a_non_finite_work_item_number_records_as_none(tmp_path, field, literal):
    crew, sid = _live_crew(tmp_path)
    item = _item(tmp_path, crew["id"], sid, 2251, {field: json.loads(literal)})
    assert item[field] is None
    # Left OUT of the entry rather than written as null: the line is JSON a strict
    # reader parses, and an absent field reads as "unchanged" to the fold.
    (entry,) = _entries(sid)
    assert field not in entry.data


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

    sid = _unit(crew["id"])
    item = _item(tmp_path, crew["id"], sid, 2251, {"pr_number": 2271, "claim_comment_id": 9911})
    assert (item["pr_number"], item["claim_comment_id"]) == (2271, 9911)
    # A later write must not blank a field it was not given.
    carried = _item(tmp_path, crew["id"], sid, 2251, {"next": "rebase"})
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


def test_a_write_that_does_not_move_the_item_leaves_phase_off_the_event_line(tmp_path):
    """`phase` on an event line means an ENTRY into that phase, so a no-move write
    must not carry one.

    The fabric measures an open dwell from the MOST RECENT entry into the current
    phase, which is what a review pass legitimately restarts. Stamping the
    phase on every write makes each CI round -- which lands `ci_state` while the
    item sits still in `awaiting-ci` -- look like a fresh entry, so an item parked
    for hours reads as minutes old and never surfaces as stalled. The item polled
    most often is the one whose stall would be hidden best, which is the exact
    inversion of what the view is for.
    """
    crew, sid = _live_crew(tmp_path)
    cid = crew["id"]

    entered = _record(tmp_path, cid, sid, 7301, {"phase": "awaiting-ci"}, "ci", "round 1")
    assert entered["event"].get("phase") == "awaiting-ci", "an entry must be recorded"

    for round_no in (2, 3):
        polled = _record(
            tmp_path,
            cid,
            sid,
            7301,
            {"ci_state": {"state": "pending", "round": round_no}},
            "ci",
            f"round {round_no}",
        )
        assert "phase" not in polled["event"], (
            "a write that did not move the item must not claim a phase entry "
            f"(round {round_no} did)"
        )

    moved = _record(tmp_path, cid, sid, 7301, {"phase": "implementing"}, "implement", "fix")
    assert moved["event"].get("phase") == "implementing", "a real move must be recorded"

    # And a genuine RE-entry still records, so a round-trip keeps restarting the clock.
    back = _record(tmp_path, cid, sid, 7301, {"phase": "awaiting-ci"}, "ci", "round 4")
    assert back["event"].get("phase") == "awaiting-ci", "a re-entry must be recorded"

    phases = [
        ev.get("phase")
        for ev in cs.read_events(OWNER, REPO, tmp_path, crew_id=cid)
        if ev.get("number") == 7301
    ]
    assert [p for p in phases if p] == [
        "awaiting-ci",
        "implementing",
        "awaiting-ci",
    ], f"only entries should carry a phase, got {phases}"
    # The per-item phase history the pipeline view draws lanes from says the same.
    assert [
        row["phase"] for row in cs.read_ledger(OWNER, REPO, cid, tmp_path)["phase_lines"]["7301"]
    ] == [
        "awaiting-ci",
        "implementing",
        "awaiting-ci",
    ]
