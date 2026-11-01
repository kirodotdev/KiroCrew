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
    """The snapshot is written via the canonical atomic_write helper -- no
    stale temp file is left behind even after multiple writes.

    atomic_write uses tempfile.mkstemp() so each writer gets a unique
    "tmpXXXXXX.tmp" name (preventing the ENOENT race that a deterministic
    "open_slots.json.tmp" would re-introduce when _persist_open_slots fires
    concurrently from the periodic flush thread and the shutdown handler). After a successful replace() the temp file
    is gone; on failure the except branch unlinks it. Either way no .tmp
    artifacts should accumulate.
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
    """Failure to write the snapshot is logged at debug, not raised.

    The canonical atomic_write helper uses os.fchmod (against the open file
    descriptor) rather than os.chmod (against a path), so we patch fchmod
    here. A read-only filesystem or restricted container is the realistic
    failure mode -- snapshot must still no-op cleanly without raising.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-1-foo")
    with patch("kiro_crew.atomic_write.os.fchmod", side_effect=OSError("read-only filesystem")):
        # Should not raise
        state._persist_open_slots()


def test_restore_open_slots_rejects_path_separator_keys(tmp_path, monkeypatch):
    """Path-traversal guard: keys with / or \\ are rejected with a warning.

    Defence-in-depth: slot keys flow into ``_history_key_for()`` -> filesystem
    path construction. A crafted key
    smuggled into open_slots.json (via symlink attack at write time or a
    separate vuln) could escape the sessions directory. The 0o600 permissions
    set by atomic_write make this a small real-world risk, but the guard is
    cheap and matches the validation pattern used for reasoning_effort against
    the same on-disk-trust threat model.

    This test pins:
      1. Forward-slash keys are rejected.
      2. Backslash keys are rejected (Windows-style attempts).
      3. Legitimate keys in the same file ARE restored (one bad apple does not
         poison the whole snapshot).
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
    """Partial-state cleanup when rehydrate fails.

    ``_rehydrate_slot_from_history`` calls ``state.get_or_create_slot(slot_name, ...)``
    BEFORE its fallible work (read_messages, redact_exfiltration_urls /
    redact_credentials on assistant content, slot.append). If any of that raises
    (disk corruption, partial writes, EIO, manually edited session file, schema
    drift) the empty slot is already registered in ``state._slots``. Without an
    explicit rollback in ``restore_open_slots``, the next caller in start_dashboard
    -- ``restore_recent_sessions`` -- would dedupe on slot key (`if slot_name in
    state._slots: continue`) and SKIP the proper restore. User would see a tab
    with the right title/agent but wrong-or-empty message history.

    This test pins the rollback: when ``_rehydrate_slot_from_history`` raises,
    ``restore_open_slots`` must remove the partial slot from ``state._slots`` so
    a downstream restore path can fill it in cleanly.
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
    """tab_id persistence across rehydrate (fork chaining).

    ``_rehydrate_slot_from_history`` calls ``state.get_or_create_slot`` (in its
    caller path) which assigns a fresh random uuid to ``slot._tab_id``. If the
    helper does NOT then read ``meta['tab_id']`` and overwrite that random uuid,
    the next ``_flush_dirty_slots`` will persist the random uuid back into the
    session metadata, severing the tab_id ancestry that
    ``read_messages_chained`` walks across forks. One restart + one flush =
    permanent loss of forked-session history.

    This test pins:
      1. Pre-existing tab_id in meta is restored onto slot._tab_id (not
         overwritten with a fresh random uuid).
      2. If meta has no tab_id (legacy session), one is generated AND written
         back to meta so subsequent reads find it.
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
    """Chained read + 500-message window on rehydrate.

    ``_rehydrate_slot_from_history`` must not call
    ``conversation_log.read_messages(history_key)`` (no chain, capped at 200
    in-memory). ``restore_recent_sessions`` uses
    ``read_messages_chained(key)`` (capped at 500). Because
    ``restore_open_slots`` runs FIRST in start_dashboard and dedupes by key,
    every long-running session lost 200+ messages of visible window on every
    gateway restart.

    This test pins:
      1. ``read_messages_chained`` is the call used (not ``read_messages``).
      2. The in-memory window cap is 500, not 200 (matches
         ``restore_recent_sessions``).
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
    """Functional window-cap pin: rehydrate loads the full window.

    Seeds 250 messages — strictly more than the old 200 cap and well below
    the new 500 cap — then rehydrates and asserts ALL 250 were loaded into
    the slot. This pin is durable against refactors that the previous
    inspect-the-source approach was brittle to (extracting 500 to a named
    constant, reformatting, etc. would silently break a string-match
    assertion). 250 keeps the test fast (sub-second seeding) while still
    proving the cap is materially > 200.

    Pre-fix (200 cap), this test would see only the last 200 of 250
    messages restored. With the 500 cap, all 250 land.
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
    # Verify ordering (oldest first, newest last) — defensive, in case a
    # future refactor accidentally reverses the slice.
    assert slot.messages[0]["content"] == "msg-000"
    assert slot.messages[-1]["content"] == "msg-249"


def test_persist_open_slots_excludes_incognito_and_temporary(tmp_path, monkeypatch):
    """Incognito/temporary tabs must not survive restarts.

    Pre-this-CR, incognito ("incognito" / "temporary" memory_mode) tabs fell
    off naturally because nothing referenced them across restarts and
    ``restore_recent_sessions`` enforces a 30-min mtime window. Persisting all
    keys in ``_persist_open_slots`` without filtering would make incognito
    tabs survive restarts indefinitely -- a contract regression. The user
    promise of incognito is "no consolidation / no lessons / closes when I'm
    done"; persistence across restarts violates the practical effect users
    rely on.

    This test pins: only ``memory_mode == "persistent"`` slots are written
    to ``open_slots.json``.
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
    """Rollback must also discard _restricted_keys on rehydrate failure.

    ``_rehydrate_slot_from_history`` adds ``f"dashboard:{slot_name}"`` to
    ``state._restricted_keys`` BEFORE the subsequent fallible
    ``read_messages_chained`` / redact / ``slot.append`` work, for any
    non-persistent ``memory_mode``. If that fallible work raises, the existing
    rollback in ``restore_open_slots`` only does ``state._slots.pop`` -- the
    ``_restricted_keys`` entry persists. A later
    ``state.get_or_create_slot(slot_name)`` (default ``memory_mode='persistent'``)
    would silently inherit restricted status, blocking consolidation/lessons
    for what should be a normal persistent session.

    This test pins: rollback removes the slot AND the _restricted_keys entry.
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
# A display-style slot name (e.g. "Artifact: My Doc" from the artifact iterate
# flow) must not survive as a raw slot key while its JSONL filename takes the
# lossy _safe_key() fold. After a restart, restore_open_slots rehydrates the
# raw key from open_slots.json while restore_recent_sessions derives a SECOND
# slot from the filename stem — two identical sidebar sessions backed by one
# transcript. get_or_create_slot folds keys to the filename charset, and
# the restore paths apply the same fold so pre-fix snapshots self-heal.

RAW_KEY = "Artifact: 2026 Example Benchmark Report - alice vs Bob Smith Org"
FOLDED_KEY = "Artifact__2026_Example_Benchmark_Report_-_alice_vs_Bob_Smith_Org"


def test_restore_open_slots_folds_legacy_raw_keys(tmp_path, monkeypatch):
    """A pre-fix snapshot key restores under the canonical folded key."""
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


# ---------------------------------------------------------------------------
# _slot_counter reseed after restore (tab-key collision fix)
# ---------------------------------------------------------------------------
#
# Regression: DashboardState.__init__ resets _slot_counter to 0 on every boot.
# The restore paths rehydrate tabs under their original "chat-<N>-<ts>" keys
# without advancing the counter, so the first new chat after a restart re-mints
# a low index that collides with an already-restored tab — clicking the tab
# then loads the wrong session. reseed_slot_counter() must advance the counter
# past the highest restored index so new slots get fresh, unique keys.


def test_reseed_advances_past_highest_restored_index(tmp_path, monkeypatch):
    """reseed_slot_counter seeds the counter to the max restored slot index."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    # Counter starts at 0 on a fresh (restarted) gateway.
    assert state._slot_counter == 0
    # Simulate restored tabs holding high indices.
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
    """End-to-end: restore high-index tabs, reseed, then mint — no key collision.

    This is the exact bug: without reseed, the freshly minted slot would take
    index 1 and there'd be no way for the frontend to distinguish it from a
    restored chat-1 tab. After reseed, the new slot must get a strictly higher,
    unused index.
    """
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
    # The minted index must be exactly one past the highest restored index (7).
    # Pins the pre-increment mint contract: get_or_create_slot does
    # `_slot_counter += 1` BEFORE formatting the key. If mint ever regressed to
    # post-increment, the new key would be chat-7-* and re-collide — this catches it.
    assert int(new_slot.key.rsplit("-", 2)[1]) == 8


def test_reseed_skips_unicode_digit_key_without_crashing(tmp_path, monkeypatch):
    """A stray unicode-digit segment must not crash boot-time reseeding.

    str.isdigit() is True for chars like superscript '²', but int() raises
    ValueError on them. The isascii() guard must skip such a key rather than
    letting the exception abort start_dashboard. (Not reachable for minted keys,
    which interpolate real ints — this pins the belt-and-suspenders guard.)
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
# start_flush_loop() arms the 5s periodic flush BEFORE the startup open-tab
# restore runs (several awaits — yolo apply, dropped-grant read, channel
# transcript migration, the cautious-boot pause — separate the two). In that
# gap ``_slots`` is empty and ``restoring_open_slots`` is still False, so a
# flush landing there snapshots ``{"keys": []}`` over a good open_slots.json.
# The NEXT restart then finds no tabs and every session lands in "older
# sessions". ``open_slots_restored`` gates the empty write until the restore
# has actually run this boot.


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
    """A non-empty pre-restore flush must NOT shrink the seed either.

    The HTTP listener binds before restore, so a user can create a new chat
    while the gateway is still booting. That makes ``_slots`` non-empty — but it
    holds ONLY the boot-time tab, not the seeded ones the restore has yet to
    read. Pruning the seed down to it would lose every other tab across the next
    restart. Before restore the writer MERGES instead of pruning: it folds the
    existing on-disk seed in, so the write is the union of the seed and the
    boot-time tab. A crash in this window keeps both, and the restore (which
    reads open_slots.json) still finds every seeded tab.
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
    """End-to-end: a flush in the pre-restore gap of boot #1 does not strand
    idle sessions in "older" after boot #2.

    Boot #1: a good seed exists, the flush loop fires before restore (empty
    _slots), THEN restore runs. Boot #2: the seed must still list every idle
    session so restore_open_slots rehydrates them as active sidebar slots.
    """
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
    """A crash in the pre-restore window must lose neither the seed nor a new tab.

    The HTTP listener binds before restore, so a chat opened during boot (C) is
    live while the seed (A, B) has not been read yet. If the gateway dies after
    a flush but before restore, the next boot restores from whatever that flush
    left on disk. The pre-restore merge means that flush wrote A ∪ B ∪ C, so the
    next boot rehydrates all three — the boot-time tab is NOT lost, and it did
    NOT replace the seed.
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
    """A transient seed read failure pre-restore must skip the write, not shrink.

    If ``_read_persisted_open_slot_keys`` cannot read an existing seed (e.g. a
    Windows sharing violation from a foreign handle), merging an empty read
    would write the live keys alone and shrink the file — losing the seeded
    tabs the restore has yet to read. The writer must skip the whole write and
    leave the on-disk seed intact for the next flush to retry.
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
    """A surface that never runs a restore (e.g. headless) still persists.

    The pre-restore path merges rather than skipping, so the open-slot write is
    never a permanent off-switch on a surface whose live set is authoritative
    from construction and that never calls a restore driver.
    """
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
    """The sibling writer must not prune context snapshots pre-restore either.

    _persist_context_snapshots prunes the snapshot map down to ``set(_slots)``.
    Armed before restore (empty _slots), a prune would delete every restored
    tab's context reading. Before restore the writer skips the prune and writes
    the union of disk and live readings, so the restored tabs' readings and the
    boot-time reading both survive. Same gap, same cause as _persist_open_slots.
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
    """A failed snapshot read before restore must not replace the file.

    The flush merges the disk readings into memory before it writes. If that
    read fails transiently (a Windows sharing violation) and the failure were
    latched as an empty, loaded map, the write would replace the file with
    only the boot-time reading and destroy every other tab's saved reading.
    The write is skipped instead, the same rule as the open_slots.json seed.
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

    Several endpoint suites build their fixture as
    ``DashboardState.__new__(DashboardState)`` and then set only the attributes
    the handler under test touches — they never run __init__. push_slots_update
    reads the suspend counter on EVERY call, so keeping that counter as an
    __init__-only assignment made all of those suites raise AttributeError
    (19 failures across test_queue_cancel/edit/reorder in CI). The counter and
    its companions are therefore class-level defaults; this test pins that so a
    future __init__-only attribute added to the same read path cannot silently
    reintroduce the break.
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
    """A tab must survive a Windows sharing violation on its transcript.

    ``_restore_open_slots_steps`` skips any key whose metadata reads back empty,
    on the reasoning that the session was never persisted -- and it does so
    silently (``logger.debug``). But ``_read_metadata`` also returned ``{}`` when
    it simply could not OPEN the file, which on Windows happens transiently while
    an indexer or AV scanner holds a just-written transcript
    (``ERROR_SHARING_VIOLATION`` -> ``PermissionError``). The two were
    indistinguishable, so one unlucky read silently cost the user a tab and the
    restore returned one short -- the shape of the intermittent
    ``assert 5 == 6`` / ``assert 7 == 8`` failures on the Windows CI line.

    Faults the FIRST read of exactly one session's transcript, which is what a
    scanner holding one file looks like, and requires the full set back.
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

    Companion to ``test_a_transient_metadata_read_failure_does_not_drop_a_tab``,
    which covers the metadata (first-line) read. ``_read_messages`` is on the
    same restore path (``read_messages_chained`` ->
    ``_rehydrate_slot_from_history``), and before the fix its body read
    propagated a Windows ``PermissionError`` (``ERROR_SHARING_VIOLATION``, an
    ``OSError`` subclass) straight out of rehydrate. ``_restore_open_slots_steps``
    then dropped the tab -- the same intermittent ``assert 7 == 8`` shape on the
    Windows CI line, but from the body read rather than the metadata read, so the
    metadata-only retry did not cover it.

    Primes the metadata cache first so the ONLY open under the violation is the
    body read we want to exercise, faults it once, and requires the messages
    back intact.
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

    Swallowing an exhausted read to ``[]`` is
    indistinguishable from a genuinely empty session, so
    ``_rehydrate_slot_from_history`` would register an EMPTY slot -- which
    ``restore_recent_sessions`` then dedupes by key and skips, stranding the tab
    history-less for the whole session. ``_read_messages`` must instead re-raise
    on exhaustion so rehydrate rolls back and the fallback restore can retry.
    Transient failures are still absorbed (see the companion test above).
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

    ``_read_messages`` re-raises a persistent OSError so
    restore can drop+retry the tab -- but ``FileNotFoundError`` is NOT a
    transient lock. Re-raising it would turn a benign concurrent
    ``delete_session`` into an HTTP 500 in a caller like ``api_session_detail``,
    which reaches ``read_messages`` after its own ``exists()`` check. It must be
    caught separately and return ``[]`` (matching the ``exists()``-miss branch),
    without spending the retry budget on a file that is gone.
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
    """End-to-end: a persistent body-read failure DROPS the tab, never registers
    it empty.

    Pinned at the restore layer: the other tabs restore, and the
    unreadable one is ABSENT from ``_slots`` (so the mtime-based
    ``restore_recent_sessions`` fallback -- and the next restart -- can still
    recover it) rather than being registered as a history-less slot that the
    dedup guard would then skip.
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
    """A tab dropped by an unreadable read must survive in open_slots.json.

    A retry keeps a ONE-shot sharing violation from costing a tab
    (see ``test_a_transient_metadata_read_failure_does_not_drop_a_tab``). But
    when the retry budget is exhausted the tab is still dropped, and that drop
    must be deferred rather than PERMANENT: this snapshot is taken from live
    ``_slots``, the ``restoring_open_slots`` guard is released as soon as the
    restore finishes, and the next 5s flush therefore rewrites the file WITHOUT
    the dropped key. That erases the only seed a later restore could
    have recovered from -- and ``dashboard.restore_sessions`` defaults to
    ``False``, so the ``restore_recent_sessions`` fallback is not a safety net
    for an unfoldered tab.

    Asserts the seed, not the slot: dropping the tab for this boot is the
    intended behaviour, losing the ability to ever restore it is not.
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
    """Negative control: a key with no transcript is still pruned.

    Preservation must be scoped to reads that FAILED. A session that genuinely
    has no transcript is a real answer, and keeping its key would resurrect a
    dead tab on every restart forever.
    """
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

    GPT 6.1 F1 / crash-data-loss: a replacement task restores ``open_slots.json``
    from a committed remote snapshot but NOT the transcripts (they load lazily,
    per turn). The listed slot then has empty-but-readable local metadata -- the
    same signal as a genuinely-absent session -- and the plain restore prunes it,
    so the next flush writes an empty ``open_slots.json`` the sidecar commits:
    silent loss of the whole restored slot table on an ordinary replacement.

    With ``ENV_AUTHORITY_RESTORED`` set, the key is kept as a reopen seed and the
    flush preserves it. The negative control
    ``test_absent_session_is_dropped_from_reopen_seed`` proves the ordinary boot
    (flag unset) still prunes, so dead tabs do not resurrect forever.
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
    """The restore layer must name the dropped tab, not just the history layer.

    ``_read_metadata`` warns that it could not read the file, but nothing said a
    TAB was affected -- ``restored`` was simply one lower, which is unactionable
    when the user reports "a tab disappeared".
    """
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
    """A transcript whose first line is valid JSON but not an OBJECT is skipped.

    ``json.loads`` happily returns ``None`` / a list / a str / an int, and a bare
    ``data.get("_type")`` on any of those raises ``AttributeError`` -- NOT
    ``JSONDecodeError``. Two things must hold:

    1. It stays isolated to the one tab. ``restore_open_slots_async`` has no
       ``except`` at its call site in ``server.py``, so an escaping exception
       aborts dashboard startup and no LATER tab restores either.
    2. It is treated as a corrupt line, exactly like an undecodable one: a
       genuine empty answer, so the key is PRUNED from the reopen seed. Carrying
       it would retry a permanently-broken transcript on every boot forever,
       and would disagree with the ``{not json`` case for no reason.
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
