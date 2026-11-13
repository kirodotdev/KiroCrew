"""Tests for the open-slots persistence helper used by gateway restart.

When the user has multiple chat tabs open and the gateway restarts, the
``restore_recent_sessions`` mtime cutoff (default 30 minutes) silently drops
long-running tabs that haven't seen a new message in 30 min. To preserve
the user's active tab set across restarts, ``DashboardState._persist_open_slots``
snapshots the live ``_slots`` keys to ``<config_dir>/open_slots.json`` on
every flush + shutdown, and ``restore_open_slots`` reads it back on startup
before the legacy mtime restore runs.

Path resolution goes through ``kiro_crew.config.loader.config_dir`` (the
canonical helper used by every other dashboard persistence path -- session
metadata, vector memory, agent metadata, secretary, etc.) so the snapshot
honors ``KIROCREW_HOME``. These tests set ``KIROCREW_HOME`` to ``tmp_path``
directly to exercise that resolution end-to-end (rather than monkeypatching
``Path.home`` and bypassing the env-var branch).

These tests cover:

* The snapshot file is written with the expected shape (``keys`` list).
* ``restore_open_slots`` rehydrates each key as a chat slot.
* Closed sessions are NOT restored (the rehydrate guard wins).
* Missing / malformed file is a no-op.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from open_slots_persistence_helpers import _seed_session
from windows_sim import builtin_open_sharing_violation, read_text_sharing_violation

from kiro_crew.dashboard.chat_persistence import (
    ENV_AUTHORITY_RESTORED,
    restore_open_slots,
    restore_open_slots_async,
)
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import DashboardState


def test_persist_writes_open_slots_json(tmp_path, monkeypatch):
    """_persist_open_slots writes the live slot keys to <config_dir>/open_slots.json."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True  # steady state: the boot restore has run
    state.get_or_create_slot("chat-1-foo")
    state.get_or_create_slot("chat-2-bar")

    state._persist_open_slots()

    snapshot_path = tmp_path / "open_slots.json"
    assert snapshot_path.exists()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-1-foo", "chat-2-bar"}
    assert isinstance(payload["ts"], (int, float))


def test_persist_overwrites_atomically(tmp_path, monkeypatch):
    """Repeated snapshot writes leave no ``.tmp`` file behind.

    atomic_write uses a unique mkstemp name per writer, because the flush thread and the
    shutdown handler can persist at the same time and would race on a fixed temp name.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True  # steady state: the boot restore has run
    state.get_or_create_slot("chat-1-foo")
    state._persist_open_slots()
    state.get_or_create_slot("chat-2-bar")
    state._persist_open_slots()

    files = sorted(p.name for p in tmp_path.iterdir() if p.is_file())
    assert "open_slots.json" in files
    # No .tmp artifacts of any name (deterministic OR mkstemp) should linger
    leftover_tmps = [f for f in files if f.endswith(".tmp")]
    assert leftover_tmps == [], f"unexpected leftover temp files: {leftover_tmps}"
    payload = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-1-foo", "chat-2-bar"}


def test_persist_honors_kirocrew_home_env(tmp_path, monkeypatch):
    """Snapshot lands in KIROCREW_HOME, not ~/.kirocrew -- proves the env-var path."""
    custom_home = tmp_path / "custom-kirocrew-home"
    monkeypatch.setenv("KIROCREW_HOME", str(custom_home))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True  # steady state: the boot restore has run
    state.get_or_create_slot("chat-1-foo")
    state._persist_open_slots()
    assert (custom_home / "open_slots.json").exists()


def test_restore_open_slots_rehydrates_listed_keys(tmp_path, monkeypatch):
    """restore_open_slots rehydrates each listed key from history."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    # Seed two sessions on disk
    _seed_session(state, "chat-1-alpha")
    _seed_session(state, "chat-2-beta")
    # Write the snapshot (config_dir auto-creates tmp_path; it already exists here)
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-alpha", "chat-2-beta"], "ts": 0.0}))

    # Fresh state (no slots) -- simulate gateway restart
    state2 = _make_state(tmp_path / "sessions")
    assert "chat-1-alpha" not in state2._slots
    assert "chat-2-beta" not in state2._slots

    restored = restore_open_slots(state2)
    assert restored == 2
    assert "chat-1-alpha" in state2._slots
    assert "chat-2-beta" in state2._slots


def test_restore_open_slots_skips_closed_sessions(tmp_path, monkeypatch):
    """A session marked closed=True in metadata must not be restored."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-open")
    _seed_session(state, "chat-2-closed", closed=True)
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-open", "chat-2-closed"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)
    assert restored == 1
    assert "chat-1-open" in state2._slots
    assert "chat-2-closed" not in state2._slots


def test_restore_open_slots_missing_file_is_noop(tmp_path, monkeypatch):
    """No snapshot file -> 0 restored, no exception."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state) == 0


def test_restore_open_slots_malformed_file_is_noop(tmp_path, monkeypatch):
    """Garbage in the snapshot file -> 0 restored, gateway still boots."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text("{not valid json")
    assert restore_open_slots(state) == 0


def test_restore_open_slots_skips_already_loaded(tmp_path, monkeypatch):
    """If a key is already in _slots (e.g. created via another path) skip it."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-foo")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-foo"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    state2.get_or_create_slot("chat-1-foo")  # already loaded
    restored = restore_open_slots(state2)
    assert restored == 0  # already present, skipped
    assert "chat-1-foo" in state2._slots


def test_persist_open_slots_handles_write_failure_gracefully(tmp_path, monkeypatch):
    """A permission failure while writing the snapshot is logged, not raised.

    atomic_write calls os.fchmod on the open descriptor, not os.chmod, so that is the
    seam patched here.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-1-foo")
    with patch("kiro_crew.atomic_write.os.fchmod", side_effect=OSError("read-only filesystem")):
        # Should not raise
        state._persist_open_slots()
    # fchmod_safe logs a permission failure and the write goes on, so the
    # snapshot still lands.
    snapshot = json.loads((tmp_path / "open_slots.json").read_text())
    assert snapshot["keys"] == ["chat-1-foo"]


def test_restore_open_slots_rejects_path_separator_keys(tmp_path, monkeypatch):
    """Keys with ``/`` or ``\\`` are rejected; legitimate keys in the same file still restore.

    Slot keys become filesystem paths through ``_history_key_for()``, so a crafted key in
    open_slots.json could otherwise escape the sessions directory.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-legit")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "keys": [
                    "../../etc/passwd",
                    "x:../../foo",
                    "windows\\..\\..\\evil",
                    "chat-1-legit",  # legitimate, must still be restored
                ],
                "ts": 0.0,
            }
        )
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)
    # Only the legit key is restored; the three traversal attempts are skipped.
    assert restored == 1
    assert "chat-1-legit" in state2._slots
    assert "../../etc/passwd" not in state2._slots
    assert "x:../../foo" not in state2._slots
    assert "windows\\..\\..\\evil" not in state2._slots


def test_restore_open_slots_rolls_back_partial_slot_on_rehydrate_failure(tmp_path, monkeypatch):
    """A failed rehydrate removes the partial slot it already registered.

    ``_rehydrate_slot_from_history`` creates the slot before its fallible reads; a leftover
    empty slot would make ``restore_recent_sessions`` dedupe on the key and skip the real
    restore, leaving an empty tab.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-good")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-good"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")

    # Patch _rehydrate_slot_from_history so that it leaks a partial slot
    # (mirroring the real partial-state path) and then raises. Without the
    # rollback in restore_open_slots, the partial slot would persist.
    from kiro_crew.dashboard import chat_persistence as cp_mod

    def _failing_rehydrate(state_arg, slot_name):
        # Mimic the real failure mode: register an empty slot via
        # get_or_create_slot, then bomb on the fallible work.
        state_arg.get_or_create_slot(slot_name, app="")
        raise RuntimeError("simulated read_messages failure (e.g. disk EIO)")

    monkeypatch.setattr(cp_mod, "_rehydrate_slot_from_history", _failing_rehydrate)
    restored = restore_open_slots(state2)

    # Rehydrate raised, so nothing was successfully restored.
    assert restored == 0
    # CRITICAL: the partial slot must be rolled back so a subsequent
    # restore_recent_sessions (or other restore path) can populate it.
    assert "chat-1-good" not in state2._slots, (
        "partial slot leaked into state._slots after rehydrate failure -- "
        "restore_recent_sessions would dedup on key and skip the proper restore, "
        "leaving the user with an empty/partial tab"
    )


def test_rehydrate_slot_restores_persisted_tab_id_for_fork_chaining(tmp_path, monkeypatch):
    """Rehydrate keeps the persisted tab_id; a session without one gets one written back.

    ``get_or_create_slot`` assigns a random tab_id, and the next flush would persist it,
    cutting the fork chain ``read_messages_chained`` walks.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-with-tab-id")
    # Inject a known tab_id into the persisted metadata to simulate an
    # already-forked session whose chain we must preserve.
    history_key = _history_key_for("chat-1-with-tab-id")
    state.conversation_log.update_metadata(history_key, {"tab_id": "knownTabId123"})

    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-with-tab-id"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)
    assert restored == 1
    slot = state2._slots["chat-1-with-tab-id"]
    assert slot._tab_id == "knownTabId123", (
        f"tab_id was overwritten with random uuid {slot._tab_id!r} "
        "instead of being restored from meta. The next flush would persist this "
        "random value and sever the fork chain."
    )

    # Legacy-session path: no tab_id in meta -> one is generated and written back.
    _seed_session(state, "chat-2-legacy-no-tab-id")
    snapshot_path.write_text(json.dumps({"keys": ["chat-2-legacy-no-tab-id"], "ts": 0.0}))
    state3 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state3)
    assert restored == 1
    slot2 = state3._slots["chat-2-legacy-no-tab-id"]
    # A fresh tab_id was generated...
    assert slot2._tab_id and len(slot2._tab_id) == 12
    # ...AND it was written back to meta (so a subsequent restart finds it).
    history_key2 = _history_key_for("chat-2-legacy-no-tab-id")
    persisted_meta = state3.conversation_log.get_metadata(history_key2)
    assert persisted_meta.get("tab_id") == slot2._tab_id


def test_restore_carries_the_agent_selection_namespace(tmp_path, monkeypatch):
    """A template-picked slot comes back as a template pick after a restart.

    ``agent_kind`` is what tells the picker which of two same-name rows the
    slot runs; restored as ``""`` it would light the MEMBER row for a slot the
    user explicitly bound to the template. Persisted with the other slot-owned
    metadata (``SLOT_OWNED_META_KEYS``), and only the two known values are
    honoured on the way back in.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-template-pick")
    history_key = _history_key_for("chat-1-template-pick")
    state.conversation_log.update_metadata(
        history_key, {"agent": "reviewer", "agent_kind": "template"}
    )
    _seed_session(state, "chat-2-junk-kind")
    state.conversation_log.update_metadata(
        _history_key_for("chat-2-junk-kind"), {"agent": "reviewer", "agent_kind": "crew"}
    )
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-template-pick", "chat-2-junk-kind"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 2
    assert state2._slots["chat-1-template-pick"].agent_kind == "template"
    # An unknown value is not a namespace the backend ever wrote; it reads as
    # "picked by name alone" rather than being trusted.
    assert state2._slots["chat-2-junk-kind"].agent_kind == ""


def test_rehydrate_slot_uses_chained_read_with_500_message_window(tmp_path, monkeypatch):
    """Rehydrate reads through ``read_messages_chained``, never the flat ``read_messages``.

    ``restore_open_slots`` runs before ``restore_recent_sessions`` and wins the key, so it
    must use the same chained, 500-message read or long sessions lose history on restart.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-long")

    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-long"], "ts": 0.0}))

    # Spy on which read method gets called: read_messages vs
    # read_messages_chained. The chained one MUST be used.
    state2 = _make_state(tmp_path / "sessions")
    chained_calls: list[str] = []
    flat_calls: list[str] = []
    real_chained = state2.conversation_log.read_messages_chained
    real_flat = state2.conversation_log.read_messages

    def _spy_chained(key, *args, **kwargs):
        chained_calls.append(key)
        return real_chained(key, *args, **kwargs)

    def _spy_flat(key, *args, **kwargs):
        flat_calls.append(key)
        return real_flat(key, *args, **kwargs)

    with (
        patch.object(state2.conversation_log, "read_messages_chained", _spy_chained),
        patch.object(state2.conversation_log, "read_messages", _spy_flat),
    ):
        restored = restore_open_slots(state2)

    assert restored == 1
    history_key = _history_key_for("chat-1-long")
    assert history_key in chained_calls, (
        f"rehydrate did NOT call read_messages_chained "
        f"(called: chained={chained_calls!r}, flat={flat_calls!r}). "
        "Forked-session ancestry would be invisible to the in-memory window."
    )
    assert history_key not in flat_calls, (
        "rehydrate still called the non-chained read_messages, "
        "which caps at 200 and does not walk fork ancestry."
    )


def test_rehydrate_slot_loads_full_500_message_window(tmp_path, monkeypatch):
    """Rehydrate loads all 250 seeded messages, so the window is above 200.

    250 sits between the flat read's 200 cap and the 500 window while keeping seeding fast.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    history_key = _history_key_for("chat-1-bigwindow")
    log = state.conversation_log
    assert log is not None
    # Seed 250 user messages — distinguishable so we can verify ordering too.
    for i in range(250):
        log.append(history_key, "user", f"msg-{i:03d}")

    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-bigwindow"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)
    assert restored == 1
    slot = state2._slots["chat-1-bigwindow"]
    assert len(slot.messages) == 250, (
        f"rehydrate loaded {len(slot.messages)} of 250 seeded "
        "messages — likely still using the old 200-message cap. "
        "The window must be >= 250 (current target: 500)."
    )
    # Oldest first, newest last.
    assert slot.messages[0]["content"] == "msg-000"
    assert slot.messages[-1]["content"] == "msg-249"


def test_persist_open_slots_excludes_incognito_and_temporary(tmp_path, monkeypatch):
    """Only ``memory_mode == "persistent"`` slots are written to ``open_slots.json``.

    Incognito and temporary tabs promise to close when the user is done, so they must not
    survive a restart.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True  # steady state: the boot restore has run
    state.get_or_create_slot("chat-persistent-1")
    state.get_or_create_slot("chat-incognito-1", memory_mode="incognito")
    state.get_or_create_slot("chat-temporary-1", memory_mode="temporary")
    state.get_or_create_slot("chat-persistent-2")

    state._persist_open_slots()

    snapshot_path = tmp_path / "open_slots.json"
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload["keys"]) == {"chat-persistent-1", "chat-persistent-2"}, (
        f"incognito/temporary keys leaked into open_slots.json "
        f"(keys={payload['keys']!r}). Incognito tabs would now survive "
        "restarts indefinitely, violating the user contract."
    )


def test_restore_open_slots_rollback_also_discards_restricted_keys(tmp_path, monkeypatch):
    """A failed rehydrate removes both the slot and its ``_restricted_keys`` entry.

    A leftover entry would make a later persistent slot of the same name inherit
    restricted status and lose consolidation and lessons.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-incognito")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(json.dumps({"keys": ["chat-1-incognito"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence as cp_mod

    def _failing_rehydrate(state_arg, slot_name):
        # Mimic the real failure mode for an INCOGNITO session: register the
        # slot, mark it restricted (matching what _rehydrate_slot_from_history
        # does for non-persistent memory_mode), THEN bomb on the fallible
        # downstream work.
        state_arg.get_or_create_slot(slot_name, app="")
        state_arg._restricted_keys.add(f"dashboard:{slot_name}")
        raise RuntimeError("simulated read_messages_chained failure (e.g. disk EIO)")

    monkeypatch.setattr(cp_mod, "_rehydrate_slot_from_history", _failing_rehydrate)
    restored = restore_open_slots(state2)

    assert restored == 0
    assert "chat-1-incognito" not in state2._slots
    # CRITICAL: the _restricted_keys entry must also be rolled back so a
    # subsequent get_or_create_slot('chat-1-incognito') with default
    # memory_mode='persistent' is not silently treated as restricted.
    assert "dashboard:chat-1-incognito" not in state2._restricted_keys, (
        "_restricted_keys entry leaked after rehydrate failure -- "
        "a later persistent get_or_create_slot would silently inherit "
        "restricted status, blocking consolidation/lessons."
    )


# ── Slot-key filename round-trip (duplicate sidebar sessions) ────────────────
#
# A raw display-style key (e.g. "Artifact: My Doc") and its _safe_key() filename
# stem would restore as two sidebar sessions over one transcript, so
# get_or_create_slot and both restore paths fold keys to the filename charset.

RAW_KEY = "Artifact: 2026 Example Benchmark Report - alice vs Bob Smith Org"
FOLDED_KEY = "Artifact__2026_Example_Benchmark_Report_-_alice_vs_Bob_Smith_Org"


def test_restore_open_slots_folds_legacy_raw_keys(tmp_path, monkeypatch):
    """A raw snapshot key restores under the canonical folded key."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, FOLDED_KEY)  # on-disk file is always the folded form
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": [RAW_KEY], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 1
    assert FOLDED_KEY in state2._slots
    assert RAW_KEY not in state2._slots


def test_restore_open_slots_dedupes_raw_and_folded_snapshot_twins(tmp_path, monkeypatch):
    """A polluted snapshot carrying BOTH key forms restores exactly one slot."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, FOLDED_KEY)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [RAW_KEY, FOLDED_KEY], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 1
    assert list(state2._slots) == [FOLDED_KEY]


def test_restart_restore_paths_converge_on_one_slot(tmp_path, monkeypatch):
    """End-to-end: open_slots replay + filename-stem walk = 1 slot.

    A raw display-style key in open_slots.json plus the mtime-based
    restore_recent_sessions walk is what produces two identical sidebar sessions
    after a gateway restart.
    """
    from kiro_crew.dashboard.chat_persistence import restore_recent_sessions

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, FOLDED_KEY)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": [RAW_KEY], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    # Startup order matches server.py: snapshot replay first, mtime walk second.
    restore_open_slots(state2)
    restore_recent_sessions(state2, window_minutes=0)  # 0 = no cutoff, restore all

    matching = [k for k in state2._slots if "Benchmark" in k]
    assert matching == [FOLDED_KEY], (
        f"expected exactly one slot for the session, got {matching!r} — "
        "duplicate sidebar sessions regression"
    )


# ── _slot_counter reseed after restore ──
#
# _slot_counter starts at 0 each boot while restored tabs keep their "chat-<N>-<ts>"
# keys, so reseed_slot_counter() must move past the highest restored index or a new
# chat re-mints a key that collides with a restored tab.


def test_reseed_advances_past_highest_restored_index(tmp_path, monkeypatch):
    """reseed_slot_counter seeds the counter to the max restored slot index."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    assert state._slot_counter == 0
    state.get_or_create_slot("chat-6-1783712190")
    state.get_or_create_slot("chat-7-1783712220")

    state.reseed_slot_counter()

    assert state._slot_counter == 7


def test_reseed_ignores_non_indexed_keys(tmp_path, monkeypatch):
    """Custom keys (Slack sessions, sanitized names) are skipped, not crashed on."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-3-1783712000")
    # Keys without a digit in the second-to-last segment must be ignored.
    state.get_or_create_slot("my-custom-session")
    state.get_or_create_slot("takeover-9-1783712300")

    state.reseed_slot_counter()

    # Highest indexed key wins (takeover-9), custom key ignored. The parser is
    # prefix-agnostic, so a "takeover-<N>-<ts>" key still contributes its index
    # even though this fork only auto-mints the "chat" prefix.
    assert state._slot_counter == 9


def test_reseed_is_monotonic(tmp_path, monkeypatch):
    """reseed never lowers the counter below its current value."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state._slot_counter = 12
    state.get_or_create_slot("chat-3-1783712000")

    state.reseed_slot_counter()

    assert state._slot_counter == 12


def test_reseed_noop_when_no_slots(tmp_path, monkeypatch):
    """No slots -> counter unchanged, no exception."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.reseed_slot_counter()
    assert state._slot_counter == 0


def test_new_slot_does_not_collide_with_restored_tab(tmp_path, monkeypatch):
    """Restore high-index tabs, reseed, then mint: the new key is unused and one higher."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-6-restored")
    _seed_session(state, "chat-7-restored")
    snapshot_path = tmp_path / "open_slots.json"
    snapshot_path.write_text(
        json.dumps({"keys": ["chat-6-restored", "chat-7-restored"], "ts": 0.0})
    )

    # Fresh gateway boot: restore tabs, then reseed the counter.
    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 2
    state2.reseed_slot_counter()

    existing_keys = set(state2._slots)
    # Mint a brand-new chat the way the UI's "new chat" button does.
    new_slot = state2.get_or_create_slot()

    assert new_slot.key not in existing_keys
    # get_or_create_slot increments before formatting; post-increment would mint
    # chat-7-* and collide.
    assert int(new_slot.key.rsplit("-", 2)[1]) == 8


def test_reseed_skips_unicode_digit_key_without_crashing(tmp_path, monkeypatch):
    """A unicode-digit segment is skipped rather than crashing boot-time reseeding.

    str.isdigit() accepts '²' but int() raises on it, so the isascii() guard matters.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-4-1783712000")
    # Inject a pathological key directly (get_or_create_slot would ascii-sanitize it).
    state._slots["chat-²-1783712001"] = state._slots["chat-4-1783712000"]

    state.reseed_slot_counter()  # must not raise

    assert state._slot_counter == 4


# ── A flush BEFORE restore has run must not wipe the reopen seed ──
#
# The 5s periodic flush is armed several awaits before the open-tab restore runs.
# A flush in that gap would write ``{"keys": []}`` over a good seed, so
# ``open_slots_restored`` gates the empty write until the restore has run.


def test_flush_before_restore_does_not_wipe_seed(tmp_path, monkeypatch):
    """An empty-slots flush before restore must leave open_slots.json intact."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = {"keys": ["chat-1-idle", "chat-2-idle", "chat-3-idle"], "ts": 123.0}
    (tmp_path / "open_slots.json").write_text(json.dumps(seed))

    # Fresh gateway boot: _slots is empty and the open-tab restore has NOT run.
    state = _make_state(tmp_path / "sessions")
    assert state.open_slots_restored is False
    assert state._slots == {}

    # The periodic flush fires in the pre-restore gap.
    state._persist_open_slots()

    # The good seed must survive untouched — not be overwritten with [].
    persisted = json.loads((tmp_path / "open_slots.json").read_text())
    assert set(persisted["keys"]) == {"chat-1-idle", "chat-2-idle", "chat-3-idle"}, (
        "a flush before restore wiped the reopen seed; the next restart would "
        "send every session into 'older sessions'"
    )


def test_pre_restore_flush_does_not_clobber_seed_with_a_boot_time_slot(tmp_path, monkeypatch):
    """A pre-restore flush with a boot-time tab writes the union with the seed.

    The HTTP listener binds before restore, so ``_slots`` can hold only a tab opened
    during boot; pruning the seed down to it would lose every seeded tab.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = {"keys": ["chat-1-idle", "chat-2-idle"], "ts": 123.0}
    (tmp_path / "open_slots.json").write_text(json.dumps(seed))

    state = _make_state(tmp_path / "sessions")
    assert state.open_slots_restored is False
    # A chat created during boot, before restore ran.
    state.get_or_create_slot("chat-9-newtab")

    state._persist_open_slots()

    # The seed is preserved AND the boot-time tab is added — the union, not a
    # replacement that drops the seeded tabs.
    persisted = json.loads((tmp_path / "open_slots.json").read_text())
    assert set(persisted["keys"]) == {"chat-1-idle", "chat-2-idle", "chat-9-newtab"}, (
        "a non-empty pre-restore flush did not merge the seed; the restore "
        "would then lose the seeded tabs"
    )


def test_post_restore_empty_flush_still_clears_the_file(tmp_path, monkeypatch):
    """After restore, an empty set is authoritative (user closed every tab).

    The guard must not pin a stale seed once restore has run — otherwise closing
    the last tab would never take effect across a restart.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-old"], "ts": 0.0}))
    state = _make_state(tmp_path / "sessions")
    # Restore ran this boot and found nothing to restore.
    assert restore_open_slots(state) == 0
    assert state.open_slots_restored is True
    assert state._slots == {}

    state._persist_open_slots()

    persisted = json.loads((tmp_path / "open_slots.json").read_text())
    assert persisted["keys"] == [], (
        "a genuinely-empty slot set after restore must still persist — closing "
        "the last tab should not be undone by a restart"
    )


def test_restore_sets_open_slots_restored_flag(tmp_path, monkeypatch):
    """Both drivers mark the restore as having run, even on a missing file."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))

    sync_state = _make_state(tmp_path / "sessions")
    assert sync_state.open_slots_restored is False
    restore_open_slots(sync_state)  # missing file -> no-op
    assert sync_state.open_slots_restored is True

    async_state = _make_state(tmp_path / "sessions")
    assert async_state.open_slots_restored is False
    asyncio.run(restore_open_slots_async(async_state))
    assert async_state.open_slots_restored is True


def test_two_boot_restart_keeps_idle_sessions_active(tmp_path, monkeypatch):
    """A pre-restore flush on boot #1 does not strand idle sessions in "older" on boot #2."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    base = _make_state(tmp_path / "sessions")
    keys = ["chat-1-idle", "chat-2-idle", "chat-3-idle"]
    for k in keys:
        _seed_session(base, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    # ── Boot #1 ──
    boot1 = _make_state(tmp_path / "sessions")
    # The HTTP listener is up before restore, so the user creates a NEW chat in
    # the gap — making _slots non-empty with a tab that is NOT in the seed.
    boot1.get_or_create_slot("chat-9-newtab")
    # The periodic flush fires in that gap.
    boot1._persist_open_slots()
    # Then the startup restore runs (reads the still-intact seed).
    assert restore_open_slots(boot1) == 3
    # And a normal post-restore flush snapshots the live set (seed + boot tab).
    boot1._persist_open_slots()

    # ── Boot #2 (the next restart) ──
    boot2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(boot2)
    assert restored == 3, (
        f"boot #1's pre-restore flush wiped the seed; boot #2 restored "
        f"{restored}/3 idle sessions, the rest fell into 'older sessions'"
    )
    assert set(boot2._slots) == set(keys)


def test_crash_before_restore_keeps_seed_and_boot_time_tab(tmp_path, monkeypatch):
    """A crash after a pre-restore flush keeps both the seed (A, B) and the boot-time tab C.

    The next boot restores from that flush, and the pre-restore merge wrote A ∪ B ∪ C.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    base = _make_state(tmp_path / "sessions")
    for k in ("chat-A-idle", "chat-B-idle", "chat-C-boot"):
        _seed_session(base, k)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-A-idle", "chat-B-idle"], "ts": 0.0})
    )

    # ── Boot #1: listener up, user opens chat C, flush fires, THEN crash ──
    boot1 = _make_state(tmp_path / "sessions")
    boot1.get_or_create_slot("chat-C-boot")
    assert boot1.open_slots_restored is False
    boot1._persist_open_slots()  # the only write before the simulated crash
    # (restore never runs — the process dies here)

    # ── Boot #2: restore from the crash-time file ──
    boot2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(boot2)
    assert restored == 3, (
        f"a pre-restore crash lost tabs: boot #2 restored {restored}/3 "
        f"(seed A/B plus boot-time C must all survive)"
    )
    assert set(boot2._slots) == {"chat-A-idle", "chat-B-idle", "chat-C-boot"}


def test_pre_restore_transient_seed_read_failure_preserves_seed(tmp_path, monkeypatch):
    """A transient pre-restore seed read failure skips the write instead of shrinking the seed.

    Merging an empty read (e.g. a Windows sharing violation) would drop the seeded tabs;
    the next flush retries.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = {"keys": ["chat-A-idle", "chat-B-idle"], "ts": 0.0}
    (tmp_path / "open_slots.json").write_text(json.dumps(seed))

    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-C-boot")  # a boot-time live tab
    assert state.open_slots_restored is False

    # Simulate a transient read failure (seed exists but is momentarily
    # unreadable) distinct from a genuinely-absent file.
    from kiro_crew.dashboard.state import _persistence_for

    monkeypatch.setattr(
        _persistence_for(state),
        "_read_persisted_open_slot_keys",
        lambda path: None,
    )

    state._persist_open_slots()

    # The write was skipped: the on-disk seed is byte-for-byte intact (the
    # boot-time tab did NOT replace A/B).
    assert json.loads((tmp_path / "open_slots.json").read_text()) == seed, (
        "a transient pre-restore seed read failure shrank/overwrote the seed "
        "instead of skipping the write"
    )


def test_persist_without_restore_is_not_permanently_suppressed(tmp_path, monkeypatch):
    """A surface that never runs a restore (e.g. headless) still persists its live slots."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    assert state.open_slots_restored is False  # no restore on this surface
    state.get_or_create_slot("chat-live")

    state._persist_open_slots()

    persisted = json.loads((tmp_path / "open_slots.json").read_text())
    assert "chat-live" in persisted["keys"], (
        "a surface that never restores must still seed its live slots — the "
        "pre-restore guard must not be a permanent off-switch"
    )


def test_context_snapshot_prune_skipped_before_restore(tmp_path, monkeypatch):
    """Before restore, _persist_context_snapshots writes the disk/live union instead of pruning.

    It prunes to ``set(_slots)``, which is still empty before restore.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    snap_path = tmp_path / "context_snapshots.json"
    snap_path.write_text(json.dumps({"chat-1-idle": {"pct": 42}, "chat-2-idle": {"pct": 7}}))

    state = _make_state(tmp_path / "sessions")
    assert state.open_slots_restored is False
    # A boot-time dirty reading for a tab not yet restored.
    with state._context_snapshots_lock:
        state._context_snapshots["chat-9-newtab"] = {"pct": 1}
        state._context_snapshots_dirty = True

    state._persist_context_snapshots()

    # The restored tabs' readings survive (no prune) AND the boot-time reading
    # is written — the union, not a prune that would erase the disk readings.
    persisted = json.loads(snap_path.read_text())
    assert set(persisted) == {
        "chat-1-idle",
        "chat-2-idle",
        "chat-9-newtab",
    }, "a pre-restore context-snapshot prune erased restored tabs' readings"


def test_context_snapshot_prune_runs_after_restore(tmp_path, monkeypatch):
    """After restore the prune is authoritative again (dead keys drop)."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    snap_path = tmp_path / "context_snapshots.json"

    state = _make_state(tmp_path / "sessions")
    restore_open_slots(state)  # marks open_slots_restored
    assert state.open_slots_restored is True
    state.get_or_create_slot("chat-1-live")
    with state._context_snapshots_lock:
        state._context_snapshots["chat-1-live"] = {"pct": 50}
        state._context_snapshots["chat-2-gone"] = {"pct": 9}
        state._context_snapshots_dirty = True

    state._persist_context_snapshots()

    persisted = json.loads(snap_path.read_text())
    assert set(persisted) == {"chat-1-live"}, "a dead key survived the post-restore prune"


def test_context_snapshot_write_skipped_when_pre_restore_read_fails(tmp_path, monkeypatch):
    """A failed pre-restore snapshot read (Windows sharing violation) skips the write.

    Latching the failure as an empty loaded map would overwrite every other tab's reading.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    snap_path = tmp_path / "context_snapshots.json"
    snap_path.write_text(json.dumps({"chat-1-idle": {"pct": 42}, "chat-2-idle": {"pct": 7}}))
    before = snap_path.read_bytes()

    state = _make_state(tmp_path / "sessions")
    assert state.open_slots_restored is False
    with state._context_snapshots_lock:
        state._context_snapshots["chat-9-newtab"] = {"pct": 1}
        state._context_snapshots_dirty = True

    with read_text_sharing_violation(match="context_snapshots.json", times=1) as seen:
        state._persist_context_snapshots()

    assert seen["n"] == 1, "the simulator never intercepted the snapshot read"
    assert snap_path.read_bytes() == before, (
        "a transient pre-restore snapshot read failure replaced the saved "
        "snapshots instead of skipping the write"
    )
    assert state._context_snapshots_loaded is False, "a failed read was latched as loaded"
    assert state._context_snapshots_dirty is True, "the skipped write is owed, not lost"

    # The next flush reads the intact file and writes the union.
    state._persist_context_snapshots()
    persisted = json.loads(snap_path.read_text())
    assert set(persisted) == {"chat-1-idle", "chat-2-idle", "chat-9-newtab"}


def test_push_slots_update_survives_a_partially_constructed_state():
    """A state built with __new__ (no __init__) must still be able to broadcast.

    Endpoint suites build bare states, and push_slots_update reads the suspend counter on
    every call, so the counter and its companions are class-level defaults.
    """
    bare = DashboardState.__new__(DashboardState)
    # Only what push_slots_update itself needs — deliberately NOT the new flags.
    # `_yolo` is intentionally omitted: it is a property whose setter mutates the
    # process-global safety-override singleton, and push_slots_update reads YOLO
    # state from that global rather than from the instance.
    bare._slots = {}
    bare._ws_clients = []
    bare._sse_queues = []
    bare._notify_event = MagicMock()
    bare.channel_manager = None

    # Readable without __init__, at their documented baseline.
    assert bare._slots_push_suspend == 0
    assert bare._slots_push_pending is False
    assert bare.restoring_open_slots is False

    # And the read path actually runs rather than raising AttributeError.
    bare.push_slots_update()

    # The context manager also works on a bare state, and leaves no residue.
    with bare.suspend_slots_push():
        bare.push_slots_update()
        assert bare._slots_push_suspend == 1
    assert bare._slots_push_suspend == 0
    assert bare._slots_push_pending is False


def test_a_transient_metadata_read_failure_does_not_drop_a_tab(tmp_path, monkeypatch):
    """A tab must survive a one-shot Windows sharing violation on its transcript.

    An indexer or AV scanner can briefly hold a just-written file
    (``ERROR_SHARING_VIOLATION``), and an empty metadata read means "skip this key", so
    the metadata read retries.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-transient" for i in range(6)]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    # The transcript filename for the 3rd tab — the one the scanner "holds".
    victim = state2.conversation_log._path(_history_key_for(keys[2])).name

    with builtin_open_sharing_violation(match=victim, times=1) as seen:
        restored = restore_open_slots(state2)

    assert seen["n"] >= 1, "the simulator never intercepted the transcript open"
    assert restored == 6, (
        f"a single transient sharing violation dropped {6 - restored} tab(s); "
        f"restored slots: {sorted(state2._slots)}"
    )
    assert keys[2] in state2._slots


def test_read_messages_retries_transient_sharing_violation(tmp_path, monkeypatch):
    """A transient sharing violation on the transcript BODY is retried, not lost.

    The metadata cache is primed so the only faulted open is the body read in
    ``_read_messages``, which is on the same restore path as the metadata read.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-body")
    log = state.conversation_log
    assert log is not None
    key = _history_key_for("chat-1-body")
    victim = log._path(key).name
    # Prime the metadata cache and drop any warmed message cache so the sole
    # open under the violation is the body read this test targets.
    log.get_metadata(key)
    log._msg_cache.clear()

    with builtin_open_sharing_violation(match=victim, times=1) as seen:
        msgs = log._read_messages(key)

    assert seen["n"] >= 1, "the simulator never intercepted the body open"
    assert [m.get("content") for m in msgs] == ["hello"], (
        f"a transient sharing violation lost the message body (got {msgs!r}); "
        "the tab would restore empty or be dropped on the Windows CI line"
    )


def test_read_messages_reraises_after_exhausting_retries(tmp_path, monkeypatch):
    """A PERSISTENT body-read failure must re-raise, not swallow to ``[]``.

    ``[]`` would register an empty slot that ``restore_recent_sessions`` then skips;
    re-raising lets rehydrate roll back so a later restore can retry.
    """
    from kiro_crew.history import _METADATA_READ_ATTEMPTS

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-persist")
    log = state.conversation_log
    assert log is not None
    key = _history_key_for("chat-1-persist")
    victim = log._path(key).name
    # Prime the metadata cache and drop the message cache so the ONLY opens
    # under the violation are the body reads -- and fault EVERY attempt.
    log.get_metadata(key)
    log._msg_cache.clear()

    with builtin_open_sharing_violation(match=victim, times=_METADATA_READ_ATTEMPTS):
        with pytest.raises(OSError):
            log._read_messages(key)


def test_read_messages_missing_file_mid_read_returns_empty(tmp_path, monkeypatch):
    """A transcript deleted AFTER exists() (concurrent delete race) yields ``[]``.

    ``FileNotFoundError`` is not a transient lock: re-raising it would turn a concurrent
    ``delete_session`` into an HTTP 500 in callers like ``api_session_detail``.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-race")
    log = state.conversation_log
    assert log is not None
    key = _history_key_for("chat-1-race")
    # Prime metadata cache + drop the message cache so the body read happens.
    log.get_metadata(key)
    log._msg_cache.clear()

    # Simulate the file vanishing between the (passing) exists()/stat() and the
    # body open() -- the concurrent-delete race the guard is for.
    victim = log._path(key).name
    real_open = open

    def _fnf_open(file, *args, **kwargs):
        if victim in str(file):
            raise FileNotFoundError(2, "No such file or directory", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fnf_open)

    # Must return [] (not raise) so read_messages callers don't 500 on the race.
    assert log._read_messages(key) == []


def test_persistent_body_read_failure_drops_tab_not_registers_empty(tmp_path, monkeypatch):
    """A persistent body-read failure DROPS the tab instead of registering it empty.

    An absent key leaves ``restore_recent_sessions`` and the next restart free to recover it.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-persist" for i in range(3)]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    victim_key = _history_key_for(keys[1])
    victim = state2.conversation_log._path(victim_key).name
    # Warm the metadata cache so the victim's metadata reads are served cached;
    # then fault EVERY victim open so the message-body read exhausts its retries
    # (a large count also covers any incidental victim opens -- e.g. a tab_id
    # index rebuild -- without letting the body read slip through unfaulted).
    state2.conversation_log.get_metadata(victim_key)

    with builtin_open_sharing_violation(match=victim, times=10_000):
        restored = restore_open_slots(state2)

    assert restored == 2, f"expected the two readable tabs; got {restored}"
    assert keys[1] not in state2._slots, (
        "an unreadable tab was registered EMPTY instead of dropped -- "
        "restore_recent_sessions would dedupe it and the history would be lost"
    )
    assert keys[0] in state2._slots and keys[2] in state2._slots


def test_persistent_metadata_failure_keeps_key_in_reopen_seed(tmp_path, monkeypatch):
    """A tab dropped after exhausting read retries keeps its key in open_slots.json.

    The flush snapshots live ``_slots``, and ``dashboard.restore_sessions`` defaults to
    ``False``, so the seed is the only way to restore the tab on a later boot.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-seed" for i in range(4)]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    victim = state2.conversation_log._path(_history_key_for(keys[2])).name

    # times=3 exhausts _METADATA_READ_ATTEMPTS, so the retry cannot absorb it.
    with builtin_open_sharing_violation(match=victim, times=3) as seen:
        restored = restore_open_slots(state2)

    assert seen["n"] >= 1, "the simulator never intercepted the transcript open"
    assert restored == 3, f"expected the three readable tabs; got {restored}"
    assert keys[2] not in state2._slots, "the unreadable tab should not be registered"
    assert keys[2] in state2.unrestored_slot_keys

    # The flush must not erase it. Guard is already released by now.
    assert state2.restoring_open_slots is False
    state2._persist_open_slots()
    persisted = set(json.loads((tmp_path / "open_slots.json").read_text())["keys"])
    assert persisted == set(keys), (
        "the post-restore flush erased the unreadable tab's key from the reopen "
        f"seed, so it can never be restored; seed is now {sorted(persisted)}"
    )


def test_absent_session_is_dropped_from_reopen_seed(tmp_path, monkeypatch):
    """A key with no transcript is pruned; only FAILED reads keep their seed."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-real")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-real", "chat-2-neverexisted"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 1
    assert "chat-2-neverexisted" not in state2.unrestored_slot_keys
    state2._persist_open_slots()
    persisted = set(json.loads((tmp_path / "open_slots.json").read_text())["keys"])
    assert persisted == {"chat-1-real"}


def test_remote_only_slot_is_kept_as_reopen_seed_after_authority_restore(tmp_path, monkeypatch):
    """A listed slot with no LOCAL transcript survives a remote authority restore.

    A replacement task restores ``open_slots.json`` but loads transcripts lazily, so with
    ``ENV_AUTHORITY_RESTORED`` set the key stays a reopen seed instead of being pruned.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv(ENV_AUTHORITY_RESTORED, "1")
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-local")
    # chat-2-remote is in the restored table but its transcript is not on disk,
    # exactly the cold-replacement shape.
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-local", "chat-2-remote"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 1, "only the slot with a local transcript rebuilds this boot"
    assert "chat-2-remote" in state2.unrestored_slot_keys, (
        "a remote-only slot was pruned instead of kept as a reopen seed, so the "
        "restored slot table is lost on the next flush"
    )
    state2._persist_open_slots()
    persisted = set(json.loads((tmp_path / "open_slots.json").read_text())["keys"])
    assert persisted == {
        "chat-1-local",
        "chat-2-remote",
    }, "the post-restore flush dropped the remote-only key from the slot table"


def test_closed_tab_is_dropped_even_after_authority_restore(tmp_path, monkeypatch):
    """A ✕-closed tab stays gone even when remote-only preservation is on.

    The preserve rule is scoped to slots whose transcript is merely not-yet-local,
    not to tabs the user dismissed: a closed tab carries ``meta.closed`` and must
    not be resurrected by a replacement.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv(ENV_AUTHORITY_RESTORED, "1")
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-closed", closed=True)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-closed"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 0
    assert "chat-1-closed" not in state2.unrestored_slot_keys


def test_metadata_failure_is_reported_above_debug(tmp_path, monkeypatch, caplog):
    """The restore layer logs a WARNING naming the dropped tab, not just the unread file."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-logged")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-logged"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    victim = state2.conversation_log._path(_history_key_for("chat-1-logged")).name
    with caplog.at_level("WARNING", logger="kiro_crew.dashboard.chat_persistence"):
        with builtin_open_sharing_violation(match=victim, times=3):
            restore_open_slots(state2)

    assert any(
        "chat-1-logged" in r.message and "reopen seed" in r.message for r in caplog.records
    ), f"no WARNING named the affected tab; got {[r.message for r in caplog.records]}"


def test_get_metadata_status_separates_unreadable_from_absent(tmp_path, monkeypatch):
    """The new signal must not report absence as a read failure, or vice versa."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    log = state.conversation_log
    assert log is not None
    _seed_session(state, "chat-1-status")
    history_key = _history_key_for("chat-1-status")

    meta, readable = log.get_metadata_status(history_key)
    assert readable is True and meta, "a healthy transcript must read back"

    # No transcript at all -> empty, but a genuine answer.
    meta, readable = log.get_metadata_status(_history_key_for("chat-2-missing"))
    assert meta == {} and readable is True

    # Exists but unopenable after every retry -> empty AND flagged unreadable.
    log._meta_cache.clear()
    victim = log._path(history_key).name
    with builtin_open_sharing_violation(match=victim, times=3):
        meta, readable = log.get_metadata_status(history_key)
    assert meta == {} and readable is False

    # get_metadata keeps its plain-dict contract for the same input.
    log._meta_cache.clear()
    with builtin_open_sharing_violation(match=victim, times=3):
        assert log.get_metadata(history_key) == {}


def test_non_object_metadata_line_does_not_abort_the_whole_restore(tmp_path, monkeypatch):
    """A transcript whose first line is valid JSON but not an OBJECT is skipped and pruned.

    ``data.get`` on a non-dict raises ``AttributeError``, which would escape to
    ``server.py`` and abort startup; like an undecodable line, it is a corrupt answer.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-0-ok", "chat-1-corrupt", "chat-2-ok"]
    for k in keys:
        _seed_session(state, k)
    # Overwrite the corrupt tab's transcript so line 1 is valid JSON, not an object.
    victim_path = state.conversation_log._path(_history_key_for("chat-1-corrupt"))
    victim_path.write_text('null\n{"role": "user", "content": "hi"}\n')
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    restored = restore_open_slots(state2)

    assert restored == 2, (
        f"a corrupt metadata line cost more than its own tab; restored={restored}, "
        f"slots={sorted(state2._slots)}"
    )
    assert "chat-2-ok" in state2._slots, (
        "the tab AFTER the corrupt one did not restore -- the exception escaped "
        "the per-tab guard and aborted the whole restore"
    )
    # Corrupt, not unreadable: a genuine answer, so do not carry it forever.
    assert "chat-1-corrupt" not in state2.unrestored_slot_keys
    state2._persist_open_slots()
    persisted = set(json.loads((tmp_path / "open_slots.json").read_text())["keys"])
    assert persisted == {"chat-0-ok", "chat-2-ok"}
