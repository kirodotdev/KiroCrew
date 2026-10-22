"""Tests for kiro_crew.sel — Security Event Log.

This module keeps the core contract: HMAC key management, event logging, integrity
verification, ``recent()``, ``prune()``, the forward callback, thread and cross-process
safety, source inference, the singleton and ``_read_last_hash``. Its topic modules are
``test_sel_edge_cases.py`` (edge cases and the async and critical write paths),
``test_sel_chain_hardening.py`` (HMAC key and corrupt-tail hardening),
``test_sel_rotation.py`` (size rotation and what races it), ``test_sel_segment_files.py``
(segment files and the read path) and ``test_sel_redaction.py`` (metadata redaction and
control-character escaping). The fixtures and helpers they share live in
``sel_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from sel_helpers import (  # noqa: F401 -- fixtures are requested by name
    _make_event,
    log_fixture,
    reset_singleton,
    sel_dir_fixture,
)

import kiro_crew.sel as sel_mod
from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew import sel as kiro_crew_sel
from kiro_crew.sel import SecurityEvent, SecurityEventLog, _infer_source, sel
from kiro_crew.testing.wait import async_wait_until, wait_until


class TestHmacKeyManagement:
    def test_creates_key_file_on_first_init(self, sel_dir):
        SecurityEventLog(base_dir=sel_dir, sync=True)
        key_path = sel_dir / "trust" / "sel_hmac.key"
        assert key_path.exists()
        assert len(key_path.read_bytes()) == 32

    def test_key_file_permissions(self, sel_dir, monkeypatch):
        if sys.platform == "win32":
            # No POSIX mode bits on Windows; the mode bits are inert there and
            # 0o600 is unobservable. The real owner-only guarantee is the DACL
            # platform_compat.restrict_to_owner applies to sel_hmac.key (the SEL
            # trust-root key) at creation — exactly as sel.py's write site does
            # with restrict_to_owner=True. Assert that applied DACL directly,
            # the same way test_workflows_run_identity.py and
            # test_config_rmw_preserves_settings.py do. The pin that this key is
            # locked down (lockdown precedes the key bytes) lives in
            # test_key_lockdown_precedes_content.
            #
            # conftest's autouse _windows_restrict_to_owner_stub no-ops the
            # lockdown module-wide for hermeticity, which would leave the key
            # under its inherited DACL and make this assertion vacuous (it would
            # observe no DACL at all). Opt out for THIS test only — the same
            # thing test_workflows_run_identity's native-Windows test does — by
            # restoring the real file-shape lockdown so the key is actually
            # locked down before we read its descriptor. _apply_owner_only_dacl
            # is the real engine both restrict_to_owner and restrict_dir_to_owner
            # route through and is itself never stubbed.
            from kiro_crew import windows_acl

            monkeypatch.setattr(
                platform_compat,
                "restrict_to_owner",
                lambda p: platform_compat._apply_owner_only_dacl(p, inherit=False),
            )
            SecurityEventLog(base_dir=sel_dir, sync=True)
            key_path = sel_dir / "trust" / "sel_hmac.key"
            sid = platform_compat.current_user_sid()
            assert sid
            # The SID set restrict_to_owner applies for a FILE (inherit=False):
            # OWNER RIGHTS (S-1-3-4) plus the current user — see
            # platform_compat._apply_owner_only_dacl.
            assert windows_acl.owner_only_dacl_matches(
                key_path, inherit=False, sids=("S-1-3-4", sid)
            )
            return
        SecurityEventLog(base_dir=sel_dir, sync=True)
        key_path = sel_dir / "trust" / "sel_hmac.key"
        mode = oct(key_path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_reuses_existing_key(self, sel_dir):
        log1 = SecurityEventLog(base_dir=sel_dir, sync=True)
        key1 = log1._hmac_key
        SecurityEventLog._instance = None
        log2 = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log2._hmac_key == key1


class TestEventLogging:
    def test_log_creates_file(self, log, sel_dir):
        event = SecurityEvent(
            event_id="abc123",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="execute_bash",
        )
        log.log(event)
        sel_file = sel_dir / "security_events.jsonl"
        assert sel_file.exists()
        lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

    def test_log_writes_valid_json(self, log, sel_dir):
        event = SecurityEvent(
            event_id="test1",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="cli_chat",
            agent="kirocrew",
            source="cli",
            operation="fs_write",
        )
        log.log(event)
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert data["event_id"] == "test1"
        assert data["operation"] == "fs_write"
        assert data["entry_hash"] != ""
        assert data["prev_hash"] == ""

    def test_log_chains_hashes(self, log, sel_dir):
        for i in range(3):
            log.log(SecurityEvent(
                event_id=f"evt{i}",
                timestamp="2026-01-01T00:00:00+00:00",
                event_type="tool_invocation",
                caller_identity="dashboard:slot0",
                agent="kirocrew",
                source="dashboard",
                operation=f"op{i}",
            ))
        sel_file = sel_dir / "security_events.jsonl"
        lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        entries = [json.loads(line) for line in lines]
        assert entries[0]["prev_hash"] == ""
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]
        assert entries[2]["prev_hash"] == entries[1]["entry_hash"]

    def test_log_tool_invocation_convenience(self, log, sel_dir):
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="execute_bash",
            tool_kind="shell",
            outcome="approved",
            resources="ls -la",
        )
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert data["event_type"] == "tool_invocation"
        assert data["operation"] == "execute_bash"
        assert data["outcome"] == "approved"
        assert data["source"] == "dashboard"

    def test_log_api_access_convenience(self, log, sel_dir):
        log.log_api_access(
            caller="token:abc",
            operation="GET /api/sessions",
            outcome="allowed",
        )
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert data["event_type"] == "api_access"
        assert data["source"] == "dashboard"

    def test_log_api_access_redacts_and_clips_outcome(self, log, sel_dir):
        """``outcome`` is scrubbed like ``resources``/``error``, not forwarded raw.

        It reads as a constrained vocabulary, and is one for in-tree callers, but
        an installed app reaches this helper through ``ctx.audit`` -- so the value
        can be caller text. This log is append-only and served over
        ``/api/sel/events``, so a credential landing here cannot be taken back.
        """
        log.log_api_access(
            caller="app:doc-store",
            operation="doc-store.publish",
            outcome="failed for AKIAIOSFODNN7EXAMPLE " + "x" * 900,
        )
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert "AKIAIOSFODNN7EXAMPLE" not in data["outcome"]
        assert len(data["outcome"]) <= 500

    @pytest.mark.parametrize(
        "outcome", ["ok", "allowed", "denied", "completed", "rejected", "failed"]
    )
    def test_log_api_access_leaves_a_real_outcome_unaltered(self, log, sel_dir, outcome):
        # The scrub above must be the identity function on every spelling in-tree
        # code writes, or it would rewrite the meaning of existing audit rows.
        log.log_api_access(caller="token:abc", operation="GET /api/x", outcome=outcome)
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert data["outcome"] == outcome

    def test_resources_truncated(self, log, sel_dir):
        long_resource = "x" * 1000
        log.log_tool_invocation(
            session_key="cli_chat",
            tool_name="test",
            outcome="completed",
            resources=long_resource,
        )
        sel_file = sel_dir / "security_events.jsonl"
        data = json.loads(sel_file.read_text(encoding="utf-8").strip())
        assert len(data["resources"]) == 500


class TestVerifyIntegrity:
    def test_empty_log(self, log):
        total, valid = log.verify_integrity()
        assert total == 0
        assert valid == 0

    def test_valid_chain(self, log):
        for i in range(5):
            log.log(SecurityEvent(
                event_id=f"evt{i}",
                timestamp="2026-01-01T00:00:00+00:00",
                event_type="tool_invocation",
                caller_identity="dashboard:slot0",
                agent="kirocrew",
                source="dashboard",
                operation=f"op{i}",
            ))
        total, valid = log.verify_integrity()
        assert total == 5
        assert valid == 5

    def test_detects_tampered_entry(self, log, sel_dir):
        log.log(SecurityEvent(
            event_id="evt0",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="op0",
        ))
        log.log(SecurityEvent(
            event_id="evt1",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="op1",
        ))
        # Tamper with first entry
        sel_file = sel_dir / "security_events.jsonl"
        lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        entry["operation"] = "TAMPERED"
        lines[0] = json.dumps(entry)
        sel_file.write_text("\n".join(lines) + "\n")

        total, valid = log.verify_integrity()
        assert total == 2
        # Entry 0's self-hash is still valid; entry 1's chain breaks because prev_hash mismatches
        assert valid < 2


class TestRecent:
    def test_returns_most_recent(self, log):
        for i in range(10):
            log.log(SecurityEvent(
                event_id=f"evt{i}",
                timestamp=f"2026-01-01T00:0{i}:00+00:00",
                event_type="tool_invocation",
                caller_identity="dashboard:slot0",
                agent="kirocrew",
                source="dashboard",
                operation=f"op{i}",
            ))
        results = log.recent(limit=3)
        assert len(results) == 3
        assert results[0]["event_id"] == "evt9"
        assert results[2]["event_id"] == "evt7"

    def test_empty_log_returns_empty(self, log):
        assert log.recent() == []


class TestPrune:
    def test_removes_old_entries(self, log, sel_dir):
        # Write an entry with an old timestamp
        log.log(SecurityEvent(
            event_id="old",
            timestamp="2020-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="old_op",
        ))
        log.log(SecurityEvent(
            event_id="new",
            timestamp="2099-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="new_op",
        ))
        removed = log.prune(keep_days=365)
        assert removed == 1
        sel_file = sel_dir / "security_events.jsonl"
        remaining = sel_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(remaining) == 1
        assert "new_op" in remaining[0]

    def test_prune_empty_log(self, log):
        assert log.prune() == 0


class TestForwardCallback:
    def test_callback_called_on_log(self, log):
        received = []
        log.set_forward_callback(lambda evt: received.append(evt))
        log.log(SecurityEvent(
            event_id="cb1",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="test_op",
        ))
        assert len(received) == 1
        assert received[0]["event_id"] == "cb1"

    def test_callback_failure_does_not_break_logging(self, log, sel_dir):
        def bad_callback(evt):
            raise RuntimeError("callback exploded")

        log.set_forward_callback(bad_callback)
        log.log(SecurityEvent(
            event_id="cb2",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="test_op",
        ))
        # Event should still be written despite callback failure
        sel_file = sel_dir / "security_events.jsonl"
        assert sel_file.exists()
        assert "cb2" in sel_file.read_text(encoding="utf-8")


class TestThreadSafety:
    def test_concurrent_writes(self, log, sel_dir):
        """Multiple threads writing simultaneously should not corrupt the log."""
        def write_events(start_id, count):
            for i in range(count):
                log.log(SecurityEvent(
                    event_id=f"t{start_id}_{i}",
                    timestamp="2026-01-01T00:00:00+00:00",
                    event_type="tool_invocation",
                    caller_identity="dashboard:slot0",
                    agent="kirocrew",
                    source="dashboard",
                    operation=f"op{start_id}_{i}",
                ))

        threads = [threading.Thread(target=write_events, args=(t, 10)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sel_file = sel_dir / "security_events.jsonl"
        lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 40
        # All lines should be valid JSON
        for line in lines:
            json.loads(line)


class TestCrossProcessSafety:
    """Two PROCESSES sharing one log must produce one contiguous HMAC chain.

    TestThreadSafety above only exercises threads inside one interpreter, which
    a threading.Lock already orders. The gateway and each managed MCP server are
    separate processes sharing this file, and each holds its own singleton with
    its own cached chain tip — the case a thread lock cannot cover.
    """

    _CHILD = '''
import sys
import time
from pathlib import Path

from kiro_crew.sel import SecurityEvent, SecurityEventLog

sel_dir, sync_dir, tag, count = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
log = SecurityEventLog(base_dir=sel_dir, sync=True)

# Announce readiness, then wait for the parent's start signal, so both children
# hold a chain tip they read BEFORE either has written.
(sync_dir / ("ready-" + tag)).write_text("1", encoding="utf-8")
deadline = time.monotonic() + 60
while not (sync_dir / "go").exists():
    if time.monotonic() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.005)

for i in range(count):
    log.log(SecurityEvent(
        event_id=tag + "-" + str(i),
        timestamp="2026-01-01T00:00:00+00:00",
        event_type="tool_invocation",
        caller_identity="dashboard:" + tag,
        agent="kirocrew",
        source="dashboard",
        operation="op-" + tag + "-" + str(i),
    ))
log.flush()
'''

    def test_concurrent_processes_keep_one_unbroken_chain(self, tmp_path):
        sel_dir = tmp_path / "sel"
        sync_dir = tmp_path / "sync"
        sync_dir.mkdir()
        events_per_child = 12
        tags = ("alpha", "beta")

        # Materialize the HMAC key up front so the children share one key: two
        # processes racing to create it would sign with different keys, which
        # would fail this test for a reason that is not the chain ordering.
        SecurityEventLog(base_dir=sel_dir, sync=True)
        assert (sel_dir / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._HMAC_KEY_FILE).exists()

        child = tmp_path / "sel_child.py"
        child.write_text(self._CHILD, encoding="utf-8")

        env = dict(os.environ)
        # parents[1] is the src/ root holding kiro_crew, so the children import
        # the same tree as this test rather than any installed copy.
        env["PYTHONPATH"] = str(Path(kiro_crew_sel.__file__).parents[1])

        procs = [
            subprocess.Popen(
                [sys.executable, str(child), str(sel_dir), str(sync_dir), tag, str(events_per_child)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                cwd=tmp_path,
            )
            for tag in tags
        ]
        try:

            def _all_ready() -> bool:
                for proc in procs:
                    assert proc.poll() is None, f"child exited early: {proc.communicate()}"
                return all((sync_dir / f"ready-{tag}").exists() for tag in tags)

            wait_until(
                _all_ready,
                timeout=60,
                describe=lambda: (
                    "children never became ready: "
                    f"{sorted(p.name for p in sync_dir.glob('ready-*'))}"
                ),
            )
            (sync_dir / "go").write_text("1", encoding="utf-8")

            for proc in procs:
                out, err = proc.communicate(timeout=60)
                assert proc.returncode == 0, f"child failed: {out}\n{err}"
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    # Reap the killed child: without the wait() the zombie
                    # lingers for the rest of the test run and the ResourceWarning
                    # from Popen.__del__ can fail an unrelated -W error test.
                    proc.wait()

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        verifier = SecurityEventLog(base_dir=sel_dir, sync=True)
        total, valid = verifier.verify_integrity()

        expected = len(tags) * events_per_child
        assert total == expected, f"expected {expected} entries on disk, found {total}"
        # Pre-fix, each process chains every entry off the tip it cached at
        # construction, so the two lineages fork and verify_integrity reports
        # the breaks: valid < total.
        assert valid == total, f"HMAC chain broken: only {valid}/{total} entries verify"

    def test_lock_sidecar_lives_in_the_protected_trust_dir(self, tmp_path):
        """The sidecar must sit where the agent cannot reach it.

        A sibling of security_events.jsonl is NOT covered: the sensitive-path
        floor lists that log by exact leaf name. An agent able to unlink the
        sidecar mid-hold leaves two writers holding locks on different inodes —
        the fork this serialization prevents — and one able to hold it wedges
        every writer.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="lockloc-1"))

        expected = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE
        assert expected.exists(), "lock sidecar was not created under the trust dir"
        assert not list(tmp_path.glob("*.lock")), "a lock file sits beside the log"

    def test_appends_survive_when_the_trust_dir_is_uncreatable(self, tmp_path):
        """A legacy install that cannot create trust/ must keep auditing.

        The sidecar's directory is uncreatable here, so the chain lock falls
        back to the deny-list-protected legacy key file instead of failing every
        acquire, which would drop every best-effort audit and deny every
        critical action on an install that is otherwise signing fine. What
        admits the fallback is that this process RESOLVED its key at the legacy
        location, an install-wide fact settled once at init -- not the legacy
        file's mere existence, which the audited agent can arrange on a healthy
        install (see
        ``test_a_planted_legacy_key_does_not_arm_the_fallback_on_a_healthy_install``).
        """
        legacy = tmp_path / kiro_crew_sel._HMAC_KEY_FILE
        # Windows' CRT text mode treats a trailing 0x1A as DOS EOF. Keep this
        # input deterministic so opening the binary key as a lock can never
        # regress to text mode and silently truncate its final byte.
        legacy.write_bytes(b"k" * 63 + b"\x1a")
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR

        real_mkdir = Path.mkdir

        def uncreatable_trust(self, *args, **kwargs):
            if self == trust:
                raise PermissionError("read-only config dir")
            return real_mkdir(self, *args, **kwargs)

        with patch.object(Path, "mkdir", uncreatable_trust):
            log = SecurityEventLog(base_dir=tmp_path, sync=True)
            assert log._hmac_key_file == legacy, "precondition: fallback not taken"
            assert log._chain_lock_target() == (legacy, False, None), (
                "the fallback must lock the existing legacy key, never create it, "
                "and carry no directory pin (its parent is the operator's log dir)"
            )
            log.log(_make_event(event_id="legacy-lock-crit"), critical=True)
            log.log(_make_event(event_id="legacy-lock-soft"))

        body = log._path.read_text(encoding="utf-8")
        assert "legacy-lock-crit" in body and "legacy-lock-soft" in body
        total, valid = log.verify_integrity()
        assert (total, valid) == (2, 2)
        # The key bytes are untouched: the lock fd is never written through.
        assert legacy.read_bytes() == log._hmac_key

    def test_lock_path_does_not_vary_with_the_keys_migration_state(self, tmp_path):
        """Two processes on one log directory must lock the SAME inode.

        The key's location differs BETWEEN processes mid-migration: one whose
        migration failed keeps signing from the legacy path while a sibling that
        completed it reads the relocated one. A lock path derived from the key
        hands those two writers locks on different inodes, and both then append
        to one log unserialized -- the chain fork this serialization exists to
        prevent. The sidecar's path is a function of the log directory alone, so
        both states yield one value.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        migrated = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._HMAC_KEY_FILE
        legacy = tmp_path / kiro_crew_sel._HMAC_KEY_FILE
        sidecar = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE

        log._hmac_key_file = migrated
        as_migrated = log._chain_lock_path()
        log._hmac_key_file = legacy
        as_legacy = log._chain_lock_path()

        assert (
            as_migrated == as_legacy
        ), "the chain lock path diverges between two processes on one log dir"
        assert as_legacy == sidecar
        assert as_legacy != legacy, "the lock is taken on the HMAC key itself"

    def test_the_fallback_never_creates_a_file_at_the_legacy_key_path(self, tmp_path):
        """No acquire may leave a 0-byte file where the HMAC key belongs.

        That artifact is what destroys the key: the migration block promotes a
        legacy file over the destination, so a 0-byte one created here replaces
        the key that signed every existing record, and the minimum-length check
        then fails init on every later boot. With the sidecar's directory gone
        and no legacy key to fall back to, the acquire must fail instead of
        creating one.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="nocreate-1"))
        legacy = tmp_path / kiro_crew_sel._HMAC_KEY_FILE
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR
        assert not legacy.exists(), "precondition: the key migrated out of the log dir"

        real_mkdir = Path.mkdir

        def uncreatable_trust(self, *args, **kwargs):
            if self == trust:
                raise PermissionError("read-only config dir")
            return real_mkdir(self, *args, **kwargs)

        with patch.object(Path, "mkdir", uncreatable_trust):
            with pytest.raises(OSError):
                log._chain_lock_target()

        assert not legacy.exists(), "an empty file was created at the legacy key path"

    def test_a_planted_legacy_key_does_not_arm_the_fallback_on_a_healthy_install(
        self, tmp_path
    ):
        """The agent must not be able to CREATE the condition that switches targets.

        ``sel_hmac.key`` is a sandbox read-write leaf (``sandbox_leaves.py``'s
        ``_CREW_SANDBOX_VISIBLE_LEAVES``, alongside ``trust``), so the audited
        agent can drop a file at the legacy key path on an install whose trust
        dir is perfectly healthy -- and the lock path never reads its bytes, so
        any content is enough. Were the fallback gated on that file EXISTING, the
        agent could arm it at will and then refuse one writer's acquire, leaving
        that writer on the legacy key while its sibling held the sidecar: two
        flocks on two inodes, a permanently forked chain, and
        ``verify_integrity`` reporting honest later records as tampered.

        This process signs from ``trust/``, so the refusal must fail CLOSED.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="planted-1"))
        legacy = tmp_path / kiro_crew_sel._HMAC_KEY_FILE
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR
        assert log._hmac_key_file == trust / kiro_crew_sel._HMAC_KEY_FILE, (
            "precondition: this process signs from the trust dir"
        )

        # The agent plants a long-enough file at the legacy key path.
        legacy.write_bytes(b"p" * 64)

        real_mkdir = Path.mkdir

        def uncreatable_trust(self, *args, **kwargs):
            if self == trust:
                raise PermissionError("agent-triggered refusal")
            return real_mkdir(self, *args, **kwargs)

        with patch.object(Path, "mkdir", uncreatable_trust):
            with pytest.raises(OSError):
                log._chain_lock_target()

        # The planted file was neither locked nor rewritten.
        assert legacy.read_bytes() == b"p" * 64

    def test_a_mid_acquire_directory_swap_fails_closed_instead_of_switching_targets(
        self, tmp_path
    ):
        """A per-acquire refusal may never select a different lock target.

        The two screens before the pin are properties of the filesystem, so
        every writer on the install reads them alike. ``_pin_lock_dir`` is the
        opposite by construction: it raises exactly when it catches ``trust``
        being REPLACED mid-acquire, which the audited agent triggers at will and
        which can therefore be true for one writer and false for the next.
        Routing it into the legacy fallback would hand those two writers
        different inodes on a healthy install -- the fork this serialization
        exists to prevent -- so a detected swap has one safe answer: fail.

        Pinned here with a legacy key present AND this process signing from it,
        the most permissive state the fallback has, to show the pin's refusal is
        not admitted by either condition.
        """
        legacy = tmp_path / kiro_crew_sel._HMAC_KEY_FILE
        legacy.write_bytes(b"k" * 64)
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR

        real_mkdir = Path.mkdir

        def uncreatable_trust(self, *args, **kwargs):
            if self == trust:
                raise PermissionError("read-only config dir")
            return real_mkdir(self, *args, **kwargs)

        with patch.object(Path, "mkdir", uncreatable_trust):
            log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key_file == legacy, "precondition: signing at the legacy path"
        # The static screens now pass, so only the pin can refuse.
        healthy_path, _, healthy_fd = log._chain_lock_target()
        if healthy_fd is not None:
            os.close(healthy_fd)
        assert healthy_path == trust / kiro_crew_sel._SEL_LOCK_FILE, (
            "precondition: the healthy path is reached once trust/ is creatable"
        )

        def swapped(_path):
            raise OSError("directory is not the directory its name points at")

        with patch.object(sel_mod, "_pin_lock_dir", swapped):
            with pytest.raises(OSError):
                log._chain_lock_target()

        assert legacy.read_bytes() == b"k" * 64, "the legacy key was locked or rewritten"

    def test_a_byte_range_lock_excludes_on_a_zero_length_file(self, tmp_path):
        """The premise the empty lock sidecar rests on.

        The chain-lock sidecar is never written through, so it can be zero
        length when a writer locks it. A byte-range lock must still cover byte 0
        of an empty file and exclude a second descriptor; if a platform ever
        stops honouring that, SEL writers would silently stop serializing, so it
        is asserted here rather than assumed.
        """
        lock_path = tmp_path / "empty.lock"
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        contender = os.open(lock_path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            assert os.fstat(holder).st_size == 0, "precondition: the file is empty"
            assert platform_compat.try_acquire_lock(holder, exclusive=True)
            assert not platform_compat.try_acquire_lock(contender, exclusive=True)
            platform_compat.release_lock(holder)
        finally:
            os.close(contender)
            os.close(holder)

    def test_the_chain_lock_sidecar_is_never_written_through(self, tmp_path):
        """No byte reaches the sidecar, so no write can race an acquire.

        A Windows byte-range lock is mandatory: while a writer holds byte 0, any
        other descriptor writing that byte gets ``EACCES``. A sidecar write on
        the way to the lock therefore fails against a sibling that acquired
        first, and ``_flush_batch`` turns that into a dropped audit record.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="sidecar-1"))
        log.log(_make_event(event_id="sidecar-2"))
        sidecar = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE
        assert sidecar.exists(), "precondition: the chain lock was taken on the sidecar"
        assert sidecar.stat().st_size == 0, "a byte was written on the way to the lock"

    def test_a_foreign_sidecar_holder_does_not_cost_a_best_effort_event(self, tmp_path):
        """A writer that must WAIT for the sidecar still lands its record.

        The sidecar already exists and is empty (its creator took the lock
        without writing it), and another holder has byte 0. A writer arriving
        now must wait for the lock and then append, not fail on the way to it.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        sidecar = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE
        sidecar.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        holder = os.open(sidecar, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
        release = threading.Event()
        logged = threading.Event()
        try:
            assert os.fstat(holder).st_size == 0, "precondition: the sidecar is empty"
            assert platform_compat.try_acquire_lock(holder, exclusive=True)

            def _write():
                log.log(_make_event(event_id="waited-for-the-lock"))
                logged.set()

            writer = threading.Thread(target=_write)
            writer.start()
            # Give the writer time to reach the lock and block on it. On the
            # unfixed code it instead fails on the sidecar write and returns at
            # once, which is what this wait distinguishes.
            early = logged.wait(timeout=0.5)
            release.set()
            platform_compat.release_lock(holder)
            writer.join(timeout=30)
            assert not writer.is_alive(), "the writer never finished"
        finally:
            release.set()
            os.close(holder)
        body = log._path.read_text(encoding="utf-8") if log._path.exists() else ""
        assert "waited-for-the-lock" in body, (
            "the record was dropped while another descriptor held the sidecar "
            f"(writer returned before the lock was released: {early})"
        )

    @pytest.mark.asyncio
    async def test_loop_critical_audit_does_not_wait_for_a_blocked_writer_drain(self):
        """``flush()``'s backlog wait must never run on the event-loop thread.

        With the background writer parked on another process's chain lock and
        events pending, a loop-side critical audit that drains first sits out
        ``flush()``'s full timeout ON THE LOOP -- the same cross-process wait
        the single-shot acquire keeps off the loop, re-imported one level up
        through ``_pending``. It must reach its fail-closed gate immediately
        instead; the wait could not have helped anyway, because the inline
        append fails closed at that gate while the sibling holds the lock.

        Uses the SESSION-scoped SEL default directory, not a per-test tmp dir:
        this test needs the ASYNC writer, and a daemon writer bound to a
        per-test directory outlives its test and re-creates the deleted path on
        its next flush (see the rootdir ``_isolate_sel_default_dir`` fixture).
        """
        log = SecurityEventLog()  # async writer in the session-scoped dir
        log.log(_make_event(event_id="drain-preheat"))
        log.flush()
        lock_path = log._dir / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Stand in for another process holding the chain lock (e.g. prune).
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(holder, exclusive=True)
        try:
            # Queue a normal event; the writer dequeues it and blocks on the
            # chain lock, leaving _pending nonzero either way.
            log.log(_make_event(event_id="drain-queued"))
            await asyncio.sleep(0.3)

            started = time.monotonic()
            with pytest.raises(OSError):
                log.log(_make_event(event_id="drain-critical"), critical=True)
            elapsed = time.monotonic() - started
            assert elapsed < 2.0, (
                f"loop-side critical audit waited {elapsed:.2f}s on the writer's "
                "backlog (flush timeout is 5s); it must fail closed immediately"
            )
        finally:
            platform_compat.release_lock(holder)
            os.close(holder)
        # Once the holder releases, the queued event drains normally.
        log.flush()
        assert "drain-queued" in log._path.read_text(encoding="utf-8")
        # Stop THIS test's writer deterministically. The session directory is
        # shared by every later test in this worker, and flock treats each
        # instance's fd as its own holder -- so a leftover live writer here can
        # hold the chain lock at the very instant a LATER test's loop-side
        # critical audit takes its single shot, denying it spuriously
        # (measured: the issue-radar trust re-establishment tests). The writer
        # loop exits on its None sentinel.
        log._queue.put(None)
        writer = log._writer
        if writer is not None:
            writer.join(timeout=10)
            assert not writer.is_alive(), "sel-writer did not stop"

    @pytest.mark.asyncio
    async def test_loop_critical_audit_joins_its_own_processes_hold(self, tmp_path):
        """Own-process contention must JOIN the hold, not fail closed.

        flock is per open file description, so a second fd in the same process
        reads as a foreign writer -- and the loop-side single shot would then
        deny a critical action because our own background writer was flushing a
        batch at the same instant (measured: the fingerprint read's terminal
        audit racing the writer flushing the same call's earlier best-effort
        event). The flock excludes OTHER processes; threads of this process are
        serialized by ``_lock``, so joining is safe, and the joiner's wait is
        bounded local append work.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="join-preheat"))

        entered = threading.Event()
        release = threading.Event()

        def holder():
            with log._chain_lock():  # this process's own append-kind hold
                entered.set()
                release.wait(10)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            # Pre-fix: single-shot flock attempt on a second fd fails closed.
            # The join waits its turn at the hold's gate, so release the holder
            # on a short timer and assert the critical write went through
            # promptly rather than being refused.
            threading.Timer(0.3, release.set).start()
            started = time.monotonic()
            log.log(_make_event(event_id="join-critical"), critical=True)
            assert time.monotonic() - started < 5.0
        finally:
            release.set()
            t.join(timeout=10)
        body = log._path.read_text(encoding="utf-8")
        assert "join-critical" in body
        total, valid = log.verify_integrity()
        assert valid == total

    @pytest.mark.asyncio
    async def test_loop_critical_audit_does_not_join_its_own_prune(self, tmp_path):
        """prune's hold spans a whole streaming rewrite -- joining it from the
        loop is the stall the single-shot gate exists to prevent, so that one
        own-process hold is refused, exactly like a foreign holder."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="prune-hold-preheat"))

        entered = threading.Event()
        release = threading.Event()

        def holder():
            with log._chain_lock(kind="prune"):
                entered.set()
                release.wait(10)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            with pytest.raises(OSError):
                log.log(_make_event(event_id="prune-hold-critical"), critical=True)
        finally:
            release.set()
            t.join(timeout=10)
        assert "prune-hold-critical" not in log._path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_a_joining_prune_is_visible_to_loop_side_callers(self, tmp_path):
        """A prune that JOINS an append-kind hold must promote the hold's kind.

        Without the promotion, the shared kind stays "append" and a loop-side
        critical audit arriving mid-rewrite joins and waits behind the whole
        streaming rewrite -- the exact stall the prune-kind refusal exists for.
        The promotion happens when the prune joins (before it waits its turn at
        the hold's gate), so it is observable while the append holder is still
        inside.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="promote-preheat"))
        key = str(log._chain_lock_path())

        entered = threading.Event()
        release = threading.Event()

        def append_holder():
            with log._chain_lock():
                entered.set()
                release.wait(10)

        def prune_joiner():
            entered.wait(5)
            with log._chain_lock(kind="prune"):
                pass

        t1 = threading.Thread(target=append_holder, daemon=True)
        t2 = threading.Thread(target=prune_joiner, daemon=True)
        t1.start()
        t2.start()
        try:
            assert entered.wait(5)

            def _hold_kind() -> str | None:
                with kiro_crew_sel._CHAIN_HOLDS_MUTEX:
                    hold = kiro_crew_sel._CHAIN_HOLDS.get(key)
                return None if hold is None else hold.kind

            await async_wait_until(
                lambda: _hold_kind() == "prune",
                timeout=5,
                describe=lambda: f"prune join never promoted the hold's kind: {_hold_kind()!r}",
            )
            # Pre-fix: the hold still reads "append", so this JOINS -- and in
            # production would wait behind the streaming rewrite.
            with pytest.raises(OSError):
                log.log(_make_event(event_id="promote-critical"), critical=True)
        finally:
            release.set()
            t1.join(timeout=10)
            t2.join(timeout=10)
        assert "promote-critical" not in log._path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_rotation_inside_an_append_hold_refuses_loop_side_joins(self, tmp_path):
        """The rotation step inside an append hold must not be joinable from the loop.

        The writer's hold is append-kind, but while it runs rotation's scan +
        rename + retention sweep, a joining loop-side critical audit would
        block on ``_lock`` for that whole step. The hold is relabeled for
        exactly the step's duration, and joins work again afterwards.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="rot-preheat"))

        in_rotation = threading.Event()
        leave_rotation = threading.Event()
        after_rotation = threading.Event()
        release = threading.Event()

        def holder():
            with log._chain_lock():  # the writer's ordinary append-kind hold
                with log._chain_hold_relabel("rotation"):
                    in_rotation.set()
                    leave_rotation.wait(10)
                after_rotation.set()
                release.wait(10)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert in_rotation.wait(5)
            # During the rotation step: fail closed, do not join.
            with pytest.raises(OSError):
                log.log(_make_event(event_id="rot-critical"), critical=True)
            leave_rotation.set()
            assert after_rotation.wait(5)
            # After the step the hold reads "append" again: the join works.
            # It waits its turn at the gate, so release the holder on a short
            # timer and assert the write went through promptly.
            threading.Timer(0.3, release.set).start()
            started = time.monotonic()
            log.log(_make_event(event_id="rot-after"), critical=True)
            assert time.monotonic() - started < 5.0
        finally:
            leave_rotation.set()
            release.set()
            t.join(timeout=10)
        body = log._path.read_text(encoding="utf-8")
        assert "rot-critical" not in body
        assert "rot-after" in body

    @pytest.mark.asyncio
    async def test_loop_critical_audit_joins_a_sibling_instances_hold(self, tmp_path):
        """Two instances on one directory are still ONE process.

        flock is per open file description, so a second SecurityEventLog
        instance (test suites churn the singleton) contending on its own fd
        reads its own process as a foreign writer -- the loop-side single shot
        then denies a critical audit because a SIBLING instance's writer was
        mid-append (measured: the issue-radar trust tests failing whenever the
        session directory carried another instance's live writer). The hold
        registry is keyed by lock path at module level, so the sibling joins
        and waits its bounded turn instead.
        """
        log_a = SecurityEventLog(base_dir=tmp_path, sync=True)
        log_a.log(_make_event(event_id="xinst-preheat"))
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log_b = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log_a is not log_b

        entered = threading.Event()
        release = threading.Event()

        def holder():
            with log_a._chain_lock():  # sibling instance's hold
                entered.set()
                release.wait(10)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            threading.Timer(0.3, release.set).start()
            started = time.monotonic()
            # Pre-fix: refused with "held by another writer".
            log_b.log(_make_event(event_id="xinst-critical"), critical=True)
            assert time.monotonic() - started < 5.0
        finally:
            release.set()
            t.join(timeout=10)
        assert "xinst-critical" in log_b._path.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_contended_lock_fails_closed_on_the_event_loop(self, tmp_path):
        """On the loop thread a held lock must refuse, not wait.

        prune holds this lock across a whole streaming rewrite, so waiting here
        would stall chat and the heartbeat for that entire duration. Refusing
        surfaces as OSError, which a critical audit turns into audit-or-deny.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="preheat-1"))  # materialize dir + sidecar
        lock_path = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE

        # A second open file description contends with the writer's own, so this
        # stands in for another process holding the lock.
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(holder, exclusive=True)
        try:
            before = log._path.read_text(encoding="utf-8")
            with pytest.raises(OSError):
                log.log(_make_event(event_id="contended-1"), critical=True)
            assert log._path.read_text(encoding="utf-8") == before, "wrote while locked out"
        finally:
            platform_compat.release_lock(holder)
            os.close(holder)

        # Once the holder releases, the same instance appends normally again.
        log.log(_make_event(event_id="after-release-1"))
        assert "after-release-1" in log._path.read_text(encoding="utf-8")

    @requires_symlinks
    def test_symlinked_sidecar_is_refused(self, tmp_path):
        """A planted link is not a lock — following it defeats serialization.

        If the sidecar resolves elsewhere, replacing its target hands two writers
        locks on different inodes, which is the fork this serialization prevents.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR
        trust.mkdir(mode=0o700, parents=True, exist_ok=True)
        elsewhere = tmp_path / "planted-target"
        elsewhere.write_text("", encoding="utf-8")
        (trust / kiro_crew_sel._SEL_LOCK_FILE).symlink_to(elsewhere)

        with pytest.raises(OSError):
            log.log(_make_event(event_id="symlink-1"), critical=True)
        assert not log._path.exists() or "symlink-1" not in log._path.read_text(
            encoding="utf-8"
        )

    def test_hard_linked_sidecar_is_refused(self, tmp_path):
        """A second name for the same inode is outside the deny-list's reach."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        trust = tmp_path / kiro_crew_sel._TRUST_SUBDIR
        trust.mkdir(mode=0o700, parents=True, exist_ok=True)
        sidecar = trust / kiro_crew_sel._SEL_LOCK_FILE
        sidecar.write_text("", encoding="utf-8")
        os.link(sidecar, tmp_path / "second-name")

        with pytest.raises(OSError):
            log.log(_make_event(event_id="hardlink-1"), critical=True)

    @pytest.mark.asyncio
    async def test_corrupt_tail_does_not_scan_the_log_on_the_event_loop(self, tmp_path):
        """On the loop the tip read is bounded, so it fails closed instead of scanning.

        A corrupt tail with no newline would otherwise walk the whole file, and
        doing that under a synchronous critical audit stalls chat and the
        heartbeat.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="preheat-2"))
        # A single unterminated record longer than the bounded tail read, so the
        # last COMPLETE record lies more than one chunk back.
        with open(log._path, "a", encoding="utf-8") as f:
            f.write("x" * 9000)

        with pytest.raises(OSError):
            log.log(_make_event(event_id="onloop-corrupt-1"), critical=True)

        # Off the loop the same log still recovers: the unbounded scan walks past
        # the corrupt tail to the last complete record.
        assert log._read_last_hash() != ""

    @pytest.mark.asyncio
    async def test_loop_is_not_blocked_by_a_writer_waiting_cross_process(self, tmp_path):
        """A writer waiting on the chain lock must not stall a loop-side audit.

        With the thread lock taken first, a background writer blocked on another
        process's chain lock would hold _lock, and a loop-side critical audit
        would then block on _lock — an ordinary blocking lock with no fail-closed
        gate. Taking the chain lock first means the loop-side path reaches its
        single-shot gate before it can wait on anything.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="order-preheat"))
        lock_path = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE

        # Stand in for another process holding the sidecar (e.g. tail recovery).
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(holder, exclusive=True)
        released = threading.Event()

        def _release_holder():
            if released.is_set():
                return
            released.set()
            platform_compat.release_lock(holder)

        # Watchdog: with the lock order reversed this test would DEADLOCK (the
        # loop-side audit blocks on _lock, so the release below is never
        # reached). Releasing from a timer converts that into a clean, fast
        # assertion failure on the elapsed time rather than a CI timeout.
        watchdog = threading.Timer(5.0, _release_holder)
        watchdog.daemon = True
        watchdog.start()

        writer_reached_wait = threading.Event()
        writer_done = threading.Event()

        def _blocked_writer():
            # Off the loop, so this waits on the chain lock the way the real
            # background writer would.
            writer_reached_wait.set()
            log.log(_make_event(event_id="order-writer"))
            writer_done.set()

        t = threading.Thread(target=_blocked_writer, daemon=True)
        t.start()
        try:
            assert writer_reached_wait.wait(5)
            # Give the writer time to actually enter its wait.
            await asyncio.sleep(0.2)
            assert not writer_done.is_set(), "writer should still be waiting"

            # The loop-side audit must fail fast, not block behind the writer.
            started = time.monotonic()
            with pytest.raises(OSError):
                log.log(_make_event(event_id="order-loop"), critical=True)
            assert time.monotonic() - started < 2.0, "loop-side audit blocked"
        finally:
            watchdog.cancel()
            _release_holder()
            os.close(holder)
            t.join(timeout=10)

        assert writer_done.is_set(), "writer never completed after release"
        body = log._path.read_text(encoding="utf-8")
        assert "order-writer" in body
        assert "order-loop" not in body

    @pytest.mark.asyncio
    async def test_on_loop_contention_makes_exactly_one_nonblocking_attempt(
        self, tmp_path, monkeypatch
    ):
        """The loop-side acquire must try ONCE and refuse — never block or poll.

        A retry spin, however short each nap, sleeps the gateway event loop, so
        the stall is paid by every session it serves. This pins the contract:
        one ``try_acquire_lock`` call, an immediate fail-closed raise, and no
        call to either blocking lock primitive. Wall time cannot prove that
        contract: runner scheduling and the surrounding file work are outside
        the lock algorithm but inside any elapsed-time measurement.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="single-shot-preheat"))
        lock_path = tmp_path / kiro_crew_sel._TRUST_SUBDIR / kiro_crew_sel._SEL_LOCK_FILE

        # Stand in for a sibling process holding the sidecar.
        holder = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(holder, exclusive=True)

        attempts: list[tuple[int, bool]] = []
        real_try = platform_compat.try_acquire_lock

        def _counting_try(fd: int, *, exclusive: bool = False) -> bool:
            attempts.append((fd, exclusive))
            return real_try(fd, exclusive=exclusive)

        def _blocking_acquire_is_a_bug(*_args, **_kwargs):
            pytest.fail("on-loop chain-lock acquire used a blocking primitive")

        monkeypatch.setattr(kiro_crew_sel.platform_compat, "try_acquire_lock", _counting_try)
        monkeypatch.setattr(
            kiro_crew_sel.platform_compat, "acquire_lock", _blocking_acquire_is_a_bug
        )
        monkeypatch.setattr(kiro_crew_sel.platform_compat, "file_lock", _blocking_acquire_is_a_bug)
        try:
            with pytest.raises(OSError):
                log.log(_make_event(event_id="single-shot-critical"), critical=True)
        finally:
            platform_compat.release_lock(holder)
            os.close(holder)

        assert len(attempts) == 1, f"on-loop acquire polled {len(attempts)} times"
        assert attempts[0][1] is True, "the single lock attempt was not exclusive"
        assert "single-shot-critical" not in log._path.read_text(encoding="utf-8")

    def test_on_loop_acquire_helper_never_sleeps(self):
        """No sleep may reappear in the on-loop acquire, in any form.

        A source-level assertion rather than a timing one: a future poll spin
        would be reintroduced as a sleep call, and a wall-clock bound alone
        would tolerate a short one.
        """
        src = inspect.getsource(kiro_crew_sel._acquire_chain_lock_on_loop)
        body = src.split('"""')[-1]
        assert "sleep" not in body, "on-loop chain-lock acquire sleeps on the event loop"
        assert not hasattr(kiro_crew_sel, "time"), (
            "kiro_crew.sel imports time again — the on-loop path must not sleep"
        )

    @pytest.mark.asyncio
    async def test_loop_joiner_fails_closed_when_the_hold_is_promoted_mid_wait(
        self, tmp_path
    ):
        """A join admitted under "append" must not wait through a promotion.

        The label check in ``_try_join_chain_hold`` runs before the gate wait,
        so rotation's relabel (or a prune joining) can land AFTER a loop-side
        joiner was admitted -- and a plain blocking gate acquire would then
        stall the event loop for the heavy step's whole duration. The wait
        must re-read the label and raise once the hold stops being an append.
        """
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="promote-preheat"))
        key = str(log._chain_lock_path())

        entered = threading.Event()
        release = threading.Event()

        def _holder() -> None:
            with log._chain_lock():
                entered.set()
                release.wait(timeout=10)

        holder = threading.Thread(target=_holder)
        holder.start()
        assert entered.wait(timeout=5), "holder never took the chain lock"

        def _promote_soon() -> None:
            # Give the loop-side joiner time to be admitted and start waiting
            # on the gate, then promote the hold the way rotation's relabel
            # does. 0.2s spans several poll slices.
            release.wait(timeout=0.2)
            with kiro_crew_sel._CHAIN_HOLDS_MUTEX:
                hold = kiro_crew_sel._CHAIN_HOLDS.get(key)
                if hold is not None:
                    hold.kind = "rotation"

        promoter = threading.Thread(target=_promote_soon)
        promoter.start()
        try:
            started = time.monotonic()
            with pytest.raises(OSError, match="promoted"):
                log.log(_make_event(event_id="promoted-critical"), critical=True)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            promoter.join(timeout=5)
            holder.join(timeout=5)

        assert elapsed < 5, f"loop joiner waited {elapsed:.3f}s through the promotion"
        assert "promoted-critical" not in log._path.read_text(encoding="utf-8")
        # The refused joiner must have left the hold: once the holder exits,
        # the registry entry is gone and a fresh append works normally.
        with kiro_crew_sel._CHAIN_HOLDS_MUTEX:
            assert key not in kiro_crew_sel._CHAIN_HOLDS, "hold leaked after refusal"
        log.log(_make_event(event_id="after-promotion-1"))
        assert "after-promotion-1" in log._path.read_text(encoding="utf-8")


class TestInferSource:
    @pytest.mark.parametrize("key,expected", [
        ("dashboard:slot0", "dashboard"),
        ("dashboard:slot5", "dashboard"),
        # The side chat's isolated session (`side:<slot>`) IS a dashboard
        # surface: a dashboard-bound governance profile must bind it. Before
        # this branch the key fell through to the "slack" fallback and a
        # dashboard-scoped profile skipped every side turn.
        ("side:slot0", "dashboard"),
        ("side:dashboard:slot0", "dashboard"),
        ("cron:job123", "cron"),
        ("subagent:abc", "subagent"),
        ("taskrunner:spec1", "taskrunner"),
        ("_bg", "background"),
        ("_consolidate", "background"),
        ("_consolidate:other", "slack"),
        ("cli_chat", "cli"),
        # Namespaced messaging channels are attributed to their transport,
        # matching context._runtime_display_name's set — via ``{ns}:`` …
        ("discord:123:kirocrew", "discord"),
        ("telegram:456", "telegram"),
        ("wecom:c1", "wecom"),
        ("weixin:c1", "weixin"),
        ("feishu:c1", "feishu"),
        ("webex:c1", "webex"),
        ("teams:c1", "teams"),
        ("slack:C08:thread", "slack"),
        # … or the ``{ns}_`` prefix form.
        ("discord_123", "discord"),
        # Bare/legacy Slack keys (thread timestamps, no namespace) stay "slack".
        ("C08HZAWV4TP:thread123", "slack"),
        ("random_key", "slack"),
        # An empty key carries no surface signal → "unknown", NOT "slack"
        # (an app-activation governance degrade passes no session_key).
        ("", "unknown"),
        # The explicit host-process sentinel → "host" (stable bind target for
        # host-side governance: app activation, workspace admission).
        ("_host", "host"),
    ])
    def test_infer_source(self, key, expected):
        assert _infer_source(key) == expected


class TestSingleton:
    def test_returns_same_instance(self, sel_dir):
        log1 = SecurityEventLog(base_dir=sel_dir, sync=True)
        log2 = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log1 is log2

    def test_sel_accessor(self, sel_dir):
        """The module-level sel() function returns the singleton."""
        with patch("kiro_crew.sel._default_dir", lambda: sel_dir):
            instance = sel()
            assert isinstance(instance, SecurityEventLog)


class TestReadLastHash:
    def test_reads_hash_from_existing_file(self, log, sel_dir):
        log.log(SecurityEvent(
            event_id="first",
            timestamp="2026-01-01T00:00:00+00:00",
            event_type="tool_invocation",
            caller_identity="dashboard:slot0",
            agent="kirocrew",
            source="dashboard",
            operation="op1",
        ))
        expected_hash = log._last_hash
        # Reset and re-read
        SecurityEventLog._instance = None
        log2 = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log2._last_hash == expected_hash
