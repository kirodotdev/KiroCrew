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

import json
from unittest.mock import patch

from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import restore_open_slots
from kiro_crew.dashboard.chat_utils import _history_key_for


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
    snapshot_path.write_text(
        json.dumps({"keys": ["chat-2-legacy-no-tab-id"], "ts": 0.0})
    )
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

    with patch.object(state2.conversation_log, "read_messages_chained", _spy_chained), \
            patch.object(state2.conversation_log, "read_messages", _spy_flat):
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
    snapshot_path.write_text(
        json.dumps({"keys": ["chat-1-bigwindow"], "ts": 0.0})
    )

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

RAW_KEY = "Artifact: 2026 Code Activity Benchmark - nrb vs Dan Lloyd Org"
FOLDED_KEY = "Artifact__2026_Code_Activity_Benchmark_-_nrb_vs_Dan_Lloyd_Org"


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
