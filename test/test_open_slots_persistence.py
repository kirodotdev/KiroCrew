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
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state
from windows_sim import builtin_open_sharing_violation

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    restore_open_slots,
    restore_open_slots_async,
)
from kiro_crew.dashboard.chat_utils import _history_key_for
from kiro_crew.dashboard.state import DashboardState


def _seed_session(state, slot_key: str, *, closed: bool = False) -> None:
    """Write a minimal session metadata + one user message so rehydrate succeeds."""
    history_key = _history_key_for(slot_key)
    log = state.conversation_log
    assert log is not None
    log.append(history_key, "user", "hello")
    if closed:
        # Use the canonical update_metadata helper rather than manually
        # rewriting the JSONL — depends only on the public API and is
        # resilient to format changes.
        log.update_metadata(history_key, {"closed": True})


def test_persist_writes_open_slots_json(tmp_path, monkeypatch):
    """_persist_open_slots writes the live slot keys to <config_dir>/open_slots.json."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
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
    state.get_or_create_slot("chat-1-foo")
    state._persist_open_slots()
    # Add another slot and re-persist
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


def test_restore_open_slots_rehydrates_project_identity(tmp_path, monkeypatch):
    """A Project-backed session keeps its bundle identity across restart."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-project")
    history_key = _history_key_for("chat-1-project")
    state.conversation_log.update_metadata(
        history_key,
        {
            "project": "/tmp/payments",
            "project_id": "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e",
        },
    )
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-project"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state2) == 1
    restored = state2._slots["chat-1-project"]
    assert restored.project == "/tmp/payments"
    assert restored.project_id == "018f4f4a-760f-7a8b-a5d4-5a7e0f130d4e"
    assert restored.to_dict()["project_id"] == restored.project_id


def test_restore_open_slots_rejects_an_object_project_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-project")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-project"), {"project_id": {"invalid": True}}
    )
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-project"], "ts": 0.0}))

    restored_state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(restored_state) == 1
    assert restored_state._slots["chat-1-project"].project_id == ""


def test_restore_open_slots_rejects_a_noncanonical_project_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-project")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-project"), {"project_id": "NOT-A-UUID"}
    )
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-project"], "ts": 0.0}))

    restored_state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(restored_state) == 1
    assert restored_state._slots["chat-1-project"].project_id == ""


def test_restore_recent_sessions_rejects_an_object_project_identity(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_persistence import restore_recent_sessions

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-project")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-project"), {"project_id": {"invalid": True}}
    )

    restored_state = _make_state(tmp_path / "sessions")
    assert restore_recent_sessions(restored_state, window_minutes=0) == 1
    assert restored_state._slots["chat-1-project"].project_id == ""


def test_restore_recent_sessions_rejects_a_noncanonical_project_identity(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_persistence import restore_recent_sessions

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-project")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-project"), {"project_id": "NOT-A-UUID"}
    )

    restored_state = _make_state(tmp_path / "sessions")
    assert restore_recent_sessions(restored_state, window_minutes=0) == 1
    assert restored_state._slots["chat-1-project"].project_id == ""


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


def test_rehydrate_slot_uses_chained_read_with_500_message_window(tmp_path, monkeypatch):
    """Chained read + 500-message window on rehydrate.

    ``_rehydrate_slot_from_history`` previously called
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
# Display-style slot names (e.g. "Artifact: My Doc" from the artifact iterate
# flow) used to survive as raw slot keys while their JSONL filename got the
# lossy _safe_key() fold. After a restart, restore_open_slots rehydrated the
# raw key from open_slots.json while restore_recent_sessions derived a SECOND
# slot from the filename stem — two identical sidebar sessions backed by one
# transcript. get_or_create_slot now folds keys to the filename charset, and
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
    """End-to-end regression: open_slots replay + filename-stem walk = 1 slot.

    This is the exact user-visible bug: a raw display-style key in
    open_slots.json plus the mtime-based restore_recent_sessions walk used to
    produce two identical sidebar sessions after a gateway restart.
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


# ── Startup must not block the event loop (stall-watchdog crash-loop) ──
#
# Regression cover for the gateway crash-loop: restoring many large tabs ran
# synchronously on the event loop, so the LoopStallWatchdog heartbeat (which pets
# the watchdog FROM A COROUTINE) never got a turn. After exit_after=25s the
# watchdog dumped thread stacks and _exit()ed, so the app never finished starting.


def test_restore_open_slots_async_yields_between_tabs(tmp_path, monkeypatch):
    """The async restore must hand the loop back per tab so the heartbeat can run.

    Pins the actual crash mechanism: a coroutine running concurrently with the
    restore must observe a partially restored slot set. If restore ever blocks
    through every tab and yields only after the work is done, this fails.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    tab_count = 6
    for i in range(tab_count):
        _seed_session(state, f"chat-{i}-yield")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-yield" for i in range(tab_count)], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    partial_restore_observed = False

    async def _drive():
        async def ticker():
            nonlocal partial_restore_observed
            while True:
                restored_so_far = len(state2._slots)
                if 0 < restored_so_far < tab_count:
                    partial_restore_observed = True
                await asyncio.sleep(0)

        t = asyncio.create_task(ticker())
        try:
            return await restore_open_slots_async(state2)
        finally:
            t.cancel()
            # `cancel()` only requests cancellation; without awaiting it the ticker is
            # still live when `asyncio.run` tears the loop down, leaving a "coroutine
            # ignored GeneratorExit" for a later test to trip over.
            try:
                await t
            except asyncio.CancelledError:
                pass

    restored = asyncio.run(_drive())
    assert restored == tab_count
    # Observe intermediate progress rather than an incidental number of scheduler
    # turns; a single yield after all tab work is complete must not satisfy the test.
    assert partial_restore_observed, "restore did not yield between tabs"


def test_restore_reads_transcript_before_backfilling_tab_id(tmp_path, monkeypatch):
    """A tab needing a tab_id backfill must be READ before the backfill fires.

    ``_rehydrate_slot_from_history`` mints a tab_id for a legacy session that
    lacks one and persists it via ``update_metadata_off_loop`` — which dispatches
    an ``os.replace()`` of THIS session file to a worker thread. If that write is
    dispatched BEFORE the loop-thread transcript read of the same file, the
    replace races the read: on Windows the in-flight replace makes the reader's
    ``open()`` raise a sharing violation, and the on-loop read retry cannot pause
    (a loop sleep would starve the LoopStallWatchdog), so it drops the tab —
    the intermittent ``restored == N-1`` open-tabs loss on restart.

    Reproduced deterministically without threads: dispatching the backfill for a
    key arms its transcript read to raise the sharing violation, standing in for
    the in-flight replace holding the file. Under the correct order (read first)
    the read completes while the file is quiescent, so nothing is ever armed and
    every tab restores. Under the buggy order the armed read faults and the tab
    is dropped. The read-retry mechanics themselves are out of scope here (they
    are exercised by test_history's sharing-violation tests); this pins the
    ordering that keeps the file quiescent for the read in the first place.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    n = 4
    for i in range(n):
        # _seed_session writes no tab_id, so every tab triggers the backfill.
        _seed_session(state, f"chat-{i}-race")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-race" for i in range(n)], "ts": 0.0})
    )

    import kiro_crew.dashboard.chat_persistence as chat_persistence

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    armed_unreadable: set[str] = set()

    real_read_messages = log._read_messages

    def guarded_read_messages(key):
        if key in armed_unreadable:
            raise PermissionError(f"[WinError 32] simulated in-flight os.replace holding {key}")
        return real_read_messages(key)

    monkeypatch.setattr(log, "_read_messages", guarded_read_messages)

    real_backfill = chat_persistence.update_metadata_off_loop

    def arming_backfill(conv_log, key, fields):
        # Dispatching the tab_id os.replace makes the file briefly unreadable.
        armed_unreadable.add(key)
        return real_backfill(conv_log, key, fields)

    monkeypatch.setattr(chat_persistence, "update_metadata_off_loop", arming_backfill)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == n, (
        "a tab was dropped: its transcript was read while its tab_id backfill "
        "replace was in flight (read must precede the backfill dispatch)"
    )
    assert set(state2._slots) == {f"chat-{i}-race" for i in range(n)}


def test_restore_open_slots_async_matches_sync_result(tmp_path, monkeypatch):
    """The async and sync drivers must restore the same slots.

    They share one generator, so this guards the two thin wrappers from drifting.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-same")
    _seed_session(state, "chat-2-same")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-same", "chat-2-same"], "ts": 0.0})
    )

    sync_state = _make_state(tmp_path / "sessions")
    async_state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(sync_state) == asyncio.run(restore_open_slots_async(async_state))
    assert set(sync_state._slots) == set(async_state._slots) == {"chat-1-same", "chat-2-same"}


def test_rehydrate_does_not_scan_the_whole_session_dir(tmp_path, monkeypatch):
    """Rehydrating one tab must not call list_sessions().

    list_sessions() stats + reads the first line of EVERY session file. It used to
    run once per restored tab purely to look up one title (which it never actually
    found — its keys are filename stems, ``dashboard_x``, while the lookup used the
    canonical ``dashboard:x``), making restore O(tabs x all sessions). That was ~13s
    of the stall on a real 77-tab home.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-noscan")
    state.conversation_log.update_metadata(
        _history_key_for("chat-1-noscan"), {"title": "Kept Title"}
    )

    state2 = _make_state(tmp_path / "sessions")
    with patch.object(
        type(state2.conversation_log),
        "list_sessions",
        side_effect=AssertionError("list_sessions() must not be called per slot"),
    ):
        slot = _rehydrate_slot_from_history(state2, "chat-1-noscan")

    assert slot is not None
    # Title still comes through, from the metadata line we already read.
    assert slot.title == "Kept Title"
    assert slot._titled is True


def test_bulk_restore_emits_one_slots_broadcast(tmp_path, monkeypatch):
    """suspend_slots_push() coalesces the per-slot broadcasts into one.

    get_or_create_slot() broadcasts the FULL slot list every call, so restoring N
    tabs serialized 1+2+...+N slots — quadratic redaction work for intermediate
    states no client ever renders.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for i in range(5):
        _seed_session(state, f"chat-{i}-bcast")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": [f"chat-{i}-bcast" for i in range(5)], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state2._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state2.suspend_slots_push():
        restored = restore_open_slots(state2)

    assert restored == 5
    assert seen.count("slots") == 1, f"expected 1 coalesced slots push, got {seen.count('slots')}"


def test_suspend_slots_push_unwinds_and_flushes_on_exception(tmp_path, monkeypatch):
    """The suspend depth must unwind (and the owed push fire) even if the body raises.

    Otherwise one failed restore would leave the gateway permanently unable to
    broadcast slot updates.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        with state.suspend_slots_push():
            state.push_slots_update()
            raise RuntimeError("boom")

    assert state._slots_push_suspend == 0
    assert seen.count("slots") == 1


def test_suspend_slots_push_nested_does_not_flush_early(tmp_path, monkeypatch):
    """Only the OUTERMOST suspend block flushes — nested users must not push early."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state.suspend_slots_push():
        with state.suspend_slots_push():
            state.push_slots_update()
        assert seen.count("slots") == 0, "inner exit flushed early"
    assert seen.count("slots") == 1


def test_suspend_slots_push_no_push_means_no_broadcast(tmp_path, monkeypatch):
    """An empty suspend block must not synthesize a broadcast nobody asked for."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    seen: list[str] = []
    state._broadcast = lambda note: seen.append(note.get("_type"))  # type: ignore[method-assign]

    with state.suspend_slots_push():
        pass

    assert seen.count("slots") == 0


# ── The deferred restore must not let a flush truncate the snapshot ──
#
# start_flush_loop() is running (every 5s) BEFORE the startup restore. While the
# restore was synchronous it starved that timer, so a flush could never land
# mid-restore. Now that the restore yields per tab, one can — and
# _flush_dirty_slots calls _persist_open_slots, which would overwrite the very
# file being restored FROM with a half-populated slot set. Reproduced at real
# scale before the guard: 77 tabs collapsed to 70.


def test_flush_during_async_restore_does_not_truncate_snapshot(tmp_path, monkeypatch):
    """A flush landing mid-restore must not shrink open_slots.json."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-flush" for i in range(8)]
    for k in keys:
        _seed_session(state, k)
    snapshot = tmp_path / "open_slots.json"
    snapshot.write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")

    # Fire the flush deterministically at the restore's own yield point — that is
    # exactly where the real 5s flush timer gets to run — rather than racing a
    # second task (which the scheduler may not interleave at all).
    real_sleep = asyncio.sleep
    observed: list[int] = []

    async def flushing_sleep(delay, *a, **kw):
        if delay == 0:
            state2._persist_open_slots()
            # Record what a crash at THIS instant would leave on disk.
            observed.append(len(json.loads(snapshot.read_text())["keys"]))
        return await real_sleep(delay, *a, **kw)

    with patch("kiro_crew.dashboard.chat_persistence.asyncio.sleep", side_effect=flushing_sleep):
        restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 8
    assert observed, "flush never landed mid-restore — test would not detect the bug"
    # Assert on the INTERMEDIATE states, not just the final one. Without the guard
    # the file transiently reads 1, 2, 3 … tabs; it only ends up complete because
    # the restore happens to finish. A kill in that window is what loses tabs.
    assert all(
        n == len(keys) for n in observed
    ), f"snapshot was truncated mid-restore: sizes {observed} (expected all {len(keys)})"
    assert set(json.loads(snapshot.read_text())["keys"]) == set(keys)


def test_restoring_flag_clears_and_reenables_persistence(tmp_path, monkeypatch):
    """The guard must be released after the restore so snapshots resume."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-flag")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-flag"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    assert state2.restoring_open_slots is False
    asyncio.run(restore_open_slots_async(state2))
    assert state2.restoring_open_slots is False

    # A normal flush after restore writes the live set again.
    state2.get_or_create_slot("chat-9-postrestore")
    state2._persist_open_slots()
    assert set(json.loads((tmp_path / "open_slots.json").read_text())["keys"]) == {
        "chat-1-flag",
        "chat-9-postrestore",
    }


def test_restoring_flag_cleared_even_if_restore_raises(tmp_path, monkeypatch):
    """A crash mid-restore must not leave open-tab persistence disabled forever."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-boom")
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": ["chat-1-boom"], "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    with patch(
        "kiro_crew.dashboard.chat_persistence._build_kiro_model_map",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(restore_open_slots_async(state2))

    assert state2.restoring_open_slots is False


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

    GPT review (PR #2052): swallowing an exhausted read to ``[]`` is
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

    GPT review (PR #2052): ``_read_messages`` re-raises a persistent OSError so
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

    Pins the GPT #2052 fix at the restore layer: the other tabs restore, and the
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

    #1733 added a retry, so a ONE-shot sharing violation no longer costs a tab
    (see ``test_a_transient_metadata_read_failure_does_not_drop_a_tab``). But
    when the retry budget is exhausted the tab is still dropped, and the drop
    was previously PERMANENT rather than deferred: this snapshot is taken from
    live ``_slots``, the ``restoring_open_slots`` guard is released as soon as
    the restore finishes, and the next 5s flush therefore rewrites the file
    WITHOUT the dropped key. That erases the only seed a later restore could
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

    # The flush that previously erased it. Guard is already released by now.
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


# ── Startup must not READ on the event loop either (#895) ──
#
# The per-tab yield above bounded how long ONE tab could hold the loop, but the
# per-tab WORK still ran on it: get_metadata_status plus a chained
# read_messages_chained walk on a multi-megabyte transcript is seconds of loop
# time *between* yields, and the ``open_slots.json`` read plus the agent→model
# map glob were on-loop before the first yield ever arrived. So the reads now
# move into asyncio.to_thread and only the slot mutation stays on the loop --
# the same prefetch-then-apply split rehydrate_slot_from_history_async
# established, and for the same non-negotiable reason (slot construction
# broadcasts through asyncio.Queue.put_nowait / Event.set, neither thread-safe).


def _record_restore_threads(monkeypatch, state):
    """Instrument one state's restore so each half's thread is observable.

    Returns a dict of thread-name lists, filled in as the restore runs:

    * ``walk`` — ``read_messages_chained``, the expensive half. Must NEVER run on
      the loop thread; this is the property #895 is about.
    * ``prefetch`` — ``_prefetch_rehydrate_inputs``, the offloaded read bundle.
    * ``recheck`` — ``_deletion_during_read``, the post-hop deletion guard, which
      must run ON the loop *by design*: it gates the build and no suspension point
      may separate the two. So it is asserted separately rather than lumped in
      with the reads.
    * ``build`` — ``_rehydrate_slot_from_history``, loop-affine.
    * ``snapshot`` — the ``open_slots.json`` read.

    Wraps the REAL implementations so the restore still completes end to end and
    the assertions are about placement, not about a stub's behaviour.
    """
    from kiro_crew.dashboard import chat_persistence

    log = state.conversation_log
    assert log is not None
    seen: dict[str, list[str]] = {
        "walk": [],
        "prefetch": [],
        "recheck": [],
        "build": [],
        "snapshot": [],
    }

    real_chained = log.read_messages_chained
    real_prefetch = chat_persistence._prefetch_rehydrate_inputs
    real_recheck = chat_persistence._deletion_during_read
    real_build = chat_persistence._rehydrate_slot_from_history
    real_keys = chat_persistence._read_open_slots_keys

    def _chained(key):
        seen["walk"].append(threading.current_thread().name)
        return real_chained(key)

    def _prefetch(*a, **kw):
        seen["prefetch"].append(threading.current_thread().name)
        return real_prefetch(*a, **kw)

    def _recheck(*a, **kw):
        seen["recheck"].append(threading.current_thread().name)
        return real_recheck(*a, **kw)

    def _build(*a, **kw):
        seen["build"].append(threading.current_thread().name)
        return real_build(*a, **kw)

    def _keys():
        seen["snapshot"].append(threading.current_thread().name)
        return real_keys()

    monkeypatch.setattr(log, "read_messages_chained", _chained)
    monkeypatch.setattr(chat_persistence, "_prefetch_rehydrate_inputs", _prefetch)
    monkeypatch.setattr(chat_persistence, "_deletion_during_read", _recheck)
    monkeypatch.setattr(chat_persistence, "_rehydrate_slot_from_history", _build)
    monkeypatch.setattr(chat_persistence, "_read_open_slots_keys", _keys)
    return seen


def test_restore_open_slots_async_reads_off_the_loop(tmp_path, monkeypatch):
    """Every per-tab disk read must run in a worker thread, not on the loop.

    This is the assertion the yield-only fix could not make: it passes only when
    the metadata read and the chained transcript walk have actually left the
    event-loop thread. Driving the synchronous generator (the previous shape)
    fails it on the first tab.

    The post-hop deletion re-check is deliberately excluded and asserted to be ON
    the loop instead — see the sibling test below.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = [f"chat-{i}-offloop" for i in range(3)]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == len(keys)
    assert set(state2._slots) == set(keys)
    assert seen["walk"], "no transcript was read — the test would not detect the bug"
    assert all(t != main for t in seen["walk"]), (
        "a transcript walk ran ON the event-loop thread; a large transcript "
        f"there stalls the stall-watchdog heartbeat (threads: {sorted(set(seen['walk']))})"
    )
    assert seen["prefetch"] and all(t != main for t in seen["prefetch"]), (
        f"the prefetch bundle ran on the loop (threads: {sorted(set(seen['prefetch']))})"
    )
    assert seen["snapshot"] and all(t != main for t in seen["snapshot"]), (
        "open_slots.json was read on the event loop before the first yield"
    )
    assert seen["build"] == [main] * len(keys), (
        "slot construction left the event-loop thread; it broadcasts through "
        "asyncio.Queue.put_nowait / Event.set, neither of which is thread-safe "
        f"(threads: {seen['build']})"
    )


def test_the_deletion_recheck_runs_on_the_loop_next_to_the_build(tmp_path, monkeypatch):
    """The guard must NOT be offloaded — that would reopen the window it closes.

    ``_deletion_during_read`` gates the build, so an await between the two would
    let a delete land in the gap. It is one mtime-cached metadata line, which is
    why paying for it on the loop is the right trade.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-gated")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-gated"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert seen["recheck"] == [main], (
        "the deletion re-check must run on the loop, immediately before the "
        f"build it gates (threads: {seen['recheck']})"
    )
    assert seen["build"] == [main]


def test_restore_open_slots_sync_driver_still_reads_inline(tmp_path, monkeypatch):
    """The synchronous caller keeps its inline reads — no loop to offload to.

    Guards the split from being "fixed" by making the sync path spawn threads it
    has no reason to: with no running loop, ``_locked`` already takes the patient
    path and a thread hop would only add latency. It also must not pay for the
    post-hop deletion re-check, since its read has no suspension point to go
    stale across.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-inline")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-inline"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    seen = _record_restore_threads(monkeypatch, state2)

    main = threading.current_thread().name
    assert restore_open_slots(state2) == 1
    assert seen["walk"] and all(t == main for t in seen["walk"])
    assert seen["build"] == [main]
    assert seen["recheck"] == [], (
        "the sync driver paid for a post-hop re-check it cannot need"
    )


def test_async_restore_keeps_an_unreadable_tab_in_the_reopen_seed(tmp_path, monkeypatch):
    """The offloaded read must still distinguish "unreadable" from "gone".

    ``get_metadata`` reports ``{}`` for both, and treating the second as the
    first is what silently drops a live tab (the Windows sharing-violation
    shape). Moving the read into a worker thread must not lose the
    ``get_metadata_status`` readability flag that carries the difference.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-ok", "chat-2-unreadable"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_status = log.get_metadata_status

    def _status(key):
        if key.endswith("unreadable"):
            return {}, False
        return real_status(key)

    monkeypatch.setattr(log, "get_metadata_status", _status)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert "chat-1-ok" in state2._slots
    assert "chat-2-unreadable" not in state2._slots
    assert "chat-2-unreadable" in state2.unrestored_slot_keys, (
        "an unreadable tab was dropped from the reopen seed instead of carried"
    )


def test_async_restore_does_not_carry_a_confidently_absent_tab(tmp_path, monkeypatch):
    """A readable-but-empty metadata read is a confident answer, not a retry.

    Companion to the test above: carrying every miss would resurrect keys for
    sessions that genuinely no longer exist, forever.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-present")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-present", "chat-2-absent"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert "chat-2-absent" not in state2.unrestored_slot_keys


def test_async_restore_rejects_a_path_separator_key(tmp_path, monkeypatch):
    """The path-traversal screen must survive the driver rewrite.

    ``open_slots.json`` is attacker-writable in the threat model the screen
    exists for, and the async driver no longer shares the generator's body — so
    the screen is asserted on the driver that startup actually runs.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-clean")
    (tmp_path / "open_slots.json").write_text(
        json.dumps(
            {"keys": ["../../etc/passwd", "..\\..\\windows", "chat-1-clean"], "ts": 0.0}
        )
    )

    state2 = _make_state(tmp_path / "sessions")
    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-clean"}


def test_async_restore_leaves_a_carried_seed_alone_when_there_is_no_snapshot(
    tmp_path, monkeypatch
):
    """A missing snapshot is a no-op — including for ``unrestored_slot_keys``.

    The set is rebound per restore so a key that becomes readable stops being
    carried. Rebinding it when there is nothing to restore FROM would instead
    erase a seed the previous boot deliberately kept.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    carried = {"chat-7-carried"}
    state.unrestored_slot_keys = carried

    assert asyncio.run(restore_open_slots_async(state)) == 0
    assert state.unrestored_slot_keys is carried
    assert state.restoring_open_slots is False


# ── Deletion during the offloaded read (#895 round 3) ──
#
# ``ConversationLog.delete_session`` leaves NO tombstone — its own docstring
# notes that once the delete releases the lock "a concurrent writer can recreate
# the session". Offloading the transcript read opened a window where the user can
# permanently delete a session while restore holds its content, and the published
# slot would rewrite the deleted file on its next flush. The dashboard's HTTP
# listener is bound BEFORE startup restore runs (``_start_site`` precedes it in
# ``start_dashboard``), so this is reachable, not theoretical.
#
# The chat-resume handler already guards its own read this way; the restore paths
# mirror it rather than reinventing it. Raised as blocking by GPT 5.6 review.


def test_a_session_deleted_during_the_read_is_not_restored(tmp_path, monkeypatch):
    """A tab whose session vanished mid-read must not be rebuilt from stale bytes."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-keep", "chat-2-deleted"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _delete_then_read(key):
        msgs = real_chained(key)
        if "chat-2-deleted" in key:
            # The user hits Delete while this transcript is in flight.
            log.delete_session(key)
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _delete_then_read)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-keep"}, (
        "restored a tab for a session the user permanently deleted; its next "
        "flush would rewrite the deleted transcript"
    )
    # A confident answer, not a retry — the key must not be carried forward.
    assert "chat-2-deleted" not in state2.unrestored_slot_keys


def test_a_session_recreated_during_the_read_is_not_overwritten(tmp_path, monkeypatch):
    """Delete-then-recreate leaves metadata PRESENT but belonging to a new chat.

    Existence alone reads that as "still here" and would publish a slot holding
    the OLD transcript, whose flush overwrites a conversation the user is actively
    using — worse than the plain-delete arm, because the destroyed data is live.
    ``created_at`` is the discriminator.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-swapped")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-swapped"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _swap_then_read(key):
        msgs = real_chained(key)
        if "swapped" in key:
            log.delete_session(key)
            log.append(key, "user", "a brand new conversation")
            log.update_metadata(key, {"created_at": "2099-01-01T00:00:00"})
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _swap_then_read)

    assert asyncio.run(restore_open_slots_async(state2)) == 0
    assert "chat-1-swapped" not in state2._slots
    # The replacement conversation is intact on disk.
    assert [m.get("content") for m in log.read_messages_chained(
        _history_key_for("chat-1-swapped")
    )] == ["a brand new conversation"]


def test_an_unreadable_recheck_still_restores_the_tab(tmp_path, monkeypatch):
    """Refusing is the destructive direction on doubt, so unreadable != deleted.

    ``get_metadata`` returns ``{}`` for both "deleted" and "could not be read",
    and treating an unreadable line as a deletion would silently discard a LIVE
    tab — the Windows sharing-violation shape again. The guard must fall through
    to restoring.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-flaky")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-flaky"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_status = log.get_metadata_status
    calls = {"n": 0}

    def _status(key):
        calls["n"] += 1
        # First call is the prefetch (must succeed); the post-hop re-check reads
        # back unreadable.
        if calls["n"] > 1:
            return {}, False
        return real_status(key)

    monkeypatch.setattr(log, "get_metadata_status", _status)

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-flaky" in state2._slots
    assert calls["n"] >= 2, "the post-hop re-check never ran"


def test_a_rewrite_that_preserves_created_at_is_not_refused(tmp_path, monkeypatch):
    """A compaction/rewrite carries ``created_at`` through, so it must not fire.

    Guards the identity arm from being a blanket "anything changed" refusal,
    which would drop tabs on ordinary housekeeping.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-rewritten")
    log0 = state.conversation_log
    key0 = _history_key_for("chat-1-rewritten")
    log0.update_metadata(key0, {"created_at": "2026-01-01T00:00:00"})
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-rewritten"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    def _touch_then_read(key):
        msgs = real_chained(key)
        if "rewritten" in key:
            # Metadata churn that keeps identity — the rewrite case.
            log.update_metadata(key, {"title": "renamed by housekeeping"})
        return msgs

    monkeypatch.setattr(log, "read_messages_chained", _touch_then_read)

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-rewritten" in state2._slots


def test_missing_created_at_falls_through_to_restoring(tmp_path, monkeypatch):
    """Pre-``created_at`` transcripts must stay restorable.

    Refusing when the discriminator is absent on either side would reject every
    session whose metadata predates the field — a visible break for real users —
    to close a narrow race. The residual (an undetected recreate of such a
    transcript) is accepted and documented; the durable fix is a tombstone in
    ``history.delete_session``, which is out of scope.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-legacy")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-legacy"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    key = _history_key_for("chat-1-legacy")
    from kiro_crew.dashboard import chat_persistence as cp

    # The session is PRESENT (so the absence arm cannot fire) and the pre-read
    # metadata carries no ``created_at`` — the legacy-transcript shape. The
    # identity arm needs two DIFFERING stamps, so one missing side falls through.
    assert cp._deletion_during_read(log, key, {"title": "legacy"}, []) is None
    # And with both sides present but equal, it also falls through.
    live_meta = log.get_metadata(key)
    assert live_meta.get("created_at"), "fixture no longer stamps created_at"
    assert cp._deletion_during_read(log, key, live_meta, []) is None

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-legacy" in state2._slots


def test_a_never_persisted_key_is_not_treated_as_a_deletion(tmp_path, monkeypatch):
    """An absent key is a new conversation, not something that was deleted."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import chat_persistence as cp

    assert cp._deletion_during_read(
        state.conversation_log, "dashboard:never-existed", {}, None
    ) is None


def test_a_tab_closed_during_the_open_slot_read_is_not_restored(tmp_path, monkeypatch):
    """The open-tab driver needs the SAME tombstone re-check as its siblings.

    ``rehydrate_slot_from_history_async`` and the recent-sessions driver both
    consult the close tombstone after their thread hop; this driver was the one
    converted surface still missing it (GPT 5.6 review, round 3). The close pops
    the slot and records the tombstone synchronously, but persists the ``closed``
    flag only after its own awaits — so the metadata read mid-flight still says
    open. Restoring from it re-creates a dismissed tab, and worse, the restored
    slot's next flush writes metadata WITHOUT ``closed``, erasing the close.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    keys = ["chat-1-stays", "chat-2-dismissed"]
    for k in keys:
        _seed_session(state, k)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))

    state2 = _make_state(tmp_path / "sessions")
    log = state2.conversation_log
    real_chained = log.read_messages_chained

    from kiro_crew.dashboard import channel_slots

    def _close_then_read(key):
        if "chat-2-dismissed" in key:
            # The user clicks ✕ while this transcript is in flight.
            channel_slots.note_slot_closed(state2, "chat-2-dismissed")
        return real_chained(key)

    monkeypatch.setattr(log, "read_messages_chained", _close_then_read)

    restored = asyncio.run(restore_open_slots_async(state2))

    assert restored == 1
    assert set(state2._slots) == {"chat-1-stays"}, (
        "restored a tab the user dismissed mid-read; its flush would also clear "
        "the closed marker"
    )
    # A confident answer, not a retry — the key must not be carried forward.
    assert "chat-2-dismissed" not in state2.unrestored_slot_keys


def test_an_older_close_does_not_block_an_open_slot_restore(tmp_path, monkeypatch):
    """Negative control: only a close DURING the read is the race.

    Without the ``>= started`` comparison the guard would make a reopened tab
    un-restorable for the tombstone's whole lifetime.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    _seed_session(state, "chat-1-reopened")
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": ["chat-1-reopened"], "ts": 0.0})
    )

    state2 = _make_state(tmp_path / "sessions")
    from kiro_crew.dashboard import channel_slots

    channel_slots.note_slot_closed(state2, "chat-1-reopened")  # then reopened
    time.sleep(0.01)

    assert asyncio.run(restore_open_slots_async(state2)) == 1
    assert "chat-1-reopened" in state2._slots
