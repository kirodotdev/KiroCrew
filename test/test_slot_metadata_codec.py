"""The slot metadata codec, tested at its interface.

``slot_persistence.metadata_codec`` owns every slot field a session's metadata line
carries: how each save form writes it (``encode``) and which hydration purposes read
it back (``slot_args`` + ``apply`` + ``AppliedMeta.settle``). These tests drive that
interface with real slots on a temp-home ``DashboardState``:

* the round trip -- for every row a purpose reads, ``apply(fresh, encode(slot))``
  reproduces the field, and for every row it does not read the fresh slot keeps
  what its constructor gave it (each declared asymmetry, observed);
* the write forms -- both save forms write every slot-owned key, the full save
  clears by absence and the merge by a falsy value, in their recorded key order;
* the purpose-specific reads the table's ``why`` column declares.
"""

from __future__ import annotations

import copy
import json

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_persistence as cp
from kiro_crew.dashboard.relay_archive import relay_archive_refusal
from kiro_crew.dashboard.slot_buffers import (
    sanitize_restored_deferred_notes,
    serialize_deferred_notes,
)
from kiro_crew.dashboard.slot_persistence import metadata_codec as codec
from kiro_crew.dashboard.slot_persistence import metadata_line
from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
from kiro_crew.dashboard.state import durable_queue_entries
from kiro_crew.history import SLOT_OWNED_META_KEYS

K1 = source_ref_identity_key(("github", "github.com", "o", "r", 1, "", "change", ""))
K2 = source_ref_identity_key(("github", "github.com", "o", "r", 2, "", "change", ""))
PROMPT = {
    "role": "user",
    "content": "the opener",
    "ts": "2026-01-01T00:00:09",
    "meta": {"mid": "m-o"},
}

#: Every field set to a value no constructor would give it.
_POPULATED: dict = {
    "created_at": "2026-01-01T00:00:00",
    "title": "My Title",
    "_titled": True,
    "_title_origin": "auto",
    "_title_refresh_mark": 5,
    "_title_low_signal": True,
    "agent": "agent-a",
    "model": "model-m",
    "reasoning_effort": "high",
    "autocompact_pct": 44.0,
    "_dismissed_source_links": {K1, K2},
    "workspace": "ws",
    "memory_store": "silo",
    "agent_kind": "member",
    "project": "/srv/p",
    "executor": "remote",
    "instance_id": "inst-1",
    "remote_slot": "rs-1",
    "_turn_in_flight_generation": 2,
    "_turn_in_flight_prompt": PROMPT,
    "mode": "focus",
    "_created_by": "member-x",
    "folder_id": "folder-1",
    "_channel_folder_filed": True,
    "_origin": "cron",
    "_artifact": "my-artifact",
    "pinned": True,
    "mutes_opened": True,
    "color_index": 3,
    "color_hex": "#abcdef",
    "color_theme": "dark",
    "tags": ["t1", "t2"],
    "_auto_tagged": True,
    "_human_seen": True,
    "forked_from": "parent-slot",
    "forked_from_created_at": "2026-01-01T00:00:00",
    "linked_session_key": "slack:C1:1.2",
    "channel_origin": True,
    "_tab_id": "tab-src",
}

_NOTES = [
    {"id": "n-1", "content": "held", "session": "dashboard:src"},
    {"id": "n-2", "content": "delivered", "session": "dashboard:src"},
]
_QUEUE = [{"id": "q-1", "content": "queued", "meta": {"sendId": "s1"}}]

#: Fields whose value lands on ``AppliedMeta`` (the reader acts on it) rather than
#: on the slot.
_APPLIED = {
    "turn_in_flight_generation": "turn_generation",
    "turn_in_flight_prompt": "turn_prompt",
}


#: The key order each save form writes: the bytes a save puts on disk, as the two
#: hand-written save sites wrote them before the codec. A change here is a change to
#: every saved file.
_BASE_LINE_ORDER = (
    "_type",
    "created_at",
    "last_consolidated",
    "closed",
    "closed_at",
    "memory_mode",
    "title",
    "title_origin",
    "title_refresh_mark",
    "title_low_signal",
    "agent",
    "model",
    "reasoning_effort",
    "autocompact_pct",
    "dismissed_source_links",
    "mode",
    "workspace",
    "memory_store",
    "agent_kind",
    "project",
    "executor",
    "instance_id",
    "remote_slot",
    "turn_in_flight_generation",
    "turn_in_flight_prompt",
    "folder_id",
    "channel_folder_filed",
    "app",
    "origin",
    "created_by",
    "artifact",
    "pinned",
    "mutes_opened",
    "color_index",
    "color_hex",
    "color_theme",
    "tags",
    "auto_tagged",
    "human_seen",
    "last_user_at",
    "deferred_notes",
    "queued_prompts",
    "forked_from",
    "forked_from_created_at",
    "linked_session_key",
    "channel_origin",
    "tab_id",
    "rotation_generation",
)
_BASE_MERGE_ORDER = (
    "folder_id",
    "tags",
    "pinned",
    "mutes_opened",
    "mode",
    "artifact",
    "reasoning_effort",
    "color_index",
    "color_hex",
    "color_theme",
    "memory_mode",
    "model",
    "queued_prompts",
    "autocompact_pct",
    "title",
    "title_origin",
    "title_refresh_mark",
    "title_low_signal",
    "agent",
    "workspace",
    "memory_store",
    "agent_kind",
    "project",
    "app",
    "origin",
    "created_by",
    "linked_session_key",
    "channel_origin",
    "forked_from",
    "forked_from_created_at",
    "turn_in_flight_generation",
    "turn_in_flight_prompt",
    "executor",
    "instance_id",
    "remote_slot",
    "tab_id",
    "auto_tagged",
    "human_seen",
    "channel_folder_filed",
    "closed",
    "closed_at",
    "dismissed_source_links",
    "deferred_notes",
)


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state._tags = [{"id": "t1"}, {"id": "t2"}]
    state._tags_authoritative = True
    return state


def _populated(state, *, app: str = "app-1"):
    slot = state.get_or_create_slot("src", app=app)
    for attr, value in _POPULATED.items():
        setattr(slot, attr, copy.deepcopy(value))
    slot._deferred_notes = sanitize_restored_deferred_notes(copy.deepcopy(_NOTES))
    slot._queue[:] = copy.deepcopy(_QUEUE)
    return slot


def _purpose(purpose: str, *, name: str, member=None, listed: object = "Listed"):
    if purpose == codec.RESTORE:
        return codec.Restore(name=name, member=member)
    if purpose == codec.RECENT:
        return codec.Recent(
            name=name, member=member, listing={"title": listed}, history_key=f"dashboard_{name}"
        )
    return codec.Resume(member=member, history_key=f"dashboard:{name}")


def _slot_folds(slot) -> codec.SaveFolds:
    """What a save of *slot* folds when the line on disk adds nothing: its own values."""
    return codec.SaveFolds(
        memory_mode=slot.memory_mode,
        created_at=slot.created_at,
        dismissed_source_links=sorted(slot._dismissed_source_links) or None,
        deferred_notes=serialize_deferred_notes(slot._deferred_notes[:]),
        queued_prompts=slot.durable_queue_entries(),
        channel_folder_filed=slot._channel_folder_filed,
        tab_id=slot._tab_id,
    )


def _read_back(state, meta: dict, purpose: str, *, name: str = "", **kwargs):
    """Construct a slot the way a reader does and apply *meta* to it."""
    name = name or f"dst-{purpose}"
    chosen = _purpose(purpose, name=name, **kwargs)
    slot = state.get_or_create_slot(name, **codec.slot_args(meta, chosen))
    baseline = {
        row.attr: copy.deepcopy(getattr(slot, row.attr)) for row in codec.FIELDS if row.attr
    }
    applied = codec.apply(state, slot, meta, chosen)
    return slot, applied, baseline


def _value(slot, applied, row):
    if row.key in _APPLIED:
        return getattr(applied, _APPLIED[row.key])
    value = getattr(slot, row.attr)
    if row.key == "queued_prompts":
        return durable_queue_entries(value)
    if isinstance(value, set):
        return sorted(value)
    return value


def _expected(source, row):
    if row.key == "queued_prompts":
        return durable_queue_entries(source._queue)
    return _value(source, None, row) if row.key not in _APPLIED else getattr(source, row.attr)


@pytest.mark.parametrize("merge", [False, True], ids=["line", "merge"])
@pytest.mark.parametrize("purpose", sorted(codec.PURPOSES))
def test_every_field_a_purpose_reads_round_trips_and_every_other_is_left_alone(
    purpose, merge, tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch)
    source = _populated(state)
    meta = json.loads(json.dumps(codec.encode(source, merge=merge, folds=_slot_folds(source))))
    # A recent restore shows the session list's title, which is the line's.
    fresh, applied, baseline = _read_back(state, meta, purpose, listed=meta.get("title"))

    write = "merge" if merge else "line"
    restored, kept, settled = [], [], []
    for row in codec.FIELDS:
        if not row.attr or getattr(row, write) is None:
            continue
        if purpose in row.purposes:
            if row.key in _APPLIED:
                settled.append(row)
            else:
                assert _value(fresh, applied, row) == _expected(source, row), row.key
            restored.append(row.key)
        else:
            if row.key not in _APPLIED:
                assert _value(fresh, applied, row) == baseline[row.attr], row.key
            kept.append(row.key)
    # The marker RECENT and RESUME parse after the window is read once settled.
    applied.settle([])
    for row in settled:
        assert _value(fresh, applied, row) == _expected(source, row), row.key
    # Non-vacuous: every purpose restores most fields, and each declares an asymmetry.
    assert len(restored) >= 20
    assert kept


def test_the_round_trip_sees_a_dropped_read(tmp_path, monkeypatch):
    """Red check: a purpose that silently stops reading a field fails the round trip."""
    state = _state(tmp_path, monkeypatch)
    source = _populated(state)
    meta = json.loads(json.dumps(codec.encode(source, folds=_slot_folds(source))))
    rows = tuple(
        codec.Field(**{**row.__dict__, "read": None}) if row.key == "pinned" else row
        for row in codec.FIELDS
    )
    monkeypatch.setattr(codec, "FIELDS", rows)
    fresh, _, _ = _read_back(state, meta, codec.RESTORE)
    assert fresh.pinned is False


def test_the_table_declares_every_asymmetry_the_readers_had():
    """The purposes each row is read by. A change here is a behaviour change."""
    partial = {
        row.key: sorted(row.purposes)
        for row in codec.FIELDS
        if row.purposes and row.purposes != codec.PURPOSES
    }
    assert partial == {
        "model": ["recent", "restore"],
        "reasoning_effort": ["recent", "restore"],
        "memory_store": ["recent", "restore"],
        "executor": ["recent", "restore"],
        "instance_id": ["recent", "restore"],
        "remote_slot": ["recent", "restore"],
        "created_by": ["recent", "restore"],
        "app": ["recent", "restore"],
        "artifact": ["recent", "restore"],
        "color_theme": ["recent", "resume"],
        "theme_consent": ["resume"],
        "theme_consent_sha": ["resume"],
        "human_seen": ["recent", "restore"],
        "queued_prompts": ["recent", "restore"],
        "linked_session_key": ["recent", "restore"],
        "channel_origin": ["restore"],
        "tab_id": ["recent", "restore"],
    }
    assert all(row.why for row in codec.FIELDS if row.key in partial)
    assert [row.key for row in codec.FIELDS if not row.purposes] == [
        "_type",
        "last_consolidated",
        "last_user_at",
        "closed",
        "closed_at",
        "rotation_generation",
    ]


def test_the_table_has_one_row_per_key_and_both_orders_name_exactly_the_written_keys():
    keys = [row.key for row in codec.FIELDS]
    assert len(keys) == len(set(keys))
    assert sorted(codec.LINE_ORDER) == sorted(row.key for row in codec.FIELDS if row.line)
    assert sorted(codec.MERGE_ORDER) == sorted(row.key for row in codec.FIELDS if row.merge)
    assert len(codec.LINE_ORDER) == len(set(codec.LINE_ORDER))
    assert len(codec.MERGE_ORDER) == len(set(codec.MERGE_ORDER))
    assert all(row.purposes <= codec.PURPOSES for row in codec.FIELDS)


def _full_folds(slot) -> codec.SaveFolds:
    return codec.SaveFolds(
        memory_mode="persistent",
        created_at="2026-01-01T00:00:00",
        closed=True,
        closed_at=1767225600.0,
        dismissed_source_links=[K1],
        deferred_notes=[{"id": "n-1", "content": "held", "session": "dashboard:src"}],
        queued_prompts=list(_QUEUE),
        last_user_at="2026-01-01T00:00:05",
        channel_folder_filed=True,
        tab_id="tab-src",
        rotation_generation=3,
    )


def test_both_save_forms_write_every_slot_owned_key(tmp_path, monkeypatch):
    """Absence means CLEARED on a full save, and a merge cannot delete a key, so a
    slot-owned key one form never writes either resurrects a stale value or clears a
    live one. Observed through what each form writes for a slot with every field set."""
    state = _state(tmp_path, monkeypatch)
    slot = _populated(state)
    line = codec.encode(slot, folds=_full_folds(slot))
    merge = codec.encode(slot, merge=True, folds=_full_folds(slot))
    assert sorted(SLOT_OWNED_META_KEYS - set(line)) == []
    assert (
        sorted(SLOT_OWNED_META_KEYS - {"_type", "created_at", "last_consolidated"} - set(merge))
        == []
    )
    assert list(line) == [key for key in _BASE_LINE_ORDER if key in line]
    assert list(merge) == [key for key in _BASE_MERGE_ORDER if key in merge]
    assert codec.LINE_ORDER == _BASE_LINE_ORDER and codec.MERGE_ORDER == _BASE_MERGE_ORDER


def test_the_coverage_check_sees_a_form_that_drops_a_key(tmp_path, monkeypatch):
    """Red check for the test above: a row whose merge writer is gone is reported."""
    state = _state(tmp_path, monkeypatch)
    slot = _populated(state)
    rows = {row.key: row for row in codec.FIELDS}
    monkeypatch.setitem(
        codec._BY_KEY, "pinned", codec.Field(**{**rows["pinned"].__dict__, "merge": None})
    )
    merge = codec.encode(slot, merge=True, folds=_full_folds(slot))
    assert "pinned" in SLOT_OWNED_META_KEYS - set(merge)


def test_the_full_save_clears_by_absence_and_the_merge_by_a_falsy_value(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    bare = state.get_or_create_slot("bare")
    folds = codec.SaveFolds(memory_mode="persistent")
    line = codec.encode(bare, folds=folds)
    merge = codec.encode(bare, merge=True, folds=folds)
    cleared = {
        "folder_id": "",
        "tags": [],
        "pinned": False,
        "mode": "",
        "artifact": "",
        "reasoning_effort": "",
        "color_index": None,
        "color_hex": "",
        "color_theme": "",
        "queued_prompts": [],
        "title": "",
        "memory_store": "",
        "agent_kind": "",
        "project": "",
        "turn_in_flight_generation": 0,
        "turn_in_flight_prompt": None,
        "deferred_notes": [],
    }
    assert {key: merge[key] for key in cleared} == cleared
    assert not set(cleared) & set(line)
    # Identity and once-flags are never written as cleared values by either form.
    for key in ("agent", "app", "origin", "created_by", "auto_tagged", "human_seen", "executor"):
        assert key not in merge and key not in line, key
    # The two forms differ on the default workspace.
    assert merge["workspace"] == "default" and "workspace" not in line


def test_slot_args_take_each_purposes_construction_inputs():
    meta = {"app": "app-1", "origin": "cron", "linked_session_key": "slack:C1:1.2"}
    assert codec.slot_args(meta, codec.Restore(name="x")) == {
        "agent": "",
        "mode": "",
        "app": "app-1",
        "channel_origin": True,
        "origin": "cron",
    }
    assert codec.slot_args(meta, codec.Recent(name="x")) == {
        "agent": "",
        "mode": "",
        "app": "app-1",
        "origin": "cron",
    }
    assert codec.slot_args(
        meta, codec.Resume(member=("crew-a", "member"), app="req-app", history_key="slack_C1_1.2")
    ) == {
        "agent": "crew-a",
        "mode": "member",
        "app": "req-app",
        "channel_origin": True,
        "origin": "cron",
    }
    assert codec.slot_args({}, codec.Resume(history_key="dashboard:x"))["channel_origin"] is False


@pytest.mark.parametrize("purpose", sorted(codec.PURPOSES))
def test_a_member_pin_keeps_the_bindings_agent_and_mode(purpose, tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    meta = {"agent": "line-agent", "mode": "focus"}
    chosen = _purpose(purpose, name="member-crew-a", member=("crew-a", "member"), listed="")
    slot = state.get_or_create_slot("member-crew-a", **codec.slot_args(meta, chosen))
    codec.apply(state, slot, meta, chosen)
    assert (slot.agent, slot.mode) == ("crew-a", "member")


def test_each_purpose_takes_the_title_from_its_own_source(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    untitled = {"title_origin": "auto"}
    restore, _, _ = _read_back(state, dict(untitled), codec.RESTORE, name="r")
    assert (restore.title, restore._titled, restore._title_origin) == ("r", False, "")
    recent, _, _ = _read_back(state, dict(untitled), codec.RECENT, name="a")
    assert (recent.title, recent._titled, recent._title_origin) == ("Listed", True, "auto")
    chosen = codec.Resume(request_title="Asked", history_key="dashboard:h")
    resume = state.get_or_create_slot("h", **codec.slot_args(untitled, chosen))
    epoch = resume._title_epoch
    codec.apply(state, resume, dict(untitled), chosen)
    assert (resume.title, resume._titled, resume._title_origin) == ("Asked", True, "user")
    assert resume._title_epoch == epoch + 1


def test_a_non_string_title_fails_a_restore_but_reads_as_absent_on_resume(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    chosen = codec.Restore(name="r")
    slot = state.get_or_create_slot("r", **codec.slot_args({"title": 5}, chosen))
    with pytest.raises(TypeError):
        codec.apply(state, slot, {"title": 5}, chosen)
    resume, _, _ = _read_back(state, {"title": 5}, codec.RESUME)
    assert resume._titled is False


def test_a_restore_reads_the_committed_agent_not_the_lines(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    meta = {"agent": "line-agent"}
    chosen = codec.Restore(name="r", agent="committed")
    slot = state.get_or_create_slot("r", **codec.slot_args(meta, chosen))
    codec.apply(state, slot, meta, chosen)
    assert slot.agent == "committed"
    resume, _, _ = _read_back(state, meta, codec.RESUME)
    assert resume.agent == "line-agent"


def test_a_line_with_no_model_falls_back_to_the_agents_configured_model(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    chosen = codec.Restore(name="r", agent="writer", model_map={"writer": "model-of-writer"})
    slot = state.get_or_create_slot("r", **codec.slot_args({}, chosen))
    codec.apply(state, slot, {}, chosen)
    assert slot.model == "model-of-writer"
    resume, _, _ = _read_back(state, {"agent": "writer"}, codec.RESUME)
    assert resume.model == ""


@pytest.mark.parametrize(
    ("raw", "restored"),
    [("focus", "focus"), ("crew", ""), ("orchestrator", ""), (7, ""), ("member", "member")],
)
def test_a_retired_or_malformed_mode_reads_as_plain_chat(raw, restored, tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _, _ = _read_back(state, {"mode": raw}, codec.RESTORE)
    assert slot.mode == restored
    resume, _, _ = _read_back(state, {"mode": raw}, codec.RESUME)
    assert resume.mode == ("" if raw == "member" else restored)


def test_the_restricted_mark_follows_the_line_and_resume_clears_a_stale_one(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    _read_back(state, {"memory_mode": "ephemeral"}, codec.RESTORE, name="r")
    _read_back(state, {"memory_mode": "ephemeral"}, codec.RECENT, name="a")
    assert {"dashboard:r", "dashboard:a"} <= state._restricted_keys
    state._restricted_keys.update({"dashboard:p", "dashboard:h"})
    _read_back(state, {"memory_mode": "persistent"}, codec.RESTORE, name="p")
    assert "dashboard:p" in state._restricted_keys
    _read_back(state, {"memory_mode": "persistent"}, codec.RESUME, name="h")
    assert "dashboard:h" not in state._restricted_keys


@pytest.mark.parametrize(
    ("instance_id", "remote_slot", "expected"),
    [("inst-1", "rs-1", ("remote", "inst-1", "rs-1")), ("", 5, ("remote", "", ""))],
    ids=["complete", "incomplete"],
)
def test_an_old_relay_line_keeps_the_archive_marker_and_ignores_its_relay_flag(
    tmp_path, monkeypatch, instance_id, remote_slot, expected
):
    state = _state(tmp_path, monkeypatch)
    meta = {
        "executor": "remote",
        "instance_id": instance_id,
        "remote_slot": remote_slot,
        "relay_in_flight": True,
    }
    slot, applied, _ = _read_back(state, meta, codec.RESTORE)
    assert (slot.executor, slot.instance_id, slot.remote_slot) == expected
    assert not hasattr(applied, "relay_in_flight")


def test_a_recent_restored_relay_chat_stays_a_read_only_archive(tmp_path, monkeypatch):
    """The recent-sessions restore keeps the relay marker: an old relay chat comes
    back a read-only archive, and the next save writes the binding back."""
    state = _state(tmp_path, monkeypatch)
    meta = {"executor": "remote", "instance_id": "inst-1", "remote_slot": "rs-1"}
    slot, _, _ = _read_back(state, meta, codec.RECENT)
    assert (slot.executor, slot.instance_id, slot.remote_slot) == ("remote", "inst-1", "rs-1")
    refusal = relay_archive_refusal(slot)
    assert refusal is not None and refusal.status == 409
    assert json.loads(refusal.text)["code"] == "relay_archive_read_only"
    line = codec.encode(slot, folds=_slot_folds(slot))
    assert (line["executor"], line["instance_id"], line["remote_slot"]) == (
        "remote",
        "inst-1",
        "rs-1",
    )


def test_a_resume_drops_a_folder_only_on_a_verdict_about_that_folder(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    for checked, kept in (("folder-1", ""), ("other", "folder-1")):
        chosen = codec.Resume(
            folder_unhidden=False, folder_checked_id=checked, history_key="dashboard:h"
        )
        slot = state.get_or_create_slot(f"h-{checked}", **codec.slot_args({}, chosen))
        codec.apply(state, slot, {"folder_id": "folder-1"}, chosen)
        assert slot.folder_id == kept


def test_only_a_resume_reads_the_theme_consent_beside_the_theme(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    meta = {"color_theme": "dark", "theme_consent": True, "theme_consent_sha": "a" * 64}
    resume, _, _ = _read_back(state, dict(meta), codec.RESUME)
    assert (resume.theme_consent, resume.theme_consent_sha) == (True, "a" * 64)
    recent, _, _ = _read_back(state, dict(meta), codec.RECENT)
    assert (recent.color_theme, recent.theme_consent, recent.theme_consent_sha) == (
        "dark",
        False,
        None,
    )
    tampered, _, _ = _read_back(
        state, {**meta, "theme_consent": "yes", "theme_consent_sha": "zz"}, codec.RESUME, name="t"
    )
    assert (tampered.theme_consent, tampered.theme_consent_sha) == (False, None)


def test_a_line_with_no_tab_id_gets_one_minted_for_the_reader_to_persist(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, applied, _ = _read_back(state, {}, codec.RECENT)
    assert applied.minted_tab_id and slot._tab_id == applied.minted_tab_id
    kept, applied, _ = _read_back(state, {"tab_id": "tab-x"}, codec.RESTORE, name="k")
    assert kept._tab_id == "tab-x" and applied.minted_tab_id is None


def test_settle_drops_the_held_notes_the_window_already_delivered(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    meta = {"deferred_notes": copy.deepcopy(_NOTES)}
    persisted = [{"role": "note", "content": "delivered", "ts": "t", "meta": {"noteId": "n-2"}}]
    for purpose in (codec.RESTORE, codec.RECENT, codec.RESUME):
        slot, applied, _ = _read_back(state, copy.deepcopy(meta), purpose, name=purpose)
        applied.settle(persisted)
        assert [n["id"] for n in slot._deferred_notes] == ["n-1"]
        assert slot._dropped_note_ids == {"n-2"}


def _saved_merge_card(index: int) -> dict:
    note_id = f"card-{index:06d}"
    return {
        "id": note_id,
        "content": f"summary-{index}",
        "cls": "reconcile-note",
        "context": {
            "content": f"context-{index}",
            "source": "merge-back",
            "ephemeral": True,
            "injectedAt": 0,
        },
        "session": "dashboard:parent",
        "merged_from": {
            "session": f"dashboard:fork-{index}",
            "slot": f"fork-{index}",
            "title": f"Fork {index}",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "after": "",
            "through": f"message-{index}",
            "digest": f"{index:064x}",
            "messages": 1,
        },
    }


def _committed_note_rows(notes: list[dict]) -> list[dict]:
    return [
        {"role": "inject", "content": note["content"], "meta": {"noteId": note["id"]}}
        for note in notes
    ]


def test_settle_restores_every_committed_merge_card_at_the_durable_hold_ceiling(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch)
    notes = [_saved_merge_card(index) for index in range(30)]
    meta = {"deferred_notes": serialize_deferred_notes(notes)}
    slot, applied, _ = _read_back(state, meta, codec.RESTORE)

    applied.settle(_committed_note_rows(notes))

    assert slot._deferred_notes == []
    assert [context["noteId"] for context in slot._pending_context] == [
        note["id"] for note in notes
    ]
    assert slot._dropped_note_ids == set()


def test_settle_keeps_uncommitted_notes_while_restoring_a_few_committed_cards(
    tmp_path, monkeypatch
):
    state = _state(tmp_path, monkeypatch)
    cards = [_saved_merge_card(index) for index in range(3)]
    held_plain = {
        "id": "plain-held",
        "content": "not delivered",
        "cls": "reconcile-note",
        "context": None,
        "session": "dashboard:parent",
    }
    delivered_plain = {
        "id": "plain-done",
        "content": "already delivered",
        "cls": "reconcile-note",
        "context": None,
        "session": "dashboard:parent",
    }
    notes = [*cards, held_plain, delivered_plain]
    meta = {"deferred_notes": serialize_deferred_notes(notes)}
    slot, applied, _ = _read_back(state, meta, codec.RESTORE, name="ordinary")

    applied.settle(_committed_note_rows([*cards, delivered_plain]))

    assert [note["id"] for note in slot._deferred_notes] == ["plain-held"]
    assert [context["noteId"] for context in slot._pending_context] == [
        note["id"] for note in cards
    ]
    assert slot._dropped_note_ids == {"plain-done"}


def test_settle_turns_a_turn_marker_into_the_interruption_row(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    bare, applied, _ = _read_back(state, {"turn_in_flight_generation": 3}, codec.RESUME)
    assert applied.settle([]) is True
    assert applied.turn_generation == 3 and bare._turn_in_flight_generation == 0
    assert [m["role"] for m in bare.messages] == ["error"]
    assert bare.messages[-1]["meta"]["kind"] == "gateway_restart_interruption"
    # A lost opener comes back first, and the unanswered opener is itself the
    # interruption, so no second row lands.
    meta = {"turn_in_flight_generation": 3, "turn_in_flight_prompt": copy.deepcopy(PROMPT)}
    opened, applied, _ = _read_back(state, meta, codec.RESTORE, name="o")
    assert applied.settle([]) is True
    assert [(m["role"], m["content"]) for m in opened.messages] == [("user", "the opener")]
    quiet, applied, _ = _read_back(state, {}, codec.RECENT, name="q")
    assert applied.settle([]) is False and quiet.messages == []


def test_each_reader_parses_the_turn_marker_where_it_always_did(tmp_path, monkeypatch):
    """A marker that fails to parse raises in ``apply`` for RESTORE, which reads it
    before the executor, and only in ``settle`` for RECENT and RESUME, which read it
    after the window -- so every field after it is already on the slot."""
    state = _state(tmp_path, monkeypatch)
    meta = {
        "turn_in_flight_generation": 2,
        "turn_in_flight_prompt": {"role": ["user"], "content": "x"},
        "folder_id": "f",
    }
    with pytest.raises(TypeError):
        _read_back(state, copy.deepcopy(meta), codec.RESTORE)
    for purpose in (codec.RECENT, codec.RESUME):
        slot, applied, _ = _read_back(state, copy.deepcopy(meta), purpose)
        assert slot.folder_id == "f", purpose
        with pytest.raises(TypeError):
            applied.settle([])


def test_a_full_save_names_the_queue_shortfall_before_a_bad_rotation_fails_it(
    tmp_path, monkeypatch, caplog
):
    """The over-cap queue warning is logged before the rewrite reads the stored
    rotation generation, so a save that then fails still reports the shortfall."""
    slot = _state(tmp_path, monkeypatch).get_or_create_slot("s")
    with caplog.at_level("WARNING", logger=metadata_line.logger.name):
        with pytest.raises(ValueError):
            metadata_line.build_full_line(
                slot,
                {"rotation_generation": "abc"},
                live_session="",
                closed=False,
                closed_at=None,
                window=[],
                queue_snapshot=[],
                queue_candidates=2,
                rewrite=True,
                rows_only=False,
            )
    assert any("2 queued prompt(s) exceed" in r.getMessage() for r in caplog.records)


def test_a_resume_mints_a_tag_revision_even_for_a_slot_without_the_bump(tmp_path, monkeypatch):
    """Each read rotates the tag revision when it replaces the tags; a resume goes
    through the tag module's helper, which mints one for a slot lacking the method,
    while the startup reads call the method only."""
    state = _state(tmp_path, monkeypatch)
    meta = {"tags": ["t1"]}
    for purpose in sorted(codec.PURPOSES):
        slot, _, _ = _read_back(state, copy.deepcopy(meta), purpose, name=f"bump-{purpose}")
        assert slot.tags == ["t1"] and slot.tags_revision, purpose
    monkeypatch.setattr(type(slot), "bump_tags_revision", None)
    revisions = {}
    for purpose in sorted(codec.PURPOSES):
        slot = state.get_or_create_slot(f"bare-{purpose}")
        before = slot.tags_revision
        codec.apply(state, slot, copy.deepcopy(meta), _purpose(purpose, name=f"bare-{purpose}"))
        revisions[purpose] = slot.tags_revision != before
    assert revisions == {codec.RECENT: False, codec.RESTORE: False, codec.RESUME: True}


def test_the_facade_still_binds_every_name_that_moved_into_the_codec():
    for name in (
        "COLOR_HEX_RE",
        "_RETIRED_MODES",
        "_rebase_rehydrated_refresh_mark",
        "_rehydrate_slot_title",
        "_rehydrate_title_low_signal",
        "_rehydrate_title_origin",
        "_rehydrate_title_refresh_mark",
        "_restore_dismissed_source_links",
        "_restore_model_fields",
        "_restored_mode",
        "_validate_autocompact_pct",
    ):
        assert getattr(cp, name) is getattr(codec, name), name
