"""Audit-chain hardening tests for kiro_crew.sel: the HMAC key and the chain tail.

Pins HMAC key length validation, permission enforcement on load, atomic key creation,
the key's home under ``trust/`` with its migration and per-call path re-resolution, and
recovery past a corrupt trailing record without resetting the chain to genesis.
``test_sel.py`` holds the core contract and names the other topic modules; the shared
fixtures and helpers live in ``sel_helpers.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from sel_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _make_event,
    reset_singleton,
)

import kiro_crew.sel as sel_mod
from kiro_crew import platform_compat
from kiro_crew.sel import SecurityEventLog, sel_hmac_key_path

# ─────────────────────────────────────────────────────────────────────────
# Audit-chain hardening regression tests (Track B):
#   1. HMAC key length validation (reject empty/short keys — hard fail)
#   2. HMAC key permission re-enforcement on load
#   3. _read_last_hash does not reset the chain to genesis on a corrupt
#      trailing line when prior complete records exist
# ─────────────────────────────────────────────────────────────────────────


class TestHmacKeyValidation:
    def test_rejects_empty_key_file(self, tmp_path: Path) -> None:
        """A 0-byte key file must hard-fail init, not sign with an empty key."""
        (tmp_path / "sel_hmac.key").write_bytes(b"")
        with pytest.raises(RuntimeError, match="too short"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    def test_rejects_short_key_file(self, tmp_path: Path) -> None:
        """A present-but-too-short key (< 32 bytes) must hard-fail init."""
        (tmp_path / "sel_hmac.key").write_bytes(b"x" * 16)
        with pytest.raises(RuntimeError, match="require >= 32"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    def test_accepts_exactly_min_length_key(self, tmp_path: Path) -> None:
        """A key of exactly the minimum length is accepted."""
        key = b"k" * 32
        (tmp_path / "sel_hmac.key").write_bytes(key)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == key

    def test_generated_key_meets_minimum_length(self, tmp_path: Path) -> None:
        """The auto-generated key must satisfy the validation on next load."""
        SecurityEventLog(base_dir=tmp_path, sync=True)
        assert len((tmp_path / "trust" / "sel_hmac.key").read_bytes()) >= 32
        # Re-init from the on-disk key must not raise.
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert len(log2._hmac_key) == 32


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
class TestHmacKeyPermissionEnforcement:
    def test_created_key_is_owner_only(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        mode = (tmp_path / "trust" / "sel_hmac.key").stat().st_mode & 0o777
        assert mode == 0o600

    def test_reenforces_perms_on_load(self, tmp_path: Path) -> None:
        """A key file left group/world-readable must be tightened to 0600 on load."""
        key_path = tmp_path / "sel_hmac.key"
        key_path.write_bytes(b"k" * 32)
        os.chmod(key_path, 0o644)  # simulate relaxed perms (backup restore, etc.)
        SecurityEventLog(base_dir=tmp_path, sync=True)
        # The legacy file is migrated into trust/ and tightened there.
        migrated = tmp_path / "trust" / "sel_hmac.key"
        assert not key_path.exists()
        mode = migrated.stat().st_mode & 0o777
        assert mode == 0o600

    def test_chmod_failure_on_load_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chmod failure while re-enforcing perms on load must warn, not crash."""
        key = b"k" * 32
        (tmp_path / "sel_hmac.key").write_bytes(key)

        def _boom(*a, **kw):
            raise OSError("chmod denied")

        monkeypatch.setattr("kiro_crew.platform_compat.os.chmod", _boom)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == key


class TestReadLastHashCorruptTail:
    def test_corrupt_tail_chains_from_last_valid_record(self, tmp_path: Path) -> None:
        """A truncated final line must NOT reset the chain to genesis when
        prior complete records exist — the next record chains off the last
        COMPLETE record's entry_hash."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="e0"))
        log.log(_make_event(event_id="e1"))
        good_tip = log._last_hash
        path = tmp_path / "security_events.jsonl"
        # Simulate a crash mid-append: a partial/truncated trailing line.
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"event_id": "e2", "prev_hash": "abc", "entry_ha')

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        # Chain tip recovered from the last COMPLETE record, not reset to "".
        assert log2._last_hash == good_tip
        assert log2._last_hash != ""

    def test_new_record_after_corrupt_tail_keeps_chain_linked(self, tmp_path: Path) -> None:
        """After recovering past a corrupt tail, appending a new record links
        it to the surviving complete record (verify_integrity stays clean for
        the intact prefix)."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="a0"))
        log.log(_make_event(event_id="a1"))
        prev_tip = log._last_hash
        path = tmp_path / "security_events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"truncated": tru')  # invalid JSON tail

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._last_hash == prev_tip

    def test_only_corrupt_lines_returns_empty(self, tmp_path: Path) -> None:
        """When NO complete record exists, "" is still the correct tip (nothing
        to chain from) — preserves the genuine genesis case."""
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "security_events.jsonl").write_text("not-json-at-all\n")
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._last_hash == ""

    def test_non_object_json_tail_is_skipped(self, tmp_path: Path) -> None:
        """A valid-JSON-but-non-object trailing line (e.g. a bare number) must
        be skipped, not crash init on the .get() call."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="n0"))
        good_tip = log._last_hash
        path = tmp_path / "security_events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write("12345\n")

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._last_hash == good_tip

    def test_corrupt_tail_across_4kb_boundary(self, tmp_path: Path) -> None:
        """The recovery scan works even when the last complete record is more
        than one 4 KB chunk before the truncated tail."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        big = "x" * 200
        for i in range(60):  # ~15 KB — spans multiple 4 KB chunks
            log.log(_make_event(event_id=f"c{i:02d}", resources=big))
        good_tip = log._last_hash
        path = tmp_path / "security_events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"event_id": "trunc", "entry_ha')  # truncated tail

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._last_hash == good_tip


class TestCorruptTailNewlineBoundary:
    """A record appended after recovering past an UNTERMINATED corrupt tail
    must start on a fresh line — never glued onto the truncated fragment.

    The silent-void hazard: _read_last_hash() recovers the right
    prev_hash, but if the writer O_APPENDs directly onto a tail line with no
    trailing newline, the new record fuses into that fragment as one
    unparseable line — so the event, though correctly chained, is orphaned
    from every readable record (recent()/verify_integrity can't see it).
    """

    def _crash_with_truncated_tail(self, tmp_path: Path) -> tuple[str, str]:
        """Log two clean events, then simulate a crash mid-append (a trailing
        line with NO newline). Returns (recovered_tip, fragment)."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="e0"))
        log.log(_make_event(event_id="e1"))
        tip = log._last_hash
        fragment = '{"event_id": "e2", "prev_hash": "abc", "entry_ha'
        with open(tmp_path / "security_events.jsonl", "a", encoding="utf-8") as f:
            f.write(fragment)
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        return tip, fragment

    def test_new_record_is_parseable_after_corrupt_tail(self, tmp_path: Path) -> None:
        tip, fragment = self._crash_with_truncated_tail(tmp_path)
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._last_hash == tip  # recovered, not reset to genesis
        log2.log(_make_event(event_id="e_after"))

        lines = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
        # Last physical line must be the NEW record, cleanly parseable — not
        # the corrupt fragment glued to it.
        last = json.loads(lines[-1])
        assert last["event_id"] == "e_after"
        # And it chains off the recovered tip.
        assert last["prev_hash"] == tip
        # The corrupt fragment is PRESERVED as its own line (append-only
        # forensic evidence), not truncated away.
        assert any(fragment in ln for ln in lines)

    def test_new_record_surfaces_in_recent_after_corrupt_tail(self, tmp_path: Path) -> None:
        self._crash_with_truncated_tail(tmp_path)
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log2.log(_make_event(event_id="visible"))
        # recent() skips the corrupt fragment but MUST surface the new event —
        # proof it isn't orphaned by gluing.
        assert any(e["event_id"] == "visible" for e in log2.recent(limit=10))

    def test_intact_prefix_still_verifies_after_recovery(self, tmp_path: Path) -> None:
        tip, _ = self._crash_with_truncated_tail(tmp_path)
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log2.log(_make_event(event_id="post"))
        total, valid = log2.verify_integrity()
        # The two original records + the post-recovery record all chain and
        # verify; only the single corrupt fragment line is non-valid.
        assert valid == 3
        assert total - valid == 1  # exactly the preserved corrupt fragment

    def test_no_separator_inserted_when_tail_is_clean(self, tmp_path: Path) -> None:
        """Normal appends (file ends with a newline) must NOT gain a blank
        separator line — the boundary fix triggers only on a truncated tail."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="s0"))
        log.log(_make_event(event_id="s1"))
        raw = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
        assert "\n\n" not in raw  # no spurious blank line between records

    def test_ends_without_newline_helper(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        path = tmp_path / "security_events.jsonl"
        # A freshly-created log ends with a newline → no separator needed.
        assert log._ends_without_newline() is False
        # Empty file → no separator needed.
        path.write_text("", encoding="utf-8")
        assert log._ends_without_newline() is False
        # Properly terminated line → no separator needed.
        path.write_text("{}\n", encoding="utf-8")
        assert log._ends_without_newline() is False
        # Truncated tail (no trailing newline) → separator needed.
        path.write_text('{"x": 1', encoding="utf-8")
        assert log._ends_without_newline() is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
class TestHmacKeyAtomicCreation:
    """Key creation is atomic: the key file is only ever visible as the full
    32 bytes, so a crash/partial-write can't leave a short key that the
    load-time length check would then hard-fail on the next boot.
    """

    def test_created_key_is_full_length_and_owner_only(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        key_path = tmp_path / "trust" / "sel_hmac.key"
        assert len(key_path.read_bytes()) == 32
        assert (key_path.stat().st_mode & 0o777) == 0o600

    def test_no_temp_key_files_left_behind(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        leftovers = list((tmp_path / "trust").glob(".sel_hmac_*"))
        assert leftovers == []

    def test_crash_during_create_leaves_no_short_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the write crashes mid-creation, NO key file is published (so the
        next boot regenerates cleanly instead of hard-failing on a short key),
        and the temp file is cleaned up."""
        real_write = os.write

        def _boom(fd, data):  # fail only the key write
            raise OSError("disk full during key write")

        monkeypatch.setattr(os, "write", _boom)
        with pytest.raises(OSError):
            SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(os, "write", real_write)
        # No published key, and no orphaned temp file.
        assert not (tmp_path / "trust" / "sel_hmac.key").exists()
        assert list((tmp_path / "trust").glob(".sel_hmac_*")) == []

    def test_short_write_still_persists_full_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """os.write() returning a SHORT count (e.g. near-full disk) must not
        publish a truncated key — the writer loops until all 32 bytes land."""
        real_write = os.write

        def _short_write(fd, data):
            # Write at most 8 bytes per call, forcing the write-all loop.
            return real_write(fd, bytes(data)[:8])

        monkeypatch.setattr(os, "write", _short_write)
        SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(os, "write", real_write)
        assert len((tmp_path / "trust" / "sel_hmac.key").read_bytes()) == 32

    def test_zero_byte_write_is_treated_as_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistent 0-byte write must raise (not spin forever) and leave no
        published key or temp file."""
        real_write = os.write

        def _zero(fd, data):
            return 0

        monkeypatch.setattr(os, "write", _zero)
        with pytest.raises(OSError):
            SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(os, "write", real_write)
        assert not (tmp_path / "trust" / "sel_hmac.key").exists()
        assert list((tmp_path / "trust").glob(".sel_hmac_*")) == []


class TestHmacKeyTrustDirMigration:
    """The SEL HMAC key lives at trust/sel_hmac.key — OUTSIDE the log's own
    directory — so write access to the log dir does not imply re-signing power.
    A legacy key at <dir>/sel_hmac.key is migrated in atomically with the key
    BYTES unchanged, so pre-existing chains still verify.
    """

    def _reset(self) -> None:
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False

    def test_fresh_install_creates_key_in_trust_dir(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        assert (tmp_path / "trust" / "sel_hmac.key").exists()
        assert not (tmp_path / "sel_hmac.key").exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
    def test_trust_dir_is_owner_only(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        mode = (tmp_path / "trust").stat().st_mode & 0o777
        assert mode == 0o700

    def test_legacy_key_migrated_and_chain_still_verifies(self, tmp_path: Path) -> None:
        """Seed a legacy-layout install (key next to the log, signed entries);
        re-init must relocate the key and keep every existing entry verifying."""
        log1 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log1.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        log1.log_tool_invocation(session_key="s2", tool_name="t2", tool_kind="tool", outcome="ok")
        key_bytes = log1._hmac_key
        # Recreate the LEGACY layout: key beside the log.
        os.replace(tmp_path / "trust" / "sel_hmac.key", tmp_path / "sel_hmac.key")
        self._reset()

        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._hmac_key == key_bytes
        assert (tmp_path / "trust" / "sel_hmac.key").exists()
        assert not (tmp_path / "sel_hmac.key").exists()
        total, valid = log2.verify_integrity()
        assert total == 2
        assert valid == 2

    def test_migrated_key_can_extend_existing_chain(self, tmp_path: Path) -> None:
        log1 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log1.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        os.replace(tmp_path / "trust" / "sel_hmac.key", tmp_path / "sel_hmac.key")
        self._reset()

        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log2.log_tool_invocation(session_key="s2", tool_name="t2", tool_kind="tool", outcome="ok")
        total, valid = log2.verify_integrity()
        assert total == 2
        assert valid == 2

    def test_planted_destination_is_overwritten_by_legacy_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Upgrade-boundary defense: ``trust/`` was not deny-listed before the
        migration release, so a file already at the destination on a legacy
        install could be agent-planted (known bytes = forgeable MACs). The
        deny-list-protected legacy key must WIN and overwrite it."""
        planted_key = b"n" * 32
        legacy_key = b"l" * 32
        (tmp_path / "trust").mkdir()
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(planted_key)
        (tmp_path / "sel_hmac.key").write_bytes(legacy_key)

        with caplog.at_level("WARNING", logger="kiro_crew.sel"):
            log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == legacy_key
        assert (tmp_path / "trust" / "sel_hmac.key").read_bytes() == legacy_key
        # Legacy file consumed by the atomic replace.
        assert not (tmp_path / "sel_hmac.key").exists()
        assert any("replaced by the legacy" in r.message for r in caplog.records)

    def test_short_legacy_file_does_not_destroy_a_usable_migrated_key(self, tmp_path: Path) -> None:
        """A 0-byte legacy file must not replace the key that signed the chain.

        A mixed-binary upgrade window produces exactly this pair: a writer that
        derives its chain lock from the key location opens the legacy path with
        ``O_CREAT`` and leaves a 0-byte file behind once a sibling has relocated
        the real key. Promoting that file destroys the only copy of the signing
        key, and the minimum-length check then fails init on every later boot, so
        the loss is unrecoverable rather than merely wrong.

        Refusing is what makes that unrecoverable outcome impossible: BOTH files
        survive byte-for-byte, so the operator's recovery (remove the stub) is
        always available, and the chain still verifies afterwards.
        """
        log1 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log1.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        real_key = log1._hmac_key
        assert len(real_key) >= 32, "precondition: the migrated key is usable"
        # The artifact an old writer leaves at the legacy path: created, never written.
        (tmp_path / "sel_hmac.key").write_bytes(b"")
        self._reset()

        with pytest.raises(RuntimeError, match="too short to be a key"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

        assert (
            tmp_path / "trust" / "sel_hmac.key"
        ).read_bytes() == real_key, "the 0-byte legacy file was promoted over the real key"
        assert (tmp_path / "sel_hmac.key").read_bytes() == b"", "the legacy stub was rewritten"

        # The operator's recovery: remove the stub the old writer left behind.
        (tmp_path / "sel_hmac.key").unlink()
        self._reset()
        log3 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log3._hmac_key == real_key
        assert log3.verify_integrity() == (1, 1), "the pre-existing chain no longer verifies"

    def test_planted_destination_key_is_not_adopted_over_a_short_legacy_key(
        self, tmp_path: Path
    ) -> None:
        """Length is not trust: a destination key of the right size is refused.

        The pairing is producible by the audited agent itself -- ``trust`` and
        ``sel_hmac.key`` are both sandbox read-write leaves -- so truncating the
        legacy key below the minimum and planting a long-enough destination is an
        ordinary capability, not an extreme combination. Adopting the destination
        on its size alone would sign every later SEL and session-identity MAC
        with bytes the attacker chose, with no recovery path.
        """
        log1 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log1.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        real_key = log1._hmac_key
        self._reset()

        # What the agent can do: truncate the legacy anchor, plant a key it knows.
        (tmp_path / "sel_hmac.key").write_bytes(b"short")
        planted = b"p" * 64
        assert planted != real_key
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(planted)

        with pytest.raises(RuntimeError, match="refusing to adopt"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    def test_a_forged_audit_row_cannot_authenticate_a_planted_key(self, tmp_path: Path) -> None:
        """The refusal must not be resolvable by evidence the agent can write.

        Every file that could vouch for the destination key -- the key itself,
        the legacy key, ``security_events.jsonl`` and ``security_events.d`` -- is
        a sandbox read-WRITE leaf for the audited agent, so a check against any
        of them authenticates the attacker's own evidence. This is the shape that
        makes the audit log the worst possible anchor: the planted key signs a
        row the agent appended, so a log-anchored proof gate would ADOPT it.
        """
        log1 = SecurityEventLog(base_dir=tmp_path, sync=True)
        log1.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        self._reset()

        planted = b"p" * 64
        (tmp_path / "sel_hmac.key").write_bytes(b"short")
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(planted)

        # A row whose entry_hash IS an HMAC under the planted key, appended as the
        # newest record -- exactly what a log-anchored gate would accept as proof.
        forged = {"event_id": "forged", "timestamp": "2026-01-01T00:00:00Z"}
        payload = json.dumps(forged, sort_keys=True).encode()
        forged["entry_hash"] = hmac.new(planted, payload, hashlib.sha256).hexdigest()
        with (tmp_path / "security_events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged) + "\n")

        with pytest.raises(RuntimeError, match="refusing to adopt"):
            SecurityEventLog(base_dir=tmp_path, sync=True)
        assert (
            tmp_path / "trust" / "sel_hmac.key"
        ).read_bytes() == planted, "precondition: the planted key was left in place, not consumed"

    def test_short_legacy_key_with_an_empty_log_fails_closed(self, tmp_path: Path) -> None:
        """The refusal does not depend on there being history to weigh.

        A fresh directory holding only the ambiguous pair is refused for the same
        single reason as one with a full chain, so no branch of this decision can
        be unlocked by controlling how much the log contains.
        """
        (tmp_path / "sel_hmac.key").write_bytes(b"")
        (tmp_path / "trust").mkdir()
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(b"p" * 64)

        with pytest.raises(RuntimeError, match="refusing to adopt"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    @pytest.mark.skipif(
        not sel_mod._PIN_BY_FD_SUPPORTED, reason="platform has no directory descriptors"
    )
    def test_chain_lock_sidecar_is_opened_inside_the_pinned_directory(self, tmp_path: Path) -> None:
        """A ``trust`` replaced after the screen must not move the sidecar.

        The by-name screen and the sidecar's open are separate syscalls, and
        ``trust`` is a read-write leaf for the audited agent, so it can swap the
        directory in that window: both writers' screens then pass while their
        opens land in different directories, and two flocks on two inodes is the
        forked chain the lock exists to prevent. With the open resolved relative
        to the pinned descriptor, the swap cannot redirect it -- the sidecar
        appears under the inode that was screened, not under the new name.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        trust = tmp_path / "trust"
        pinned_identity = (trust.stat().st_dev, trust.stat().st_ino)
        (trust / sel_mod._SEL_LOCK_FILE).unlink(missing_ok=True)

        real_open = os.open
        swapped: list[Path] = []

        def swapping_open(path, flags, *args, **kwargs):
            # Fire once, at the sidecar's own dir-relative open: the pin is
            # already taken, so this is exactly the window the finding named.
            if kwargs.get("dir_fd") is not None and path == sel_mod._SEL_LOCK_FILE and not swapped:
                moved = tmp_path / "trust-pinned"
                trust.rename(moved)
                decoy = tmp_path / "trust"
                decoy.mkdir()
                swapped.append(moved)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", swapping_open):
            with log._chain_lock():
                pass

        assert swapped, "the swap never fired; the sidecar was not opened dir-relative"
        moved = swapped[0]
        assert (moved.stat().st_dev, moved.stat().st_ino) == pinned_identity
        assert (
            moved / sel_mod._SEL_LOCK_FILE
        ).exists(), "the sidecar did not land in the pinned directory"
        assert not (
            tmp_path / "trust" / sel_mod._SEL_LOCK_FILE
        ).exists(), "the swapped-in decoy captured the sidecar"

    @pytest.mark.skipif(
        not sel_mod._PIN_BY_FD_SUPPORTED, reason="platform has no directory descriptors"
    )
    def test_lock_dir_pin_refuses_a_directory_swapped_under_its_name(self, tmp_path: Path) -> None:
        """The pin's identity check is what makes the flag degradation safe."""
        real_dir = tmp_path / "trust"
        real_dir.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        real_lstat = os.lstat

        def swapped_lstat(path, *args, **kwargs):
            # Stands in for a swap landing between the open and the identity
            # read: the name now reports a DIFFERENT real directory.
            if Path(path) == real_dir:
                return real_lstat(other)
            return real_lstat(path, *args, **kwargs)

        with patch.object(os, "lstat", swapped_lstat):
            with pytest.raises(OSError, match="not the directory its name points at"):
                sel_mod._pin_lock_dir(real_dir)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_linked_trust_dir_is_removed_not_followed(self, tmp_path: Path) -> None:
        """A ``trust`` symlink planted before the upgrade must be removed
        (link only, target untouched) so the key is never written through it."""
        legacy_key = b"l" * 32
        (tmp_path / "sel_hmac.key").write_bytes(legacy_key)
        target = tmp_path / "agent-readable"
        target.mkdir()
        (tmp_path / "trust").symlink_to(target)

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == legacy_key
        assert not (tmp_path / "trust").is_symlink()
        # The key landed in the REAL dir; the link target got nothing.
        assert (tmp_path / "trust" / "sel_hmac.key").read_bytes() == legacy_key
        assert list(target.iterdir()) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_linked_key_file_is_removed_not_followed(self, tmp_path: Path) -> None:
        """A ``trust/sel_hmac.key`` symlink must be removed before use so a
        fresh key is never written through (or read via) a planted link."""
        (tmp_path / "trust").mkdir()
        target = tmp_path / "exfil.key"
        target.write_bytes(b"p" * 32)
        (tmp_path / "trust" / "sel_hmac.key").symlink_to(target)

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        key_path = tmp_path / "trust" / "sel_hmac.key"
        assert not key_path.is_symlink()
        # Fresh key minted in place, never the planted target bytes.
        assert log._hmac_key != b"p" * 32
        assert target.read_bytes() == b"p" * 32

    def test_sel_hmac_key_path_reports_trust_location(self, tmp_path: Path) -> None:
        """Dependent protocols (session_pid_sig) resolve the key through the
        accessor, so it must report the resolved trust/ path."""
        SecurityEventLog(base_dir=tmp_path, sync=True)
        assert sel_hmac_key_path() == tmp_path / "trust" / "sel_hmac.key"

    def test_sel_hmac_key_path_default_includes_trust_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without a live singleton the accessor falls back to the same
        trust/ default the singleton would use."""
        self._reset()
        monkeypatch.setattr("kiro_crew.sel._default_dir", lambda: tmp_path)
        assert sel_hmac_key_path() == tmp_path / "trust" / "sel_hmac.key"

    def test_readonly_config_dir_with_legacy_key_still_boots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy install whose config dir cannot gain a trust/ subdir
        (read-only FS) must keep signing with the legacy key — never crash
        SecurityEventLog init before the fallback can run."""
        key = b"k" * 32
        (tmp_path / "sel_hmac.key").write_bytes(key)
        real_mkdir = Path.mkdir

        def _deny_trust_mkdir(self, *args, **kwargs):  # noqa: ANN001
            if self.name == "trust":
                raise PermissionError(30, "Read-only file system", str(self))
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _deny_trust_mkdir)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
        assert log._hmac_key == key
        # Key stayed at (and is reported from) the legacy location.
        assert (tmp_path / "sel_hmac.key").exists()
        assert sel_hmac_key_path() == tmp_path / "sel_hmac.key"

    def test_failed_replace_with_planted_destination_prefers_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed os.replace while the legacy source STILL EXISTS must fall
        back to the legacy key — never adopt a destination file that could
        have been pre-planted (attacker forces the replace to fail, plants
        known bytes at the destination)."""
        legacy_key = b"l" * 32
        planted_key = b"p" * 32
        (tmp_path / "sel_hmac.key").write_bytes(legacy_key)
        (tmp_path / "trust").mkdir()
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(planted_key)
        real_replace = os.replace

        def _failing_replace(src, dst):
            raise PermissionError("simulated forced replace failure")

        monkeypatch.setattr(os, "replace", _failing_replace)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(os, "replace", real_replace)
        assert log._hmac_key == legacy_key
        # The accessor reports the file actually in use (legacy), so
        # session_pid_sig never anchors on the planted destination.
        assert sel_hmac_key_path() == tmp_path / "sel_hmac.key"

    def test_migration_race_lost_uses_already_migrated_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two processes can race the legacy->trust migration: the loser's
        os.replace fails AFTER the winner moved the key. The loser must pick up
        the already-migrated key — never mint a fresh one that forks the
        trust root."""
        key = b"k" * 32
        (tmp_path / "sel_hmac.key").write_bytes(key)
        real_replace = os.replace

        def _racing_replace(src, dst):
            # Simulate the sibling winning the race between our exists() check
            # and our os.replace call: the key is already at the new path and
            # the legacy source is gone.
            real_replace(src, dst)
            raise FileNotFoundError("simulated lost migration race")

        monkeypatch.setattr(os, "replace", _racing_replace)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        monkeypatch.setattr(os, "replace", real_replace)
        assert log._hmac_key == key
        assert sel_hmac_key_path() == tmp_path / "trust" / "sel_hmac.key"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_unremovable_planted_link_falls_back_to_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-only config dir + planted trust link + legacy key: init must
        fall back to the legacy key, never crash and never use the link."""
        legacy_key = b"l" * 32
        (tmp_path / "sel_hmac.key").write_bytes(legacy_key)
        target = tmp_path / "agent-readable"
        target.mkdir()
        (tmp_path / "trust").symlink_to(target)

        def _deny_unlink(path):
            raise PermissionError(30, "Read-only file system", str(path))

        monkeypatch.setattr("kiro_crew.platform_compat.unlink_link_or_junction", _deny_unlink)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == legacy_key
        assert sel_hmac_key_path() == tmp_path / "sel_hmac.key"
        # Nothing was ever written through the planted link.
        assert list(target.iterdir()) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_unremovable_planted_link_never_hosts_the_chain_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The APPEND path must not create the lock sidecar through the link.

        ``test_unremovable_planted_link_falls_back_to_legacy`` covers init only,
        and the chain lock is taken on the first APPEND, so a clean target there
        says nothing about the lock. ``Path.mkdir``'s ``exist_ok`` check is
        ``is_dir()``, which follows the planted link and reports the agent's
        directory as usable, so the ``FileExistsError`` is swallowed and without
        a parent-link refusal the sidecar is created inside storage the audited
        agent controls -- where it can be unlinked and recreated between two
        writers' acquires, giving them flocks on different inodes and forking
        the hash chain permanently.
        """
        legacy_key = b"l" * 32
        (tmp_path / "sel_hmac.key").write_bytes(legacy_key)
        target = tmp_path / "agent-readable"
        target.mkdir()
        (tmp_path / "trust").symlink_to(target)

        def _deny_unlink(path):
            raise PermissionError(30, "Read-only file system", str(path))

        monkeypatch.setattr("kiro_crew.platform_compat.unlink_link_or_junction", _deny_unlink)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == legacy_key

        # The lock falls back to the deny-list-protected legacy key, and must
        # NOT be allowed to create it (a 0-byte key would be promoted over the
        # real one by the migration block).
        assert log._chain_lock_target() == (tmp_path / "sel_hmac.key", False, None)

        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="execute_bash",
            tool_kind="shell",
            outcome="approved",
            resources="ls -la",
        )

        # The append landed...
        assert (tmp_path / "security_events.jsonl").read_text(encoding="utf-8").strip()
        # ...and still nothing was created through the planted link.
        assert list(target.iterdir()) == []
        # The legacy key was locked, never rewritten.
        assert (tmp_path / "sel_hmac.key").read_bytes() == legacy_key

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_symlinked_data_home_still_takes_the_real_chain_lock(self, tmp_path: Path) -> None:
        """An OPERATOR's symlink ABOVE the log dir is layout, not an attack.

        The refusal exists because the audited agent can plant a link at
        ``trust`` itself, which is the one component below the log directory it
        can write. Everything ABOVE the log directory is the operator's own
        layout -- a home reached through a symlink, a crew dir moved to another
        volume -- and the agent cannot plant any of it. Screening those
        ancestors buys no protection and costs the whole log: the refusal routes
        into the legacy-key fallback, and on an install with no legacy key it
        re-raises, so every best-effort audit is dropped and every critical
        operation is denied on a perfectly healthy install. This is not
        hypothetical -- a home directory that is itself a symlink is a normal
        layout on shared build hosts.

        ``_load_or_create_hmac_key`` screens this same ``trust`` directory with
        ``is_link_or_junction`` alone, so leaf-only is also the parity this
        sidecar's protection is asserted against.
        """
        # The operator's layout: the data home is reached through a symlink.
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real)
        base_dir = tmp_path / "link" / "crew"
        base_dir.mkdir()

        trust_dir = base_dir / sel_mod._TRUST_SUBDIR
        assert platform_compat.first_linked_ancestor(trust_dir)
        assert not platform_compat.is_link_or_junction(trust_dir)

        log = SecurityEventLog(base_dir=base_dir, sync=True)

        # The real sidecar is used -- not the legacy-key fallback, and no raise.
        lock_path, may_create, lock_dir_fd = log._chain_lock_target()
        assert (lock_path, may_create) == (
            trust_dir / sel_mod._SEL_LOCK_FILE,
            True,
        )
        if lock_dir_fd is not None:
            os.close(lock_dir_fd)

        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="execute_bash",
            tool_kind="shell",
            outcome="approved",
            resources="ls -la",
        )

        # The append landed and the sidecar is a real file in a real directory.
        assert (base_dir / "security_events.jsonl").read_text(encoding="utf-8").strip()
        assert lock_path.is_file()
        assert not platform_compat.is_link_or_junction(lock_path.parent)

    def test_migrated_short_key_still_hard_fails(self, tmp_path: Path) -> None:
        """Validation applies to the migrated file exactly as to a fresh one."""
        (tmp_path / "sel_hmac.key").write_bytes(b"x" * 8)
        with pytest.raises(RuntimeError, match="too short"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    def test_key_bytes_accessor_returns_the_live_signing_key(self, tmp_path: Path) -> None:
        """The recovery path for the dependent protocol: SEL caches the
        validated bytes at init, so they stay available when the file behind the
        frozen resolved path fails to load."""
        from kiro_crew.sel import _sel_hmac_key_bytes

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert _sel_hmac_key_bytes() == log._hmac_key
        # Still available after the file is gone — that is the whole point.
        (tmp_path / "trust" / "sel_hmac.key").unlink()
        assert _sel_hmac_key_bytes() == log._hmac_key

    def test_key_bytes_accessor_is_none_without_a_live_singleton(self) -> None:
        """The verifying MCP process has no singleton; it must get None rather
        than a partially-constructed instance's attribute."""
        from kiro_crew.sel import _sel_hmac_key_bytes

        self._reset()
        assert _sel_hmac_key_bytes() is None

    def test_key_bytes_accessor_is_none_mid_construction(self) -> None:
        """``__new__`` publishes the instance to ``_instance`` BEFORE ``__init__``
        loads the key, so a concurrent reader can see an instance whose
        ``_hmac_key`` does not exist yet. ``_initialized`` is the barrier that
        makes that window return None instead of raising or yielding garbage."""
        from kiro_crew.sel import SecurityEventLog as _SEL
        from kiro_crew.sel import _sel_hmac_key_bytes

        self._reset()
        try:
            _SEL.__new__(_SEL)  # publishes _instance, leaves _initialized False
            assert _SEL._instance is not None
            assert not getattr(_SEL._instance, "_initialized", False)
            assert _sel_hmac_key_bytes() is None
        finally:
            self._reset()

    def test_key_bytes_accessor_has_exactly_one_production_caller(self) -> None:
        """Handing out raw trust-root bytes is safe only under the file-first
        ordering its ONE caller enforces; a second caller would inherit none of
        it. Pin the caller set rather than trusting the underscore."""
        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        callers = {
            path
            for path in root.rglob("*.py")
            if path.name != "sel.py"
            # encoding is explicit: the default is cp1252 on Windows, which
            # cannot decode the non-ASCII bytes several sources contain.
            and "_sel_hmac_key_bytes" in path.read_text(encoding="utf-8")
        }
        assert callers == {
            root / "session_pid_sig.py"
        }, f"_sel_hmac_key_bytes gained a caller outside session_pid_sig: {callers}"

    def test_concurrent_first_construction_initializes_once(self, tmp_path: Path) -> None:
        """``__new__`` publishes the instance BEFORE ``__init__`` runs, so two
        threads arriving in between both see ``_initialized`` False. Unserialized,
        both run the construction body and each can mint a fresh key — one wins
        on disk while the other signs from different bytes in memory, splitting
        the audit chain from the file every other process resolves.

        Reachable because SEL is now constructed from worker threads (the
        middleware deny audits offload via ``asyncio.to_thread``), where the
        event loop does not serialize callers for free.
        """
        self._reset()
        calls: list[int] = []
        real = SecurityEventLog._load_or_create_hmac_key

        def counting(inst):
            calls.append(1)
            # Widen the window a real race would need, so an unlocked body
            # reliably interleaves instead of passing by luck.
            # flake-ok: pre-existing 50 ms window that widens a construction race -- moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing race-widening sleep moved verbatim
            time.sleep(0.05)
            return real(inst)

        barrier = threading.Barrier(8)

        def build():
            # flake-ok: pre-existing unbounded threading.Barrier rendezvous -- moved verbatim; tracked for a de-flake pass
            barrier.wait()
            SecurityEventLog(base_dir=tmp_path, sync=True)

        with patch.object(SecurityEventLog, "_load_or_create_hmac_key", counting):
            threads = [threading.Thread(target=build) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                # flake-ok: pre-existing unbounded join of the eight constructing threads -- moved verbatim; tracked for a de-flake pass
                t.join()

        assert len(calls) == 1, (
            f"construction body ran {len(calls)} times; concurrent first "
            "denials can mint competing trust-root keys"
        )
        inst = SecurityEventLog._instance
        assert inst is not None and inst._initialized
        assert inst._hmac_key == (tmp_path / "trust" / "sel_hmac.key").read_bytes()
        self._reset()


class TestTrustRootPathReResolution:
    """``sel_hmac_key_path()`` re-resolves per call.

    ``_hmac_key_file`` is decided once at init, and a failed legacy migration
    leaves it on the legacy location; a sibling process that later completes the
    migration deletes the file this process still names. The accessor follows the
    key instead of every dependent protocol growing its own recovery — but only
    to a file whose bytes are the anchor this process already validated, and
    never by moving what the audit chain signs with.
    """

    def _reset(self) -> None:
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False

    def test_resolved_path_is_returned_while_it_loads(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        assert sel_mod.sel_hmac_key_path() == tmp_path / "trust" / "sel_hmac.key"

    def test_relocation_to_the_trust_dir_is_followed(self, tmp_path: Path) -> None:
        """One process keeps the legacy path after a failed migration, then a
        sibling process completes it."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        canonical = tmp_path / "trust" / "sel_hmac.key"
        # Pin the instance to the legacy location the way a failed migration does.
        log._hmac_key_file = tmp_path / "sel_hmac.key"
        assert not (tmp_path / "sel_hmac.key").exists()

        assert sel_mod.sel_hmac_key_path() == canonical

    def test_relocation_to_the_legacy_location_is_followed(self, tmp_path: Path) -> None:
        SecurityEventLog(base_dir=tmp_path, sync=True)
        legacy = tmp_path / "sel_hmac.key"
        os.replace(tmp_path / "trust" / "sel_hmac.key", legacy)

        assert sel_mod.sel_hmac_key_path() == legacy

    def test_a_candidate_with_other_bytes_is_not_adopted(self, tmp_path: Path) -> None:
        """The no-downgrade property. The resolved path vanishing is exactly when
        an actor who can write the trust dir would plant a key of their own;
        handing a dependent protocol that path hands it signing material."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        canonical = tmp_path / "trust" / "sel_hmac.key"
        planted = b"p" * 32
        assert log._hmac_key != planted
        canonical.write_bytes(planted)
        log._hmac_key_file = tmp_path / "sel_hmac.key"

        # Unchanged, so the operator report still names the file that broke and
        # session_pid_sig recovers from the validated in-memory copy instead.
        assert sel_mod.sel_hmac_key_path() == tmp_path / "sel_hmac.key"

    def test_a_short_candidate_is_not_adopted(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        (tmp_path / "trust" / "sel_hmac.key").write_bytes(b"short")
        log._hmac_key_file = tmp_path / "sel_hmac.key"

        assert sel_mod.sel_hmac_key_path() == tmp_path / "sel_hmac.key"

    def test_re_resolution_never_moves_what_the_chain_signs_with(self, tmp_path: Path) -> None:
        """The accessor returns a PATH the signing and verification code never
        reads, so a relocation cannot orphan records already chained."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log_tool_invocation(session_key="s1", tool_name="t1", tool_kind="tool", outcome="ok")
        anchor = log._hmac_key
        legacy = tmp_path / "sel_hmac.key"
        os.replace(tmp_path / "trust" / "sel_hmac.key", legacy)

        assert sel_mod.sel_hmac_key_path() == legacy
        assert log._hmac_key == anchor
        log.log_tool_invocation(session_key="s2", tool_name="t2", tool_kind="tool", outcome="ok")
        total, valid = log.verify_integrity()
        assert (total, valid) == (2, 2)

    def test_no_singleton_resolves_the_trust_default(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(sel_mod, "_default_dir", lambda: tmp_path)
        assert SecurityEventLog._instance is None

        assert sel_mod.sel_hmac_key_path() == tmp_path / "trust" / "sel_hmac.key"

    def test_key_loads_predicate_rejects_a_directory(self, tmp_path: Path) -> None:
        d = tmp_path / "sel_hmac.key"
        d.mkdir()
        assert sel_mod._trust_root_key_loads(d) is False

    @pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO semantics")
    def test_key_loads_predicate_rejects_a_fifo(self, tmp_path: Path) -> None:
        """Asserted on the predicate rather than through the accessor on purpose:
        without the ``S_ISREG`` guard the failure mode is an unbounded in-kernel
        block on open, which would hang CI instead of failing it."""
        fifo = tmp_path / "sel_hmac.key"
        os.mkfifo(fifo)
        assert sel_mod._trust_root_key_loads(fifo) is False
