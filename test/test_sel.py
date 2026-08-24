"""Tests for kiro_crew.sel — Security Event Log."""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew import platform_compat
from kiro_crew.sel import SecurityEvent, SecurityEventLog, _infer_source, sel, sel_hmac_key_path


@pytest.fixture
def small_segments(monkeypatch):
    """Shrink the size cap so a handful of events triggers real rotation."""
    monkeypatch.setattr(sel_mod, "_SEGMENT_MAX_BYTES", 4 * 1024)
    monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 3)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the SEL singleton between tests."""
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    yield
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


@pytest.fixture
def sel_dir(tmp_path):
    """Provide a temp directory for SEL storage."""
    return tmp_path


@pytest.fixture
def log(sel_dir):
    """Create a fresh SEL instance in a temp dir.

    sync=True so events are written inline — these tests read the raw log file
    immediately after logging. The async background writer is covered
    separately in TestAsyncWriter.
    """
    return SecurityEventLog(base_dir=sel_dir, sync=True)


def _make_event(**overrides) -> SecurityEvent:
    """Build a SecurityEvent with sensible defaults for edge-case tests."""
    base = {
        "event_id": "extras-evt-0001",
        "timestamp": "2026-05-13T00:00:00+00:00",
        "event_type": "tool_invocation",
        "caller_identity": "dashboard:abc",
        "agent": "kirocrew",
        "source": "dashboard",
        "operation": "execute_bash",
    }
    base.update(overrides)
    return SecurityEvent(**base)


def _lock_is_held(lock_path: Path) -> bool:
    """Whether *lock_path* is exclusively locked, observed from another thread.

    ``fcntl.flock`` is per open-file-description, so a fresh ``open`` in this
    same process still contends with the holder's fd -- which is what lets a test
    assert that a critical section really runs under the cross-process lock. On
    Windows ``msvcrt.locking`` is per-fd in the same way. Returns False when the
    lock file does not exist yet (nothing has been serialized).
    """
    if not lock_path.exists():
        return False
    with open(lock_path, "a+b") as probe:
        if platform_compat.try_acquire_lock(probe.fileno(), exclusive=True):
            platform_compat.release_lock(probe.fileno())
            return False
    return True


def _fill(log: SecurityEventLog, count: int, *, start: int = 0, step_secs: int = 1) -> None:
    """Write *count* chronologically increasing events, seq=<n> in ``resources``."""
    base = datetime(2026, 8, 21, tzinfo=timezone.utc)
    for i in range(start, start + count):
        log.log(
            _make_event(
                event_id=f"evt{i:06d}",
                timestamp=(base + timedelta(seconds=i * step_secs)).isoformat(),
                resources=f"seq={i}",
            )
        )


#: Iteration cap for the bounded poll loops below. Generous against a healthy
#: writer (the rotation loop needs 1-2 batches; a healthy top-up usually needs
#: zero), tight enough that a genuinely broken one fails fast without flooding
#: the shared session dir -- that dir lives under pytest's basetemp, which is
#: retained across runs, so an unbounded failure loop would persist tens of MB
#: per red run.
_POLL_ITERATION_CAP = 40


def _fill_until_over_cap(log: SecurityEventLog, *, deadline: float, start: int) -> int:
    """Top up through the async writer until the live log is over the size cap.

    Two mechanisms can leave the live log BELOW the cap after a large fill even
    with a perfectly healthy writer: ``flush()`` waits on the pending-event
    counter with a bounded timeout and RETURNS on expiry, so under runner load
    the tail of the fill may not be on disk yet; and a mid-fill batch boundary
    can itself enter the rotation window and rotate, resetting the live log to
    nearly empty. Both are harness properties, not defects, so the over-the-cap
    precondition is established by topping up in small batches rather than
    asserted after one fixed fill (#5017). Bounded by *deadline* and by
    ``_POLL_ITERATION_CAP``; the diagnostic reports the pending-queue depth so
    a wedged writer is distinguishable from a rotation problem.

    Returns the next unused ``seq=`` number for the caller's own fills.
    """
    seq = start
    iterations = 0
    while log._live_size() <= sel_mod._SEGMENT_MAX_BYTES:
        iterations += 1
        assert iterations <= _POLL_ITERATION_CAP and time.monotonic() < deadline, (
            f"precondition never reached: live log stayed at {log._live_size()} "
            f"bytes (cap {sel_mod._SEGMENT_MAX_BYTES}) after {iterations - 1} "
            f"top-up batches, {log._pending} events still pending in the writer"
        )
        _fill(log, 5, start=seq)
        seq += 5
        log.flush()
    return seq


class TestHmacKeyManagement:
    def test_creates_key_file_on_first_init(self, sel_dir):
        SecurityEventLog(base_dir=sel_dir, sync=True)
        key_path = sel_dir / "trust" / "sel_hmac.key"
        assert key_path.exists()
        assert len(key_path.read_bytes()) == 32

    def test_key_file_permissions(self, sel_dir):
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


class TestInferSource:
    @pytest.mark.parametrize("key,expected", [
        ("dashboard:slot0", "dashboard"),
        ("dashboard:slot5", "dashboard"),
        ("cron:job123", "cron"),
        ("subagent:abc", "subagent"),
        ("taskrunner:spec1", "taskrunner"),
        ("_bg", "background"),
        ("cli_chat", "cli"),
        # Namespaced messaging channels are attributed to their transport (#815),
        # matching context._runtime_display_name's set (#979) — via ``{ns}:`` …
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


# ─────────────────────────────────────────────────────────────────────────
# Edge-case tests — paths the baseline coverage push doesn't exercise:
# HMAC-tamper vs chain-break detection, the 4 KB-boundary backward scan
# in ``_read_last_hash``, redaction of forwarded callback payloads, and
# robustness paths around malformed/blank lines in the on-disk JSONL.
# ─────────────────────────────────────────────────────────────────────────


class TestSecurityEventDataclass:
    def test_default_optional_fields(self) -> None:
        evt = _make_event()
        assert evt.tool_kind == ""
        assert evt.outcome == ""
        assert evt.resources == ""
        assert evt.downstream_service == ""
        assert evt.request_id == ""
        assert evt.error == ""
        assert evt.prev_hash == ""
        assert evt.entry_hash == ""
        assert evt.metadata == {}

    def test_metadata_default_factory_is_per_instance(self) -> None:
        # Catch the classic mutable-default-arg bug if someone "fixes" the
        # dataclass to use a literal {} default.
        a = _make_event()
        b = _make_event()
        a.metadata["x"] = 1
        assert b.metadata == {}


class TestHmacKeyManagementExtras:
    def test_chmod_failure_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Read-only filesystems raise OSError on chmod — must not crash init.
        # SEL key perms now go through platform_compat.chmod_safe (logs + swallows
        # OSError; no-op on Windows), so patch os.chmod IN platform_compat to
        # exercise the fail-soft path.
        def _boom(*a, **kw):
            raise OSError("chmod denied")

        monkeypatch.setattr("kiro_crew.platform_compat.os.chmod", _boom)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert (tmp_path / "trust" / "sel_hmac.key").exists()
        assert log._hmac_key

    def test_lockdown_failure_is_fail_soft(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restrict_to_owner failure must not crash SecurityEventLog init.

        The chmod test above only exercises the POSIX arm of
        ``restrict_to_owner``; on Windows it runs icacls instead, so this
        variant injects the failure at ``restrict_to_owner`` itself — the seam
        ``atomic_write`` calls on every platform — pinning that
        ``restrict_on_error="warn"`` keeps key creation fail-soft.
        """
        def _refuse(_target):
            raise OSError("icacls failed")

        monkeypatch.setattr("kiro_crew.platform_compat.restrict_to_owner", _refuse)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert (tmp_path / "trust" / "sel_hmac.key").exists()
        assert log._hmac_key

    def test_key_lockdown_precedes_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh HMAC key must never exist in a file that has not been
        locked down yet.

        atomic_write(restrict_to_owner=True) applies the lockdown to the TEMP
        file before the key bytes reach it (the previous post-rename lockdown
        left a brand-new key readable under the inherited DACL on Windows for
        the write window, issue #5285). Asserted by measuring the file's SIZE
        at lockdown time — zero means no key byte existed yet.
        """
        from kiro_crew import platform_compat

        trust_dir = tmp_path / "trust"
        calls: list[tuple[Path, int]] = []
        real_restrict = platform_compat.restrict_to_owner

        def _measuring(target):
            p = Path(str(target))
            if p.is_file() and p.parent == trust_dir:
                calls.append((p, os.stat(p).st_size))
            return real_restrict(target)

        monkeypatch.setattr("kiro_crew.platform_compat.restrict_to_owner", _measuring)
        log = SecurityEventLog(base_dir=tmp_path, sync=True)

        assert log._hmac_key
        assert calls, "premise: the key lockdown ran at all"
        _, size = calls[0]
        assert size == 0, f"the key file already held {size} bytes at lockdown time"

    def test_singleton_init_is_idempotent(self, tmp_path: Path) -> None:
        a = SecurityEventLog(base_dir=tmp_path, sync=True)
        # Second call must reuse the original instance and ignore base_dir.
        other = tmp_path / "other"
        b = SecurityEventLog(base_dir=other, sync=True)
        assert a is b
        assert a._dir == tmp_path
        assert not other.exists()


class TestLogHashAndCallbackExtras:
    def test_compute_hash_is_deterministic(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        evt = _make_event()
        h1 = log._compute_hash(evt)
        h2 = log._compute_hash(evt)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_compute_hash_excludes_entry_hash_field(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        evt = _make_event()
        h_before = log._compute_hash(evt)
        evt.entry_hash = "anything"
        # Hash MUST be stable when only the (excluded) entry_hash field changes.
        assert log._compute_hash(evt) == h_before

    def test_log_invokes_forward_callback_with_redacted_payload(
        self, tmp_path: Path
    ) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        captured: list[dict] = []
        log.set_forward_callback(captured.append)
        # Embed an AWS access key in resources — must be redacted before
        # forwarding to avoid credential exfiltration via the audit pipeline.
        log.log(_make_event(resources="key=AKIAIOSFODNN7EXAMPLE"))
        assert len(captured) == 1
        forwarded = captured[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in forwarded["resources"]
        assert "REDACTED" in forwarded["resources"]

    def test_set_forward_callback_unregister(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        captured: list[dict] = []
        log.set_forward_callback(captured.append)
        log.log(_make_event(event_id="e1"))
        log.set_forward_callback(None)
        log.log(_make_event(event_id="e2"))
        assert len(captured) == 1
        assert captured[0]["event_id"] == "e1"


class TestVerifyIntegrityExtras:
    def test_detects_chain_break(self, tmp_path: Path) -> None:
        # Distinct from a tampered HMAC: here the prev_hash linkage is
        # broken but the entry's own HMAC may still verify in isolation.
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="e0"))
        log.log(_make_event(event_id="e1"))
        path = tmp_path / "security_events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        d1 = json.loads(lines[1])
        d1["prev_hash"] = "deadbeef" * 8
        lines[1] = json.dumps(d1)
        path.write_text("\n".join(lines) + "\n")
        total, valid = log.verify_integrity()
        assert total == 2
        assert valid == 1  # entry 1 fails the chain check

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event())
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n\n   \n")
        total, valid = log.verify_integrity()
        assert total == 1 and valid == 1

    def test_handles_malformed_json(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event())
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "not-json-at-all\n")
        total, valid = log.verify_integrity()
        # Malformed line counts toward total, doesn't count as valid.
        assert total == 2
        assert valid == 1


class TestLogToolInvocationExtras:
    def test_explicit_source_overrides_inferred(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log_tool_invocation(
            session_key="dashboard:abc",  # would infer "dashboard"
            source="cli",  # explicit override
            tool_name="t",
            outcome="approved",
        )
        assert log.recent()[0]["source"] == "cli"

    def test_request_id_coerced_to_string(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log_tool_invocation(
            session_key="cli_chat",
            tool_name="t",
            outcome="approved",
            request_id=42,  # int — must be coerced
        )
        assert log.recent()[0]["request_id"] == "42"

    def test_metadata_is_persisted(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log_tool_invocation(
            session_key="cli_chat",
            tool_name="t",
            outcome="approved",
            metadata={"k": "v"},
        )
        assert log.recent()[0]["metadata"] == {"k": "v"}


class TestLogApiAccessExtras:
    def test_truncates_long_resources_and_error(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log_api_access(
            caller="alice",
            operation="op",
            outcome="failed",
            resources="r" * 800,
            error="e" * 800,
        )
        e = log.recent()[0]
        assert len(e["resources"]) == 500  # _MAX_ARG_LEN
        assert len(e["error"]) == 500


class TestRecentExtras:
    def test_respects_limit(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        for i in range(10):
            log.log(_make_event(event_id=f"e{i}"))
        events = log.recent(limit=3)
        assert len(events) == 3
        assert [e["event_id"] for e in events] == ["e9", "e8", "e7"]

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="good"))
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "garbage-line\n")
        events = log.recent()
        assert len(events) == 1
        assert events[0]["event_id"] == "good"

    def test_recent_skips_blank_lines(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event())
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n   \n")
        assert len(log.recent()) == 1


class TestPruneExtras:
    def test_recomputes_last_hash_after_prune(self, tmp_path: Path) -> None:
        # When prune removes the chain tail, _last_hash must move back so
        # subsequent log() calls link to the surviving tail, not a phantom.
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="old", timestamp="2020-01-01T00:00:00+00:00"))
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat()
        log.log(_make_event(event_id="fresh", timestamp=now))
        log.prune()
        log.log(_make_event(event_id="newer", timestamp=now))
        events = log.recent()
        assert events[0]["event_id"] == "newer"
        assert events[0]["prev_hash"] == events[1]["entry_hash"]

    def test_prune_removes_malformed_lines(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat()
        log.log(_make_event(timestamp=now))
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "not-json\n")
        # Malformed line is removable (not a structured retainable entry).
        assert log.prune() == 1

    def test_prune_keeps_when_nothing_old(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        from datetime import datetime, timezone

        now = datetime.now(tz=timezone.utc).isoformat()
        log.log(_make_event(timestamp=now))
        assert log.prune() == 0
        assert len(log.recent()) == 1


class TestReadLastHashExtras:
    def test_scans_back_across_4kb_boundary(self, tmp_path: Path) -> None:
        # Force the backward-scan loop to iterate past one 4 KB chunk so the
        # buf-prepend path is exercised.
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        big_resources = "x" * 200  # ~250 B per JSONL line
        for i in range(60):  # ~15 KB total — well past 4 KB chunk
            log.log(_make_event(event_id=f"e{i:02d}", resources=big_resources))
        expected_tail = log._last_hash

        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        log2 = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log2._last_hash == expected_tail

    def test_corrupt_file_falls_back_to_empty(self, tmp_path: Path) -> None:
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        tmp_path.mkdir(parents=True, exist_ok=True)
        # Single un-parseable line — _read_last_hash must swallow the
        # JSONDecodeError and return "" so init can succeed.
        (tmp_path / "security_events.jsonl").write_text("not json\n")
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._last_hash == ""


class TestAsyncWriter:
    """The default (production) async background-writer path."""

    def test_async_log_then_flush_persists(self, tmp_path: Path) -> None:
        """Async log() enqueues; flush() guarantees the events are on disk."""
        log = SecurityEventLog(base_dir=tmp_path)  # async (default)
        for i in range(5):
            log.log(_make_event(event_id=f"a{i}", operation=f"op{i}"))
        log.flush()
        sel_file = tmp_path / "security_events.jsonl"
        lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5

    def test_async_chain_intact_after_batch(self, tmp_path: Path) -> None:
        """Batched async writes still form a valid HMAC chain."""
        log = SecurityEventLog(base_dir=tmp_path)
        for i in range(20):
            log.log(_make_event(event_id=f"b{i}", operation=f"op{i}"))
        total, valid = log.verify_integrity()  # flushes internally
        assert total == 20
        assert valid == 20

    def test_recent_flushes_before_read(self, tmp_path: Path) -> None:
        """recent() must surface just-enqueued events (flush-before-read)."""
        log = SecurityEventLog(base_dir=tmp_path)
        log.log(_make_event(event_id="r0", operation="opX"))
        events = log.recent(limit=10)
        assert any(e["operation"] == "opX" for e in events)

    def test_async_concurrent_writes_no_loss(self, tmp_path: Path) -> None:
        """Many threads enqueue concurrently; flush then all land, chain valid."""
        log = SecurityEventLog(base_dir=tmp_path)

        def writer(start: int) -> None:
            for i in range(25):
                log.log(_make_event(event_id=f"t{start}_{i}", operation=f"op{start}_{i}"))

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total, valid = log.verify_integrity()
        assert total == 100
        assert valid == 100

    def test_flush_noop_when_nothing_queued(self, tmp_path: Path) -> None:
        """flush() on an idle log returns immediately without error."""
        log = SecurityEventLog(base_dir=tmp_path)
        log.flush()  # no writer started yet — must not hang or raise

    def test_writer_survives_failing_batch(self, tmp_path: Path) -> None:
        """If _flush_batch raises, the writer must still decrement _pending (so
        flush() doesn't hang forever) and keep draining subsequent events."""
        log = SecurityEventLog(base_dir=tmp_path)
        calls = {"n": 0}
        real_flush = log._flush_batch

        def _flaky(events):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("simulated mkdir/write failure")
            return real_flush(events)

        log._flush_batch = _flaky  # type: ignore[method-assign]
        log.log(_make_event(event_id="boom"))
        # flush() must return within the timeout, not hang on a stuck _pending.
        log.flush(timeout=2.0)
        assert log._pending == 0
        # A subsequent event still drains (the writer thread did not die).
        log.log(_make_event(event_id="ok"))
        log.flush(timeout=2.0)
        assert log._pending == 0
        assert any(e["event_id"] == "ok" for e in log.recent(limit=10))

    def test_last_hash_rolls_back_on_write_failure(self, tmp_path: Path) -> None:
        """A failed append must not advance _last_hash — otherwise the next
        event chains off a hash never written to disk, corrupting the HMAC
        chain. sync=True so the failing write happens inline."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event(event_id="e0"))  # persisted; establishes the tip
        tip = log._last_hash

        # Make the next append's open() fail, then restore it.
        real_os_open = os.open
        state = {"fail": True}

        def _maybe_fail(path, *a, **k):
            if state["fail"] and str(path).endswith("security_events.jsonl"):
                raise OSError("disk full")
            return real_os_open(path, *a, **k)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(os, "open", _maybe_fail)
        log.log(_make_event(event_id="e1"))  # write fails — must roll back
        monkeypatch.undo()

        # _last_hash unchanged (the failed event left no trace).
        assert log._last_hash == tip
        # The next successful event chains off the real tip, so the on-disk
        # chain verifies clean (no phantom-hash break).
        log.log(_make_event(event_id="e2"))
        total, valid = log.verify_integrity()
        assert total == valid  # every persisted entry links correctly
        ids = [e["event_id"] for e in log.recent(limit=10)]
        assert "e1" not in ids  # the failed write is absent
        assert "e2" in ids and "e0" in ids


class TestCriticalWrite:
    """Fail-closed ``critical=True`` audits — the crux of "audit-or-deny".

    The async writer swallows filesystem errors and warns (an audit log is
    eventually-durable). A CRITICAL audit must NOT be swallowed: it is written
    synchronously and the error propagates, so the caller (safety-override
    activation, unattended heartbeat auto-approve) can refuse the action it was
    about to audit rather than proceed unaudited. Pentest: YOLO activated while
    the SEL file was chmod 000 because ``log()`` never raised.
    """

    def test_critical_log_raises_when_file_unwritable(self, tmp_path: Path) -> None:
        """A critical write to an unwritable SEL file re-raises OSError."""
        log = SecurityEventLog(base_dir=tmp_path)
        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise PermissionError("SEL file unwritable (chmod 000)")
            return real_os_open(path, *a, **k)

        mp = pytest.MonkeyPatch()
        mp.setattr(os, "open", _boom)
        try:
            with pytest.raises(OSError):
                log.log(_make_event(event_id="crit"), critical=True)
        finally:
            mp.undo()

    def test_critical_log_persists_synchronously_without_flush(self, tmp_path: Path) -> None:
        """A critical write lands on disk immediately (no flush() needed)."""
        log = SecurityEventLog(base_dir=tmp_path)
        log.log(_make_event(event_id="crit-ok"), critical=True)
        # Read the raw file directly — do NOT call recent() (which flushes),
        # proving the write was synchronous.
        raw = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
        assert "crit-ok" in raw

    def test_critical_drains_queued_events_first_preserving_chain(self, tmp_path: Path) -> None:
        """Queued async events are drained before the critical write so the
        on-disk HMAC chain keeps enqueue order and verifies clean."""
        log = SecurityEventLog(base_dir=tmp_path)
        log.log(_make_event(event_id="async-1"))
        log.log(_make_event(event_id="async-2"))
        log.log(_make_event(event_id="crit"), critical=True)  # drains then writes
        total, valid = log.verify_integrity()
        assert total == valid == 3
        ids = [e["event_id"] for e in log.recent(limit=10)]
        assert {"async-1", "async-2", "crit"} <= set(ids)

    def test_sync_mode_critical_raises(self, tmp_path: Path) -> None:
        """In sync mode a critical write still re-raises on failure."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise OSError("disk full")
            return real_os_open(path, *a, **k)

        mp = pytest.MonkeyPatch()
        mp.setattr(os, "open", _boom)
        try:
            with pytest.raises(OSError):
                log.log(_make_event(event_id="crit-sync"), critical=True)
        finally:
            mp.undo()

    def test_non_critical_log_still_swallows_write_error(self, tmp_path: Path) -> None:
        """Regression guard: a NON-critical write must remain best-effort
        (swallow + warn), never propagate to the hot-path caller."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise OSError("disk full")
            return real_os_open(path, *a, **k)

        mp = pytest.MonkeyPatch()
        mp.setattr(os, "open", _boom)
        try:
            log.log(_make_event(event_id="soft"))  # must NOT raise
        finally:
            mp.undo()

    def test_log_api_access_critical_raises(self, tmp_path: Path) -> None:
        """``log_api_access(critical=True)`` propagates a write failure."""
        log = SecurityEventLog(base_dir=tmp_path)
        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise PermissionError("unwritable")
            return real_os_open(path, *a, **k)

        mp = pytest.MonkeyPatch()
        mp.setattr(os, "open", _boom)
        try:
            with pytest.raises(OSError):
                log.log_api_access(
                    caller="safety_override",
                    operation="safety_override:activate",
                    outcome="enabled",
                    critical=True,
                )
        finally:
            mp.undo()

    def test_log_tool_invocation_critical_raises(self, tmp_path: Path) -> None:
        """``log_tool_invocation(critical=True)`` propagates a write failure."""
        log = SecurityEventLog(base_dir=tmp_path)
        real_os_open = os.open

        def _boom(path, *a, **k):
            if str(path).endswith("security_events.jsonl"):
                raise PermissionError("unwritable")
            return real_os_open(path, *a, **k)

        mp = pytest.MonkeyPatch()
        mp.setattr(os, "open", _boom)
        try:
            with pytest.raises(OSError):
                log.log_tool_invocation(
                    session_key="_hb",
                    tool_name="ReadInternalWebsites",
                    outcome="auto_approved",
                    critical=True,
                )
        finally:
            mp.undo()


# ─────────────────────────────────────────────────────────────────────────
# Audit-chain hardening regression tests (Track B):
#   1. HMAC key length validation (reject empty/short keys — hard fail)
#   2. HMAC key permission re-enforcement on load
#   3. _read_last_hash no longer resets the chain to genesis on a corrupt
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

    Regression for the silent-void bug: _read_last_hash() recovers the right
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

        lines = (tmp_path / "security_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        # Last physical line must be the NEW record, cleanly parseable — not
        # the corrupt fragment glued to it.
        last = json.loads(lines[-1])
        assert last["event_id"] == "e_after"
        # And it chains off the recovered tip.
        assert last["prev_hash"] == tip
        # The corrupt fragment is PRESERVED as its own line (append-only
        # forensic evidence), not truncated away.
        assert any(fragment in ln for ln in lines)

    def test_new_record_surfaces_in_recent_after_corrupt_tail(
        self, tmp_path: Path
    ) -> None:
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

        monkeypatch.setattr(
            "kiro_crew.platform_compat.unlink_link_or_junction", _deny_unlink
        )
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log._hmac_key == legacy_key
        assert sel_hmac_key_path() == tmp_path / "sel_hmac.key"
        # Nothing was ever written through the planted link.
        assert list(target.iterdir()) == []

    def test_migrated_short_key_still_hard_fails(self, tmp_path: Path) -> None:
        """Validation applies to the migrated file exactly as to a fresh one."""
        (tmp_path / "sel_hmac.key").write_bytes(b"x" * 8)
        with pytest.raises(RuntimeError, match="too short"):
            SecurityEventLog(base_dir=tmp_path, sync=True)

    def test_key_bytes_accessor_returns_the_live_signing_key(
        self, tmp_path: Path
    ) -> None:
        """The recovery path for the dependent protocol: SEL caches the
        validated bytes at init, so they stay available when the file behind the
        frozen resolved path no longer loads."""
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
        assert callers == {root / "session_pid_sig.py"}, (
            f"_sel_hmac_key_bytes gained a caller outside session_pid_sig: {callers}"
        )

    def test_concurrent_first_construction_initializes_once(self, tmp_path: Path) -> None:
        """``__new__`` publishes the instance BEFORE ``__init__`` runs, so two
        threads arriving in between both see ``_initialized`` False. Unserialized,
        both run the construction body and each can mint a fresh key — one wins
        on disk while the other signs from different bytes in memory, splitting
        the audit chain from the file every other process resolves.

        Reachable because SEL is now constructed from worker threads (the
        middleware deny audits offload via ``asyncio.to_thread``), where the
        event loop no longer serializes callers for free.
        """
        self._reset()
        calls: list[int] = []
        real = SecurityEventLog._load_or_create_hmac_key

        def counting(inst):
            calls.append(1)
            # Widen the window a real race would need, so an unlocked body
            # reliably interleaves instead of passing by luck.
            time.sleep(0.05)
            return real(inst)

        barrier = threading.Barrier(8)

        def build():
            barrier.wait()
            SecurityEventLog(base_dir=tmp_path, sync=True)

        with patch.object(SecurityEventLog, "_load_or_create_hmac_key", counting):
            threads = [threading.Thread(target=build) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(calls) == 1, (
            f"construction body ran {len(calls)} times; concurrent first "
            "denials can mint competing trust-root keys"
        )
        inst = SecurityEventLog._instance
        assert inst is not None and inst._initialized
        assert inst._hmac_key == (tmp_path / "trust" / "sel_hmac.key").read_bytes()
        self._reset()


class TestSizeRotation:
    """The log is closed at a size cap and retained as N segments (issue #4843).

    Before this, ``security_events.jsonl`` was a single file with no cap: a
    long-running install measured 4.09 GB, which made the only sanctioned reader
    impractical and made every append and read pay the size.
    """

    def test_live_log_is_closed_at_the_size_cap(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        live = sel_dir / "security_events.jsonl"
        segments = log._segments_oldest_first()
        assert segments, "no rotation happened; the log grew unbounded"
        assert live.stat().st_size < sel_mod._SEGMENT_MAX_BYTES * 2

    def test_retention_keeps_only_the_newest_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        segments = log._segments_oldest_first()
        assert len(segments) == sel_mod._SEGMENT_KEEP
        # The bound that matters: total on-disk size cannot exceed the cap times
        # the segments kept plus the live log.
        total = sum(p.stat().st_size for p in segments)
        total += (sel_dir / "security_events.jsonl").stat().st_size
        ceiling = sel_mod._SEGMENT_MAX_BYTES * (sel_mod._SEGMENT_KEEP + 2)
        assert total < ceiling, f"{total} bytes exceeds the retention ceiling {ceiling}"

    def test_retention_deletes_the_oldest_not_the_newest(self, sel_dir, small_segments):
        """The sequence must keep rising across deletions.

        A name reused after retention freed it would sort as the OLDEST segment,
        so the next sweep would delete the log that was just closed and keep the
        stale ones — silently discarding the newest audit history.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        seqs = [sel_mod._segment_seq(p) for p in log._segments_oldest_first()]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        # Newest surviving segment is the immediate predecessor of the live log.
        claimed = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert claimed["metadata"]["previous_segment"] == log._segments_oldest_first()[-1].name

    def test_each_segment_is_an_independent_chain(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        for path in log._segments_oldest_first():
            first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            assert first["prev_hash"] == "", f"{path.name} chains across the boundary"
            if sel_mod._segment_seq(path) > 1:
                # Only the very first segment ever closed has no predecessor to
                # name; every later one opens with the boundary record.
                assert first["event_type"] == "sel_rotation"

    def test_rotation_record_names_the_closed_segment(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        closed = log._segments_oldest_first()[-1]
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert opener["metadata"]["previous_segment"] == closed.name
        assert opener["metadata"]["previous_bytes"] > 0

    def test_the_rotation_record_makes_no_predecessor_hash_claim(
        self, sel_dir, small_segments
    ):
        """A claim about the closed segment's tip cannot be kept true.

        A segment is not immutable the instant it is renamed: another process may
        still hold a writable fd to that inode and land a record after the tip is
        read. Any hash captured would then be stale, and verification would report
        an untampered log as compromised -- the worst failure mode available. The
        name is enough to walk the sequence, and forgery is caught per record.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert "previous_entry_hash" not in opener["metadata"]

    def test_a_late_append_into_the_closed_segment_still_verifies(
        self, sel_dir, small_segments
    ):
        """The exact race the dropped claim could not survive.

        An appender holding an fd to the live log writes AFTER the rotator renamed
        it, so the record lands in the segment -- correctly chained, because the fd
        guard let it through precisely when the identity matched. Nothing about that
        may read as corruption.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment = log._segments_oldest_first()[-1]
        tip = json.loads(segment.read_text(encoding="utf-8").strip().splitlines()[-1])
        # Stand in for the appender that was mid-write across the rename: one more
        # correctly chained record appended to the already-closed segment.
        late = _make_event(event_id="late0", resources="late")
        late.prev_hash = tip["entry_hash"]
        late.entry_hash = log._compute_hash(late)
        with open(segment, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(late)) + "\n")

        total, valid = log.verify_integrity()
        assert total == valid, (
            f"{total - valid} entries reported as compromised by a benign late append"
        )

    def test_verify_integrity_spans_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        segment_lines = sum(
            len(p.read_text(encoding="utf-8").strip().splitlines())
            for p in log._segments_oldest_first()
        )
        total, valid = log.verify_integrity()
        assert total > segment_lines, "verification ignored the rotated segments"
        assert total == valid, f"{total - valid} entries failed verification"

    def test_deleting_an_aged_out_segment_leaves_survivors_verifiable(
        self, sel_dir, small_segments
    ):
        """The property rotation had to preserve.

        A retention deletion must not look like tampering. With one chain across
        the whole log, dropping the oldest file would orphan the next file's
        first ``prev_hash`` and report a break for every sweep.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        log._segments_oldest_first()[0].unlink()
        total, valid = log.verify_integrity()
        assert total == valid, f"retention deletion produced {total - valid} invalid entries"

    def test_tampering_inside_a_segment_is_still_caught(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        victim = log._segments_oldest_first()[-1]
        lines = victim.read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[2])
        record["outcome"] = "allowed"
        lines[2] = json.dumps(record)
        victim.write_text("\n".join(lines) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert valid < total, "an edited record inside a rotated segment verified clean"

    def test_a_rewritten_segment_is_reported_per_record(self, sel_dir, small_segments):
        """Substituted content is caught by signatures, not by a boundary claim.

        The claim a previous revision carried is gone (see
        ``_rotation_event``), so this is the property that has to hold on its own:
        a segment whose content was replaced cannot produce valid per-record HMACs,
        because the key lives outside the log directory.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        victim = log._segments_oldest_first()[-1]
        rows = [json.loads(line) for line in victim.read_text(encoding="utf-8").splitlines()]
        # Rewrite the segment with plausible-looking but unsigned records.
        forged = []
        for row in rows:
            row["outcome"] = "allowed"
            forged.append(json.dumps(row))
        victim.write_text("\n".join(forged) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert valid < total, "a rewritten segment verified clean"

    def test_a_truncated_segment_does_not_read_as_corruption(self, sel_dir, small_segments):
        """Losing the TAIL of a segment leaves the surviving records verifiable.

        Retention and a crash both produce this shape, and neither is tampering of
        the surviving records. With the old boundary claim a dropped final record
        made the SUCCESSOR's rotation record read as invalid; nothing here may.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        predecessor = log._segments_oldest_first()[-1]
        kept = predecessor.read_text(encoding="utf-8").strip().splitlines()
        predecessor.write_text("\n".join(kept[:-1]) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} surviving entries reported as compromised"

    def test_only_matching_names_are_treated_as_segments(self, sel_dir, small_segments):
        """Retention must never delete a file an operator parked in the dir."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        stray = sel_dir / "security_events.d" / "operator-notes.txt"
        stray.write_text("keep me", encoding="utf-8")
        _fill(log, 600, start=600)
        assert stray.exists(), "retention deleted a non-segment file"
        assert stray not in log._segments_oldest_first()

    def test_rotation_failure_still_writes_the_event(self, sel_dir, small_segments, caplog):
        """An audit record must never be lost to a rotation that cannot happen."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = len(log.recent(limit=10_000))
        with patch("kiro_crew.sel.os.replace", side_effect=OSError("boom")):
            _fill(log, 300, start=1000)
        after = log.recent(limit=10_000)
        assert len(after) > before, "events were dropped when rotation failed"
        assert after[0]["resources"] == "seq=1299"

    def test_segment_dir_is_owner_only(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert segment_dir.is_dir()
        if os.name != "nt":  # Windows uses the DACL, not a POSIX mode
            assert oct(segment_dir.stat().st_mode & 0o777) == "0o700"

    def test_prune_ages_out_whole_segments(self, sel_dir, small_segments):
        """Age retention must reach the segments, not just the live log.

        Otherwise a rotated segment could outlive the retention window
        indefinitely, because only the live file was ever rewritten.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(200):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                    resources=f"old={i}",
                )
            )
        assert log._segments_oldest_first(), "precondition: rotation happened"
        removed = log.prune(keep_days=365)
        assert removed > 0
        assert log._segments_oldest_first() == [], "aged-out segments survived the sweep"

    def test_prune_keeps_a_segment_that_straddles_the_cutoff(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # 2026 timestamps — inside any sane retention window
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        log.prune(keep_days=365 * 100)
        assert log._segments_oldest_first() == segments


class TestRotationIsSerializedAcrossProcesses:
    """Rotation runs under a cross-process lock, taken without blocking.

    ``_lock`` is a thread lock, but several processes share one data home (the
    gateway, the CLI, cron) and append to the same file, so they reach the size
    cap at the same moment. Unserialized, two of them pick the same target name
    and the second ``os.replace`` moves the freshly recreated live log onto the
    segment the first just closed.
    """

    def test_the_acquire_never_blocks(self, sel_dir, small_segments):
        """A critical audit is written inline, sometimes on the event loop.

        Parking that caller on another process's rotation is the
        no-blocking-call-on-event-loop hazard, and rotation is deferrable while an
        audit write is not -- so the blocking lock helper must not be used here.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        with patch(
            "kiro_crew.sel.platform_compat.file_lock",
            side_effect=AssertionError("blocking lock helper used on the audit path"),
        ):
            _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"

    def test_the_rotation_step_runs_under_the_lock(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        lock_path = sel_dir / "security_events.d" / ".rotate.lock"
        held: list[bool] = []
        real_rotate = SecurityEventLog._rotate_under_lock

        def recording_rotate(inner_self):
            held.append(_lock_is_held(lock_path))
            return real_rotate(inner_self)

        with patch.object(SecurityEventLog, "_rotate_under_lock", recording_rotate):
            _fill(log, 200)
        assert held and all(held), "rotation ran without holding the cross-process lock"

    def test_below_the_cap_no_cross_process_lock_is_taken(self, sel_dir, small_segments):
        """The audit hot path must not pay for a lock it does not need."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        with patch("kiro_crew.sel.platform_compat.try_acquire_lock") as locker:
            _fill(log, 3)  # nowhere near the cap
        locker.assert_not_called()

    def test_contention_defers_rotation_without_a_stale_tip(self, sel_dir, small_segments):
        """Losing the lock must not mean appending from a pre-rotation tip.

        This is what makes skipping safe instead of merely fast: the contended
        path re-reads the live log's identity and re-anchors before the caller
        chains, so a rotation that already happened cannot orphan our record.

        The interleaving has to be exact, or the assertion passes for the wrong
        reason: at the top of the window we must still see the OLD file at the cap
        (the winner has not renamed yet, so no replacement is detectable there),
        and only the CONTENDED re-check may observe the swap. The identities are
        therefore synthesized -- first call same inode grown to the cap, second
        call a new inode -- so the top-of-window check cannot fire.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        stale_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"
        pre_dev, pre_ino, _pre_size = log._live_seen

        # A sibling has rotated and written its own first record (see the
        # foreign-rotation suite for why this is built by hand).
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
        sibling_event = _make_event(event_id="sib1", resources="sibling")
        sibling_event.prev_hash = ""
        sibling_event.entry_hash = log._compute_hash(sibling_event)
        live.write_text(json.dumps(asdict(sibling_event)) + "\n", encoding="utf-8")
        assert sibling_event.entry_hash != stale_tip

        log._live_seen = (pre_dev, pre_ino, _pre_size)
        log._last_hash = stale_tip
        with patch("kiro_crew.sel.platform_compat.try_acquire_lock", return_value=False):
            with patch.object(
                SecurityEventLog,
                "_live_identity",
                side_effect=[
                    # Top of window: same file, grown to the cap. NOT a replacement,
                    # so the fast-path re-anchor must not fire here.
                    (pre_dev, pre_ino, sel_mod._SEGMENT_MAX_BYTES),
                    # Contended re-check: the winner's rename is now visible.
                    (pre_dev, pre_ino + 1, 512),
                ],
            ):
                with log._rotation_window():
                    pass
        assert log._last_hash == sibling_event.entry_hash, (
            "kept a pre-rotation tip after deferring on contention"
        )

    def test_a_serialization_failure_still_writes_the_event(self, sel_dir, small_segments):
        """An unusable lock must not cost an audit record."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = log._segments_oldest_first()
        with patch.object(SecurityEventLog, "_open_rotation_lock", return_value=None):
            _fill(log, 50, start=3000)
        assert log._segments_oldest_first() == before, "rotated without the lock"
        assert log.recent(limit=1)[0]["resources"] == "seq=3049"

    def test_the_lock_file_is_never_treated_as_a_segment(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 600)
        lock = sel_dir / "security_events.d" / ".rotate.lock"
        assert lock.exists(), "rotation did not take the cross-process lock"
        assert lock not in log._segments_oldest_first()
        total, valid = log.verify_integrity()
        assert total == valid, "the lock file was verified as an audit segment"

    def test_a_planted_lock_link_is_not_opened_through(self, sel_dir, small_segments):
        """Opening the mutex CREATES and chmods it, so a link must not be followed."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        protected = sel_dir / "protected.json"
        protected.write_text('{"keep": "me"}\n', encoding="utf-8")
        before = protected.read_bytes()
        planted = segment_dir / ".rotate.lock"
        planted.unlink(missing_ok=True)
        try:
            planted.symlink_to(protected)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")

        _fill(log, 200, start=500)

        assert protected.read_bytes() == before, "rotation wrote through the planted link"
        assert not planted.is_symlink(), "the planted link survived"

    def test_an_unremovable_lock_link_declines_to_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before = log._segments_oldest_first()
        with patch("kiro_crew.sel.platform_compat.is_link_or_junction") as is_link:
            # The segment DIR check must still pass; only the lock path is linked.
            is_link.side_effect = lambda p: Path(p).name == ".rotate.lock"
            with patch(
                "kiro_crew.sel.platform_compat.unlink_link_or_junction",
                side_effect=OSError("read-only"),
            ):
                _fill(log, 300, start=4000)
        assert log._segments_oldest_first() == before, "rotated through a linked lock"
        assert log.recent(limit=1)[0]["resources"] == "seq=4299"

    def test_a_rotation_lost_to_another_process_re_anchors_the_chain(
        self, sel_dir, small_segments
    ):
        """Winning the lock but finding the log already rotated must re-anchor.

        The cached tip points at a record that a sibling process has just moved
        into a closed segment; appending from it would leave the new live log's
        first record naming a ``prev_hash`` that is not in the same file.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        stale_tip = log._last_hash

        # Stand in for the sibling process: force one real rotation, so the live
        # log is a fresh file whose tip differs from what we cached.
        with patch.object(
            SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES
        ):
            log._rotate_under_lock()
        fresh_tip = log._last_hash
        assert fresh_tip not in ("", stale_tip)
        assert fresh_tip == log._read_last_hash()

        # Our process still holds the pre-rotation tip and, holding the lock,
        # finds the log already small — the lost-the-race branch.
        log._last_hash = stale_tip
        with patch.object(SecurityEventLog, "_live_size", return_value=0):
            log._rotate_under_lock()
        assert log._last_hash == fresh_tip, "kept a chain tip from a closed segment"


class TestSegmentDirIsPinnedOnRead:
    """The segment DIRECTORY is pinned for the duration of a read (#4999).

    #4998 validated the descriptor of each file the readers open, which closes
    the FINAL path component; the directory itself was still walked BY NAME.
    A ``security_events.d`` replaced with a link (planted before this release,
    or swapped while a read is in flight) therefore redirected enumeration —
    and every per-file open — into another tree, whose segment-shaped REGULAR
    files pass every per-file check, because they are regular files. The read
    paths now pin the directory itself first and refuse a linked one, so a
    swapped dir fails closed instead of feeding another tree's files to
    ``recent()`` / ``verify_integrity()``. The rotation-time repair
    (``_ensure_segment_dir``) remains the write-side guard, and the live log
    is unchanged: its writer follows an operator's symlink, so its readers
    must too.
    """

    def test_a_swapped_segment_dir_is_not_read_through(self, sel_dir, small_segments):
        """A linked ``security_events.d`` must fail closed, not follow.

        The observable is ``verify_integrity()``'s totals: without the pin, the
        decoy's unsigned lines inflate ``total`` while ``valid`` stays flat —
        the false tamper alarm of #4999 — so ``total == valid`` here is exactly
        the property that must hold. Built with ``symlink_or_junction`` so the
        same attack runs on Windows junctions, which need no elevation.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        segment_dir = sel_dir / "security_events.d"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text(
            '{"planted": true}\n' * 3, encoding="utf-8"
        )
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))

        total, valid = log.verify_integrity()
        assert total == valid, (
            f"a swapped segment dir surfaced {total - valid} decoy lines as "
            "audit history (false tamper alarm)"
        )
        live_lines = (
            (sel_dir / "security_events.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()
        )
        assert total == len(live_lines), "records beyond the live log were counted"
        # The swap is itself tampering, so the detailed result must not call
        # this run verifiable over the live log alone (#5051 review).
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False
        assert "refused" in result.reason

    def test_a_refusal_is_not_reclassified_by_a_later_repair(
        self, sel_dir, small_segments, monkeypatch
    ):
        """The refusal verdict must come from pin time, not a re-stat.

        A linked segment dir is refused, and rotation's repair concurrently
        removes the link before verify classifies the outcome: re-deriving
        the answer from the path would now see a real directory again and
        report the run verifiable while the rotated history went unchecked.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        segment_dir = sel_dir / "security_events.d"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))

        real_open = sel_mod._open_segment_dir

        def repairing_open(path):
            pin, absent = real_open(path)
            if pin is None and not absent and not segment_dir.exists():
                # The repair races in behind the refusal: the real directory
                # is back by the time verify would have re-stat'ed the path.
                # The exists() guard makes the race idempotent — the repair
                # fires exactly once, whatever later pin attempt arrives.
                os.rename(sel_dir / "aside.d", segment_dir)
            return pin, absent

        monkeypatch.setattr(sel_mod, "_open_segment_dir", repairing_open)
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False, (
            "a concurrent repair reclassified the refusal as verifiable"
        )

    def test_a_fresh_install_stays_verifiable(self, sel_dir):
        """No segment dir yet is not tampering — nothing to vouch for."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 3)
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is True
        assert result.reason == ""
        assert result.total == result.valid

    def test_a_healthy_rotation_is_verifiable(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is True
        assert result.reason == ""
        assert result.total == result.valid

    @pytest.mark.skipif(os.name == "nt", reason="the fd branch that opens the dir is POSIX-only")
    def test_a_dir_vanishing_after_lstat_is_a_refusal(self, sel_dir, small_segments, monkeypatch):
        """Vanishing BETWEEN the lstat and the pin is interference, not absence.

        Fresh-install silence belongs to the directory that was NEVER there;
        one that was seen by lstat and then disappears before the open
        cannot be used to let the rotated history read as unverifiable-but-
        absent.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        real_open = os.open

        def vanishing_open(path, *args, **kwargs):
            if getattr(path, "name", str(path)).endswith("security_events.d"):
                raise FileNotFoundError(errno.ENOENT, "gone", str(path))
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(sel_mod.os, "open", vanishing_open)
        result = log.verify_integrity(detailed=True)
        assert (
            result.history_verifiable is False
        ), "a directory that vanished after lstat read as absent"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
    def test_an_unreadable_segment_dir_is_not_verifiable(self, sel_dir, small_segments):
        """A real directory the pin could not open is still unchecked history.

        Permissions changed under us, or the platform refused the open for
        any other reason: the rotated segments were skipped either way, and
        reporting the run as intact over the live log alone is the false
        negative the third outcome exists to prevent.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        before = segment_dir.stat().st_mode
        segment_dir.chmod(0o000)
        try:
            result = log.verify_integrity(detailed=True)
        finally:
            segment_dir.chmod(before)
        assert result.history_verifiable is False
        assert "refused" in result.reason

    def test_a_mid_verification_swap_is_not_verifiable(
        self, sel_dir, small_segments, monkeypatch
    ):
        """A directory replaced mid-run leaves totals from the pinned tree,
        but the tree on disk no longer is it — the detail must say so."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        monkeypatch.setattr(sel_mod._SegmentDirPin, "matches", lambda self, path: False)
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False
        assert "replaced during verification" in result.reason

    @pytest.mark.skipif(os.name == "nt", reason="dir-fd pinning is POSIX-only")
    def test_enumeration_is_immune_to_a_swapped_path_with_the_pin_alive(
        self, sel_dir, small_segments
    ):
        """POSIX: the walk goes through the pinned descriptor.

        Swapping the PATH (to a decoy link, and back again — the ABA shape:
        plant the decoy mid-read, restore the real directory before any
        recheck) cannot redirect, empty, or confuse the enumeration, because
        it reads RELATIVE to the fd. Every real name must survive.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        real_names = {p.name for p in log._segments_oldest_first()}
        assert real_names, "precondition: rotation happened"
        pin, absent = sel_mod._open_segment_dir(segment_dir)
        assert pin is not None, "the real segment dir must pin"
        assert absent is False, "a real segment dir is not absence"
        assert pin.fd is not None, "POSIX must pin by descriptor, not identity"
        try:
            decoy = sel_dir / "decoy.d"
            decoy.mkdir()
            (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text(
                "", encoding="utf-8"
            )
            os.rename(segment_dir, sel_dir / "aside.d")
            (sel_dir / "security_events.d").symlink_to(decoy)
            names = {p.name for p in log._segments_oldest_first(pin=pin)}
            assert names == real_names, (
                "enumeration leaked names through a swapped path"
            )
            fd = sel_mod._open_segment(segment_dir / sorted(real_names)[0], pin=pin)
            assert fd is not None, "the pinned descriptor refused a real segment after the swap"
            os.close(fd)
        finally:
            pin.close()

    @pytest.mark.skipif(
        os.name == "nt", reason="identity pins are the Windows-only degenerate form"
    )
    def test_an_identity_pin_fails_closed_on_a_mid_read_swap(
        self, sel_dir, small_segments, monkeypatch
    ):
        """Without directory descriptors the walk is by name, so a swap that
        survives the post-walk identity revalidation must fail closed — the
        identity the read pinned is not the identity the path names anymore.

        Forces the identity branch the way Windows takes it naturally: the
        platform gate claims no descriptors, and the pin carries only an
        identity.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segment_dir = sel_dir / "security_events.d"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        decoy = sel_dir / "decoy.d"
        decoy.mkdir()
        (decoy / "security_events-000042-20260821T000000Z.jsonl").write_text(
            "", encoding="utf-8"
        )
        pin = sel_mod._SegmentDirPin(fd=None, identity=(0, 0))
        monkeypatch.setattr(sel_mod, "_open_segment_dir", lambda path: (pin, False))
        os.rename(segment_dir, sel_dir / "aside.d")
        platform_compat.symlink_or_junction(str(decoy), str(segment_dir))
        assert log._segments_oldest_first(pin=pin) == [], (
            "enumeration surfaced names through a swapped segment dir"
        )
        result = log.verify_integrity(detailed=True)
        assert result.history_verifiable is False


class TestAppendValidatesTheFileByFd:
    """A lock-free append must notice a rotation that lands under it.

    The append is deliberately not serialized (a blocking cross-process acquire
    could park an event-loop caller writing a critical audit), so instead it
    validates the file it OPENED. A path stat cannot do this: whatever it observed
    may be replaced before the open that follows.
    """

    def test_a_rotation_between_chaining_and_opening_is_re_chained(
        self, sel_dir, small_segments
    ):
        """The exact interleaving: we chain, a sibling renames, then we open."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        real_open = os.open
        rotated: list[bool] = []

        def rotating_open(path, flags, *args, **kwargs):
            # Fire once, at the moment of the append's open: the sibling's rename
            # and its own first record land between our chaining and our write.
            if not rotated and str(path) == str(live):
                rotated.append(True)
                os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
                sibling = _make_event(event_id="sibfd", resources="sibling")
                sibling.prev_hash = ""
                sibling.entry_hash = log._compute_hash(sibling)
                live.write_text(json.dumps(asdict(sibling)) + "\n", encoding="utf-8")
            return real_open(path, flags, *args, **kwargs)

        with patch("kiro_crew.sel.os.open", rotating_open):
            _fill(log, 1, start=7000)

        assert rotated, "precondition: the injected rotation fired"
        lines = live.read_text(encoding="utf-8").strip().splitlines()
        records = [json.loads(line) for line in lines]
        ours = [r for r in records if r.get("resources") == "seq=7000"]
        assert ours, "our record was lost"
        sibling_rec = next(r for r in records if r.get("resources") == "sibling")
        assert ours[0]["prev_hash"] == sibling_rec["entry_hash"], (
            "record chained off a tip that lives in the closed segment"
        )
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_holding_the_pre_rename_inode_still_writes_there(self, sel_dir, small_segments):
        """Matching identity means our record rides into the segment, chained.

        The other correct outcome: the rename has not happened yet, so the fd we
        hold is the file we chained from and the write is valid whether or not a
        rename follows it.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        tip_before = log._last_hash
        _fill(log, 1, start=8000)
        line = (
            (sel_dir / "security_events.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()[-1]
        )
        assert json.loads(line)["prev_hash"] == tip_before

    def test_sustained_contention_refuses_rather_than_writing_a_bad_link(
        self, sel_dir, small_segments
    ):
        """Every attempt is validated, including the last.

        Writing the final attempt unguarded would put a knowingly-broken link in
        the chain, and that does not read as one imperfect record -- it makes
        `security verify` report the log as COMPROMISED, which an investigator
        cannot tell apart from real tampering. Refusing is the behaviour this class
        already has for an unwritable audit.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        tip_before = log._last_hash
        live = sel_dir / "security_events.jsonl"
        before = live.read_text(encoding="utf-8")

        calls: list[int] = []

        def always_colliding(inner_self, lines, *, expect=None):
            calls.append(1)
            assert expect is not None, "an attempt was made without the fd guard"
            raise sel_mod._LiveLogReplaced("forced collision")

        with patch.object(SecurityEventLog, "_append_lines_locked", always_colliding):
            with pytest.raises(sel_mod.SelChainContention):
                log._append_chained_locked([_make_event(event_id="cont0")])

        assert len(calls) == sel_mod._APPEND_RETRIES + 1
        assert live.read_text(encoding="utf-8") == before, "a record was written anyway"
        assert log._last_hash == tip_before, "the chain tip was left advanced"

    def test_contention_is_an_oserror_so_critical_writes_fail_closed(
        self, sel_dir, small_segments
    ):
        """A critical caller must be able to deny the action it could not audit.

        Keeps ``base_dir``: the critical path writes inline and never calls
        ``_ensure_writer``, so no daemon writer is bound to this per-test dir --
        asserted below rather than assumed.
        """
        assert issubclass(sel_mod.SelChainContention, OSError)
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        with patch.object(
            SecurityEventLog,
            "_append_lines_locked",
            side_effect=sel_mod._LiveLogReplaced("forced collision"),
        ):
            with pytest.raises(OSError):
                log.log(_make_event(event_id="cont1"), critical=True)
        assert log._writer is None, "a writer thread was started after all"

    def test_contention_does_not_kill_the_background_writer(self, small_segments):
        """Best-effort path: the batch is dropped with a warning, thread survives."""
        log = SecurityEventLog(sync=False)
        _fill(log, 2)
        log.flush()
        with patch.object(
            SecurityEventLog,
            "_append_lines_locked",
            side_effect=sel_mod._LiveLogReplaced("forced collision"),
        ):
            log.log(_make_event(event_id="cont2", resources="dropped"))
            log.flush()
        # Writer still alive and still serving later events.
        assert log._writer is not None and log._writer.is_alive()
        _fill(log, 1, start=950)
        log.flush()
        assert any(e["resources"] == "seq=950" for e in log.recent(limit=20))

    def test_the_guard_covers_both_replacement_and_a_foreign_append(self):
        """Inode catches a swap; size catches an append the inode cannot see.

        The size signal is what covers the fresh-log case: a rotator anchored on an
        ABSENT replacement has no inode to compare, so only the size reveals that
        another process already put a record there -- and without it both would
        write a record claiming genesis.
        """

        class _St:
            def __init__(self, dev, ino, size):
                self.st_dev = dev
                self.st_ino = ino
                self.st_size = size

        changed = sel_mod._identity_changed
        # Same file, same size: unchanged.
        assert changed((1, 5, 10), _St(1, 5, 10)) is False
        # Same file, grown by somebody else: changed.
        assert changed((1, 5, 10), _St(1, 5, 40)) is True
        # Replaced file: changed.
        assert changed((1, 5, 10), _St(1, 6, 10)) is True
        # No usable inode on either side -- size still decides.
        assert changed((1, 0, 10), _St(1, 0, 10)) is False
        assert changed((1, 0, 10), _St(1, 0, 40)) is True
        # Anchored on an absent replacement; another process wrote first.
        assert changed((0, 0, 0), _St(1, 7, 512)) is True
        # Anchored on an absent replacement; still absent/empty.
        assert changed((0, 0, 0), _St(1, 7, 0)) is False


class TestSegmentEnumerationIsBounded:
    def test_enumeration_stops_at_the_scan_cap(self, sel_dir, small_segments, monkeypatch):
        """This walk runs on the append path, where a critical audit may be inline.

        An unbounded listing of a directory somebody else filled would stall that
        caller (no-blocking-call-on-event-loop), so the cost must be capped rather
        than scale with the directory.
        """
        monkeypatch.setattr(sel_mod, "_SEGMENT_SCAN_CAP", 8)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        for i in range(200):
            (segment_dir / f"planted-{i:04d}.txt").write_text("x", encoding="utf-8")

        examined = 0
        real_scandir = os.scandir

        class _CountingScandir:
            """Wraps os.scandir, counting entries the caller actually pulls."""

            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()
                return False

            def __iter__(self):
                return self

            def __next__(self):
                nonlocal examined
                entry = next(self._it)
                examined += 1
                return entry

        with patch("kiro_crew.sel.os.scandir", _CountingScandir):
            log._segments_oldest_first()

        assert examined <= sel_mod._SEGMENT_SCAN_CAP + 1, (
            f"walked {examined} entries with a cap of {sel_mod._SEGMENT_SCAN_CAP}; "
            "enumeration scales with a directory an agent could have filled"
        )

    def test_the_cap_does_not_hide_the_real_segments(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 400)
        assert len(log._segments_oldest_first()) == sel_mod._SEGMENT_KEEP


class TestRetentionStopsAtAnUndeletableSegment:
    def test_a_failed_oldest_delete_does_not_eat_newer_history(
        self, sel_dir, small_segments, monkeypatch
    ):
        """Skipping past it would trade newer evidence for stuck older evidence.

        The un-deletable segment keeps occupying a retention slot either way; what
        must not happen is deleting the segments BEHIND it to make room.

        Rotation enforces retention as it goes, so a plain fill leaves exactly
        ``_SEGMENT_KEEP`` segments and no excess to sweep -- the branch under test
        would never run. Accumulate under a LARGER keep, then tighten it, so the
        sweep has real excess and a real choice about which files to delete.
        """
        monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 6)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 700)
        accumulated = log._segments_oldest_first()
        assert len(accumulated) == 6, f"precondition: expected 6 segments, got {len(accumulated)}"

        monkeypatch.setattr(sel_mod, "_SEGMENT_KEEP", 3)
        assert len(accumulated) - sel_mod._SEGMENT_KEEP == 3, "precondition: 3 to sweep"
        oldest = accumulated[0]
        real_unlink = Path.unlink

        def refuse_oldest(inner_self, *args, **kwargs):
            if inner_self == oldest:
                raise OSError("permission denied")
            return real_unlink(inner_self, *args, **kwargs)

        with patch.object(Path, "unlink", refuse_oldest):
            deleted = log._enforce_segment_retention_locked()

        assert deleted == 0, "deleted something despite the oldest being stuck"
        survivors = log._segments_oldest_first()
        for kept in accumulated:
            assert kept in survivors, (
                f"{kept.name} was deleted to make room for a segment that could not "
                "be removed -- newer audit history traded for older"
            )


class TestASiblingsAppendDoesNotReadAsCorruption:
    """Another process appending is exactly HOW the log crosses the cap.

    An append makes the file GROW, which is legitimate, so the replacement check
    stays deliberately silent while our cached tip goes stale. An earlier revision
    put a predecessor-hash claim in the rotation record and that stale tip made
    verification report an untampered log as compromised; the claim is gone (see
    ``_rotation_event``), and this pins that the benign case stays clean.
    """

    def test_rotating_after_a_sibling_append_verifies_clean(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        our_cached_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"

        # A sibling appends one correctly-chained record. Written directly: going
        # through the API would update OUR view of the file.
        sibling = _make_event(event_id="sibtip", resources="sibling")
        sibling.prev_hash = our_cached_tip
        sibling.entry_hash = log._compute_hash(sibling)
        with open(live, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(sibling)) + "\n")
        assert log._last_hash == our_cached_tip, "precondition: our tip is now stale"

        # _rotate_under_lock is called directly, so create the dir its caller
        # would normally have ensured.
        (sel_dir / "security_events.d").mkdir(parents=True, exist_ok=True)
        with patch.object(
            SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES
        ):
            log._rotate_under_lock()

        opener = json.loads(live.read_text(encoding="utf-8").splitlines()[0])
        assert opener["event_type"] == "sel_rotation"
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries reported as compromised"


class TestSegmentNameProbingIsBounded:
    def test_probing_stops_and_defers_rotation(self, sel_dir, small_segments, monkeypatch):
        """Each probe is a stat on the append path; a filled directory must not
        make rotation walk it."""
        monkeypatch.setattr(sel_mod, "_SEGMENT_NAME_PROBES", 4)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        probes = 0
        real_exists = Path.exists

        def counting_exists(inner_self, *args, **kwargs):
            nonlocal probes
            if inner_self.parent == segment_dir and inner_self.name.startswith(
                "security_events-"
            ):
                probes += 1
                return True  # every candidate name is taken
            return real_exists(inner_self, *args, **kwargs)

        with patch.object(Path, "exists", counting_exists):
            assert log._next_segment_path() is None, "did not defer on an exhausted probe"
        assert probes == 4, f"probed {probes} times with a cap of 4"

    def test_an_exhausted_probe_still_writes_the_event(self, sel_dir, small_segments):
        """Deferring means the live log is NOT renamed and the record still lands.

        Asserted on the live log's identity rather than on the segment list: a
        rotation into a low-sequence name would be deleted by retention as the
        oldest segment in the same sweep, erasing its own evidence from the list
        while the live log had still been moved out from under us.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        live = sel_dir / "security_events.jsonl"
        identity_before = (live.stat().st_dev, live.stat().st_ino)
        with patch.object(SecurityEventLog, "_next_segment_path", return_value=None):
            _fill(log, 50, start=9500)
        assert (live.stat().st_dev, live.stat().st_ino) == identity_before, (
            "the live log was renamed even though no free segment name was found"
        )
        assert log.recent(limit=1)[0]["resources"] == "seq=9549"
        total, valid = log.verify_integrity()
        assert total == valid

    def test_a_free_name_is_still_found_normally(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "rotation never found a name"


class TestCriticalWritesDoNotRotateInline:
    """A critical audit runs on its caller's thread, sometimes the event loop.

    Doing rotation's filesystem work there -- a directory scan, a rename, a
    retention sweep -- would stall the loop. Rotation is deferrable; an audit write
    is not, so the inline path writes and leaves rotation to the background writer.

    These are the only tests here that exercise the ASYNC writer, so they omit
    ``base_dir`` and use the session-scoped SEL directory from the rootdir
    ``_isolate_sel_default_dir`` fixture. That is the repo convention for a reason
    specific to this class: the writer is a daemon thread on a process singleton,
    and against a per-test ``tmp_path`` it outlives the test and RE-CREATES the
    directory on its next flush (``_flush_batch`` mkdirs), so a stray directory
    reappears after the test's own cleanup removed it. Assertions here are all
    relative to what the shared directory already holds.
    """

    def test_a_critical_write_skips_the_rotation_window(self, small_segments):
        log = SecurityEventLog(sync=False)
        # Fill past the cap through the background writer, then flush so the log
        # is genuinely over budget when the critical write arrives.
        _fill(log, 200)
        log.flush()
        segments_before = log._segments_oldest_first()

        entered: list[str] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            entered.append("yes")
            return real_window(inner_self)

        with patch.object(SecurityEventLog, "_rotation_window", recording_window):
            log.log(_make_event(event_id="crit0", resources="critical"), critical=True)
        assert entered == [], "a critical inline write entered the rotation window"
        # The record still landed -- that is the half that is not deferrable.
        assert any(e["resources"] == "critical" for e in log.recent(limit=20))
        assert log._segments_oldest_first() == segments_before

    def test_the_background_writer_still_rotates(self, small_segments):
        """Deferring must not mean never.

        Batches, not one shot, for two reasons that are both harness properties
        of the async writer: the window is entered once per batch and reads the
        size ALREADY on disk -- a batch sees the file below the cap and correctly
        does not rotate, which is the documented one-batch overshoot -- and
        ``flush()`` waits on the pending counter with a bounded timeout and
        returns on expiry, so under load a batch may not be on disk when the
        test looks. The test therefore polls for the condition with a bounded
        budget instead of asserting after a fixed number of batches
        (testing-conventions § Determinism).

        The observable is the highest segment SEQUENCE NUMBER, not the segment
        count. Rotation ends with a retention sweep, and this class runs against
        the shared session dir, so how many segments it already holds depends on
        which tests ran earlier on this worker: at ``_SEGMENT_KEEP`` segments a
        successful rotation adds one and the sweep deletes the oldest in the same
        breath, leaving the COUNT flat -- a real rotation that a count comparison
        calls "never rotated" (#5017). The sequence only ever rises
        (``_next_segment_path`` continues from the highest segment still on
        disk, precisely so retention cannot make it go backwards), so it
        observes the rotation no matter what the sweep did.
        """
        log = SecurityEventLog(sync=False)
        _fill(log, 200)
        log.flush()

        def highest_seq() -> int:
            segments = log._segments_oldest_first()
            return sel_mod._segment_seq(segments[-1]) if segments else 0

        seq = _fill_until_over_cap(log, deadline=time.monotonic() + 30.0, start=500)

        # Fresh budget for the phase actually under test, so a slow precondition
        # cannot eat the writer's rotation window and misattribute the failure.
        deadline = time.monotonic() + 30.0
        before = highest_seq()
        iterations = 0
        while highest_seq() <= before:
            iterations += 1
            assert iterations <= _POLL_ITERATION_CAP and time.monotonic() < deadline, (
                f"background writer never rotated: highest segment sequence "
                f"stayed at {before} after {iterations - 1} batches, with the "
                f"live log at {log._live_size()} bytes (cap "
                f"{sel_mod._SEGMENT_MAX_BYTES}), "
                f"{len(log._segments_oldest_first())} segments on disk, "
                f"{log._pending} events still pending in the writer"
            )
            _fill(log, 5, start=seq)
            seq += 5
            log.flush()

    def test_a_critical_write_still_fails_closed(self, sel_dir, small_segments):
        """Skipping rotation must not weaken audit-or-deny.

        Keeps ``base_dir``: the append is patched to raise, so no event is ever
        enqueued and the daemon writer is never started -- nothing outlives this
        test to re-create the directory.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        with patch.object(
            SecurityEventLog, "_append_lines_locked", side_effect=OSError("full disk")
        ):
            with pytest.raises(OSError):
                log.log(_make_event(event_id="crit1"), critical=True)
        assert log._writer is None, "a writer thread was started after all"


class TestBackwardReadBufferIsBounded:
    def test_an_unterminated_file_does_not_accumulate_without_bound(
        self, sel_dir, small_segments, monkeypatch
    ):
        """A planted segment with no newline would otherwise be held in memory.

        This reader is pointed at attacker-influenced input: a segment planted
        before this release is read here by both the time-range read and segment
        admission.
        """
        monkeypatch.setattr(sel_mod, "_MAX_LINE_BYTES", 64 * 1024)
        monkeypatch.setattr(sel_mod, "_TAIL_CHUNK_BYTES", 8 * 1024)
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        planted = segment_dir / "security_events-000001-20200101T000000Z.jsonl"
        planted.write_bytes(b"A" * (1024 * 1024))  # 1 MiB, not one newline in it

        yielded = list(log._iter_lines_backward(planted))
        assert yielded == [], "an unterminated file yielded a line"
        # Admission must simply fail, not blow up.
        assert log._segment_is_signed_by_us(planted) is False
        pin, _absent = sel_mod._open_segment_dir(segment_dir)
        assert pin is not None, "the real segment dir must pin"
        try:
            assert planted not in list(log._read_sources_newest_first(pin=pin))
        finally:
            pin.close()

    def test_a_normal_log_is_read_completely(self, sel_dir, small_segments):
        """The cap must not truncate ordinary records."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 40)
        live = sel_dir / "security_events.jsonl"
        expected = len(live.read_text(encoding="utf-8").strip().splitlines())
        assert len(list(log._iter_lines_backward(live))) == expected


class TestTwoWritersCannotBothClaimGenesis:
    """The rotation record must not assume it lands first in the fresh log.

    After the rename the replacement is absent, and another process appending sees
    a small file -- so it never enters the rotation window and never takes the
    rotation lock. If the rotator then wrote a genesis record blindly, the new log
    would hold TWO records claiming an empty prev_hash and its chain would be
    broken from the second line.
    """

    def test_a_writer_that_beat_the_rotator_is_chained_off(self, sel_dir, small_segments):
        """Driven through _rotate_under_lock, not through its helpers.

        The sibling's write is injected at the real interleaving point -- inside the
        rename -- so the rotator's own boundary-record path is what is under test.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)

        real_replace = os.replace
        raced: list[str] = []

        def replace_then_race(src, dst, *args, **kwargs):
            result = real_replace(src, dst, *args, **kwargs)
            if not raced:
                raced.append("yes")
                # A sibling process creates the replacement and puts the FIRST
                # record in it, before the rotator can write its own.
                first = _make_event(event_id="first0", resources="beat-the-rotator")
                first.prev_hash = ""
                first.entry_hash = log._compute_hash(first)
                live.write_text(json.dumps(asdict(first)) + "\n", encoding="utf-8")
            return result

        with patch("kiro_crew.sel.os.replace", replace_then_race):
            with patch.object(
                SecurityEventLog, "_live_size", return_value=sel_mod._SEGMENT_MAX_BYTES
            ):
                log._rotate_under_lock()

        assert raced, "precondition: the injected sibling write fired"
        rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines()]
        assert [r["event_type"] for r in rows] == ["tool_invocation", "sel_rotation"]
        assert rows[0]["prev_hash"] == ""
        assert rows[1]["prev_hash"] == rows[0]["entry_hash"], (
            "the rotation record claimed genesis in a log that already had a record"
        )
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_uncontended_rotation_still_puts_the_record_first(self, sel_dir, small_segments):
        """The common case is unchanged: nothing raced, so the record opens the log."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        opener = json.loads(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert opener["event_type"] == "sel_rotation"
        assert opener["prev_hash"] == ""


class TestCollisionRetryReanchorsUnconditionally:
    """A collision has already proved the file moved on.

    The retry must not re-ask a predicate to decide whether to refresh the tip. An
    earlier revision routed it through the conditional check, and because that check
    reacted only to a shrink while the guard fired on growth too, a foreign APPEND
    was detected and then not corrected -- the retry re-chained from the very tip
    the collision had just invalidated and wrote it.
    """

    def test_a_foreign_append_is_re_chained_not_re_written_stale(
        self, sel_dir, small_segments
    ):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        live = sel_dir / "security_events.jsonl"
        stale_tip = log._last_hash

        # A sibling appends one correctly-chained record: the file GROWS, which is
        # what the old shrink-only predicate refused to react to.
        sibling = _make_event(event_id="grow0", resources="sibling-grew")
        sibling.prev_hash = stale_tip
        sibling.entry_hash = log._compute_hash(sibling)
        with open(live, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(sibling)) + "\n")
        assert log._last_hash == stale_tip, "precondition: our cached tip is stale"

        _fill(log, 1, start=6000)

        rows = [json.loads(line) for line in live.read_text(encoding="utf-8").splitlines()]
        ours = rows[-1]
        assert ours["resources"] == "seq=6000"
        assert ours["prev_hash"] == sibling.entry_hash, (
            "wrote a stale prev_hash after the collision was detected"
        )
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed verification"

    def test_reanchor_now_refreshes_without_asking(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        real_tip = log._last_hash
        log._last_hash = "0" * 64  # pretend we hold something stale
        log._reanchor_now()
        assert log._last_hash == real_tip
        assert log._live_seen == log._live_identity()


class TestOnlyTheWriterThreadRotates:
    """Rotation's filesystem work must never land on an inline caller's thread.

    A critical audit and the writer-start fallback both reach ``_flush_batch``
    inline, and for an async handler that thread is the event loop. The permission
    is derived from the running thread so a future inline caller cannot reintroduce
    the stall by forgetting a flag.
    """

    def test_sync_mode_may_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log._may_rotate() is True

    def test_an_inline_caller_may_not_rotate(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=False)
        # No writer thread started yet, and this is the test's own thread.
        assert log._may_rotate() is False

    def test_the_writer_thread_may_rotate(self, small_segments):
        """Verified from INSIDE the writer, not by reasoning about it."""
        log = SecurityEventLog(sync=False)
        seen: list[bool] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            seen.append(inner_self._may_rotate())
            return real_window(inner_self)

        with patch.object(SecurityEventLog, "_rotation_window", recording_window):
            _fill(log, 5)
            log.flush()
        assert seen and all(seen), "the writer thread was denied rotation"

    def test_the_writer_start_fallback_does_not_rotate(self, small_segments):
        """The path this finding was about: _ensure_writer fails, we write inline.

        That inline write happens on whatever thread called log() -- possibly the
        event loop -- so it must not scan, rename and sweep.

        Omits ``base_dir`` for the session-scoped SEL directory: ``_fill`` starts
        the daemon writer, and against a per-test ``tmp_path`` that thread outlives
        the test and re-creates the directory after cleanup. Assertions are relative
        to whatever the shared directory already holds.
        """
        log = SecurityEventLog(sync=False)
        # Get the log over the cap first, through a path that is allowed to
        # rotate. Topped up rather than asserted after one fixed fill: the same
        # two harness properties that broke the rotation test (#5017) -- a
        # bounded flush() and a mid-fill batch-boundary rotation -- can leave
        # the live log below the cap here too.
        _fill(log, 200)
        log.flush()
        _fill_until_over_cap(log, deadline=time.monotonic() + 30.0, start=700)
        before = log._segments_oldest_first()

        entered: list[str] = []
        real_window = SecurityEventLog._rotation_window

        def recording_window(inner_self):
            entered.append("yes")
            return real_window(inner_self)

        with patch.object(SecurityEventLog, "_ensure_writer", side_effect=RuntimeError("no thread")):
            with patch.object(SecurityEventLog, "_rotation_window", recording_window):
                log.log(_make_event(event_id="fb0", resources="fallback"))

        assert entered == [], "the writer-start fallback rotated on the caller's thread"
        assert log._segments_oldest_first() == before
        # The record still landed: that is the half that is not deferrable.
        assert any(e["resources"] == "fallback" for e in log.recent(limit=20))


class TestAsyncTestsUseTheSessionScopedDirectory:
    """Ratchet for a convention I have now broken twice on this PR.

    A ``sync=False`` log started by ``_fill`` runs a DAEMON writer thread on a
    process singleton. Bound to a per-test ``tmp_path`` it outlives the test and
    re-creates the directory on its next flush, so a stray directory reappears
    after cleanup (see the rootdir ``_isolate_sel_default_dir`` fixture). A test
    that starts the writer must therefore omit ``base_dir``.

    Asserted structurally rather than by discipline, because discipline already
    failed: it was applied in one round and violated in the next.
    """

    def test_no_writer_starting_test_pins_a_per_test_dir(self):
        import ast

        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, node) or ""
            constructs_async = "sync=False" in body and "base_dir=" in body
            starts_writer = "_fill(" in body or ".log(" in body
            if constructs_async and starts_writer:
                # A test may keep base_dir if it proves no writer was started.
                if "_writer is None" in body:
                    continue
                offenders.append(node.name)
        assert offenders == [], (
            "these tests build an async SecurityEventLog on a per-test dir AND write "
            f"through it, leaking a daemon writer bound to that dir: {offenders}. "
            "Omit base_dir to use the session-scoped SEL directory."
        )


class TestPruneCannotClobberAConcurrentRotation:
    """prune's read-then-replace is the one path that can lose persisted events.

    ``_lock`` is a thread lock, so it says nothing about a sibling process. If one
    rotates while prune is streaming, prune's ``os.replace`` drops a snapshot of the
    OLD file over the fresh live log -- discarding its rotation record and every
    event appended since.
    """

    def test_prune_takes_the_rotation_lock(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(20):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                    resources=f"old={i}",
                )
            )
        _fill(log, 5, start=700)  # recent entries that must survive
        lock_path = sel_dir / "security_events.d" / ".rotate.lock"

        held: list[bool] = []
        real_prune_live = SecurityEventLog._prune_live_locked

        def recording(inner_self, cutoff, keep_days):
            held.append(_lock_is_held(lock_path))
            return real_prune_live(inner_self, cutoff, keep_days)

        with patch.object(SecurityEventLog, "_prune_live_locked", recording):
            removed = log.prune(keep_days=365)
        assert removed > 0, "precondition: something was pruned"
        assert held and all(held), "prune rewrote the live log without the rotation lock"

    def test_prune_skips_the_live_sweep_when_it_cannot_serialize(
        self, sel_dir, small_segments
    ):
        """Skipping beats an unserialized rewrite: the latter loses events."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(20):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                )
            )
        live = sel_dir / "security_events.jsonl"
        before = live.read_text(encoding="utf-8")
        with patch.object(SecurityEventLog, "_open_rotation_lock", return_value=None):
            log.prune(keep_days=365)
        assert live.read_text(encoding="utf-8") == before, "swept without the lock"

    def test_prune_re_anchors_after_replacing_the_live_log(self, sel_dir, small_segments):
        """The replace changes the inode, so the cached identity must follow.

        Note what this does NOT assert. Rewriting the live log orphans the first
        SURVIVING record's ``prev_hash`` -- it names a record prune just deleted --
        so a pruned log carries exactly one chain break. That is pre-existing
        behaviour of the in-place rewrite, not something rotation introduced, and it
        cannot be repaired by editing the survivor: ``prev_hash`` is covered by the
        record's own HMAC, so changing it would mean re-signing audit records. The
        property that must hold is that prune introduces NO FURTHER break -- our
        next append chains off the rewritten file's real tip.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        base = datetime(2020, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            log.log(
                _make_event(
                    event_id=f"old{i:04d}",
                    timestamp=(base + timedelta(seconds=i)).isoformat(),
                )
            )
        _fill(log, 3, start=800)
        assert log.prune(keep_days=365) > 0
        assert log._live_seen == log._live_identity()
        assert log._last_hash == log._read_last_hash()

        total_before, valid_before = log.verify_integrity()
        orphaned = total_before - valid_before
        assert orphaned == 1, (
            f"expected exactly the known orphaned-first-survivor break, got {orphaned}"
        )

        _fill(log, 1, start=900)
        total_after, valid_after = log.verify_integrity()
        assert total_after - valid_after == orphaned, (
            "the post-prune append introduced a NEW chain break"
        )
        last = json.loads(
            (sel_dir / "security_events.jsonl")
            .read_text(encoding="utf-8")
            .strip()
            .splitlines()[-1]
        )
        assert last["resources"] == "seq=900"


class TestSegmentDirIsNotFollowedThroughALink:
    """Neither the segment dir nor a segment name may be a planted link.

    Before this release ``security_events.d`` was not on the sensitive-path
    floor, so an agent could have pre-planted either. Same defense as the SEL
    trust dir: remove the LINK, never its target.
    """

    def test_a_planted_link_is_replaced_by_a_real_directory(self, sel_dir, small_segments):
        outside = sel_dir.parent / "agent-readable"
        outside.mkdir()
        link = sel_dir / "security_events.d"
        sel_dir.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert not link.is_symlink(), "rotation wrote through a planted link"
        assert log._segments_oldest_first(), "precondition: rotation happened"
        assert list(outside.iterdir()) == [], "audit segments landed outside the fence"

    def test_an_unremovable_link_refuses_to_rotate(self, sel_dir, small_segments):
        """Refusing beats writing audit records outside the fence.

        The log keeps growing, which is the failure this release bounds — but an
        oversized log inside the fence beats a bounded one outside it.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        with patch("kiro_crew.sel.platform_compat.is_link_or_junction", return_value=True):
            with patch(
                "kiro_crew.sel.platform_compat.unlink_link_or_junction",
                side_effect=OSError("read-only"),
            ):
                before = log._segments_oldest_first()
                _fill(log, 300, start=2000)
                assert log._segments_oldest_first() == before, "rotated through the link"
        # The audit records still landed in the live log.
        assert log.recent(limit=1)[0]["resources"] == "seq=2299"

    def test_a_planted_segment_link_is_never_read_as_audit_history(
        self, sel_dir, small_segments
    ):
        """A segment-shaped SYMLINK must not be surfaced as events.

        Every reader resolves segments by name, and `recent()` backs the
        dashboard's SEL events endpoint -- so a link planted before the upgrade
        pointing at, say, the refresh-chain store would have its JSON handed back
        as audit records.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # creates the real segment dir
        secret = sel_dir / "refresh_chains.json"
        secret.write_text(json.dumps({"chain": "s3cr3t-refresh-token"}) + "\n", encoding="utf-8")
        planted = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        try:
            planted.unlink(missing_ok=True)
            planted.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")
        assert planted.is_symlink()

        assert planted not in log._segments_oldest_first(), "a planted link ranked as a segment"
        surfaced = json.dumps(log.recent(limit=10_000))
        assert "s3cr3t-refresh-token" not in surfaced, "linked file's content surfaced as events"
        # The link's target must survive: we ignore the entry, we do not chase it.
        assert secret.exists()

    def test_a_segment_shaped_directory_is_ignored(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        bogus = sel_dir / "security_events.d" / "security_events-000002-20200101T000000Z.jsonl"
        bogus.mkdir(parents=True, exist_ok=True)
        assert bogus not in log._segments_oldest_first()
        total, valid = log.verify_integrity()
        assert total == valid


class TestSegmentOpensValidateTheDescriptor:
    """The scan->open window is closed at OPEN time, not just at scan time.

    ``_segments_oldest_first`` judges the DIRENT; a link or FIFO planted after
    that judgment must still be refused by the open itself. ``_open_segment``
    opens ``O_NOFOLLOW | O_NONBLOCK``, requires the DESCRIPTOR to be a regular
    file, and requires the opened identity to be exactly what the name points
    at — so both consumers (``_verify_file`` for verify_integrity,
    ``_iter_lines_backward`` for recent) are exercised here with the planted
    path handed to them DIRECTLY, as if it had passed the scan. Hardlink
    ALIASES are deliberately not deduplicated (a cross-name decision keyed on
    attacker-controlled names lets a plant displace real history); the
    displacement test locks that invariant.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_symlink_is_refused_by_both_consumers(self, sel_dir, small_segments):
        """O_NOFOLLOW (and the lstat identity check where it is absent) is what
        catches this: a followed link to a regular file yields a perfectly
        regular descriptor, so fstat alone would pass it."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)  # rotation creates the real segment dir
        secret = sel_dir / "outside.jsonl"
        secret.write_text(json.dumps({"chain": "s3cr3t-outside"}) + "\n", encoding="utf-8")
        planted = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        planted.unlink(missing_ok=True)
        try:
            planted.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform/filesystem")

        assert log._verify_file(planted) == (0, 0), "linked file counted as audit history"
        assert list(log._iter_lines_backward(planted)) == [], "linked file's lines surfaced"
        # End to end: nothing surfaced by the readers, nothing counted as valid.
        assert "s3cr3t-outside" not in json.dumps(log.recent(limit=10_000))
        total, valid = log.verify_integrity()
        assert total == valid
        # The link's target survives: the entry is ignored, never chased.
        assert secret.exists()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_an_alias_plant_cannot_displace_newer_history(self, sel_dir, small_segments):
        """A LOWER-sequence hardlink of the newest segment must not displace
        the real name from the read order. Aliases are deliberately NOT
        deduplicated — every key such a decision could use (which name sorts
        first, which survives) is attacker-controlled in this directory, so a
        dedupe hands the plant the power to hide or reorder real history. An
        alias's worst case without dedupe is repeating already-signed records;
        it can never displace, hide, or forge them."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert len(segments) > 1, "precondition: several segments"
        newest = segments[-1]
        alias = sel_dir / "security_events.d" / "security_events-000000-20000101T000000Z.jsonl"
        alias.unlink(missing_ok=True)
        os.link(newest, alias)  # newest content under the lowest-sorting name

        listed = log._segments_oldest_first()
        assert newest in listed, "the real newest segment was displaced by its alias"
        alias_pin, _absent = sel_mod._open_segment_dir(sel_dir / "security_events.d")
        assert alias_pin is not None, "the real segment dir must pin"
        try:
            order = list(log._read_sources_newest_first(pin=alias_pin))
        finally:
            alias_pin.close()
        assert newest in order, "the real newest segment vanished from the read order"
        assert order.index(newest) < order.index(alias), (
            "the alias outranked the real newest segment in newest-first reads"
        )
        total, valid = log.verify_integrity()
        assert total == valid, "alias duplication broke signature validity"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink semantics")
    def test_a_backup_hardlink_does_not_suppress_audit_history(self, sel_dir, small_segments):
        """Backup tools (rsync --link-dest, cp -al, rsnapshot) hardlink audit
        files from OUTSIDE the segment dir. A link count above one must not
        refuse the file: history keeps reading and verifying, and retention
        keeps moving — refusing here silently blanked real audit history while
        verify_integrity still reported clean."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        before_recent = len(log.recent(limit=10_000))
        before = log.verify_integrity()
        backups = sel_dir / "backup"
        backups.mkdir()
        os.link(sel_dir / "security_events.jsonl", backups / "live.jsonl")
        for i, seg in enumerate(log._segments_oldest_first()):
            os.link(seg, backups / f"seg{i}.jsonl")

        assert len(log.recent(limit=10_000)) == before_recent, "backup links suppressed history"
        assert log.verify_integrity() == before

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
    def test_a_fifo_does_not_block_and_is_skipped(self, sel_dir, small_segments):
        """O_NONBLOCK is load-bearing: without it the read-side open of a FIFO
        with no writer blocks forever, BEFORE any descriptor check is reachable
        — this test hangs, loudly, if that flag is dropped."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        fifo = sel_dir / "security_events.d" / "security_events-000001-20200101T000000Z.jsonl"
        fifo.unlink(missing_ok=True)
        os.mkfifo(fifo)

        assert log._verify_file(fifo) == (0, 0), "a FIFO counted as audit history"
        assert list(log._iter_lines_backward(fifo)) == [], "a FIFO yielded lines"
        total, valid = log.verify_integrity()
        assert total == valid

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_a_symlinked_live_log_stays_readable(self, sel_dir, small_segments):
        """An operator may relocate the live log via symlink. The WRITER
        follows it (O_CREAT | O_APPEND open), so the readers must too — a
        reader that refuses the link turns the live log write-only: events
        keep landing while recent() reports empty and verify_integrity()
        counts a clean (0, 0)."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 20)
        live = sel_dir / "security_events.jsonl"
        relocated = sel_dir / "relocated.jsonl"
        os.replace(live, relocated)
        live.symlink_to(relocated)
        _fill(log, 5, start=1000)  # the writer keeps appending through the link

        recent = log.recent(limit=10_000)
        assert any(
            e.get("resources") == "seq=1004" for e in recent
        ), "reads went dark through the linked live log"
        total, valid = log.verify_integrity()
        assert total == valid
        assert total > 0

    def test_regular_segments_read_identically_through_the_funnel(
        self, sel_dir, small_segments
    ):
        """The guard must not change what a REAL segment verifies or yields."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        seg = segments[0]
        expected = [
            line.strip().decode("utf-8")
            for line in reversed(seg.read_bytes().splitlines())
            if line.strip()
        ]
        assert list(log._iter_lines_backward(seg)) == expected
        total, valid = log.verify_integrity()
        assert total == valid
        assert total > 0


class TestPlantedSegmentsAreNotReadAsHistory:
    """A forged segment must never surface through the read path.

    ``security_events.d`` is created by this release and was not on the
    sensitive-path floor before it, so an agent could have pre-created the
    directory and left a segment-shaped JSONL of its own choosing. Admission is
    by SIGNATURE: the chain key lives outside the log directory, so a planted
    file cannot carry a single valid one.
    """

    @staticmethod
    def _plant(sel_dir: Path, seq: int = 1) -> Path:
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        planted = segment_dir / f"security_events-{seq:06d}-20200101T000000Z.jsonl"
        forged = {
            "event_id": "forged0000000001",
            "timestamp": "2020-01-01T00:00:00+00:00",
            "event_type": "tool_invocation",
            "caller_identity": "dashboard:victim",
            "agent": "kirocrew",
            "source": "dashboard",
            "operation": "rm -rf /",
            "outcome": "approved",
            "resources": "FORGED-MARKER",
            "prev_hash": "",
            "entry_hash": "0" * 64,
            "metadata": {},
        }
        planted.write_text(json.dumps(forged) + "\n", encoding="utf-8")
        return planted

    def test_recent_never_returns_unsigned_records(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        self._plant(sel_dir)
        surfaced = json.dumps(log.recent(limit=10_000))
        assert "FORGED-MARKER" not in surfaced, "forged records were presented as audit events"

    def test_a_genuine_segment_is_still_read(self, sel_dir, small_segments):
        """The admission gate must not hide real history."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        segments = log._segments_oldest_first()
        assert segments, "precondition: rotation happened"
        assert all(log._segment_is_signed_by_us(p) for p in segments)
        # A window reaching back into the segments returns their events.
        reached = log.recent(limit=10_000, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(reached) > len(
            (sel_dir / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
        )

    def test_verify_integrity_reports_a_planted_segment_instead_of_hiding_it(
        self, sel_dir, small_segments
    ):
        """An audit tool must surface tampering, not quietly drop the evidence."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        self._plant(sel_dir)
        total, valid = log.verify_integrity()
        assert total > valid, "the planted segment was hidden from verification"

    def test_an_empty_segment_is_not_admitted(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        empty = segment_dir / "security_events-000009-20200101T000000Z.jsonl"
        empty.write_text("", encoding="utf-8")
        assert log._segment_is_signed_by_us(empty) is False

    def test_segments_are_not_opened_when_the_live_log_answers(self, sel_dir, small_segments):
        """The bounded tail read must not pay for segment admission."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        assert log._segments_oldest_first(), "precondition: segments exist"
        with patch.object(
            SecurityEventLog,
            "_segment_is_signed_by_us",
            side_effect=AssertionError("segment opened for a tail read"),
        ):
            assert len(log.recent(limit=3)) == 3


class TestForeignRotationDoesNotBreakOurChain:
    """Another process's rotation must not leave us chaining into a closed segment.

    Several processes share one data home and append to this file, each caching
    its own tip. A process whose next append comes AFTER a foreign rotation does
    not see the cap at all, so it never enters the rotation window -- and would
    chain off a tip that has moved into the closed segment. Unlike the
    pre-existing interleaving race, that break is guaranteed for every process
    after every foreign rotation.
    """

    def test_a_replaced_live_log_re_anchors_before_the_next_append(
        self, sel_dir, small_segments
    ):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        stale_tip = log._last_hash
        live = sel_dir / "security_events.jsonl"
        # What WE last saw. The sibling's work below must not update it: the whole
        # point is that this process never observed the replacement.
        pre_rotation_identity = log._live_seen

        # Stand in for the sibling process: rotate the log out of band and write
        # one genuine record (correctly signed, chained from genesis) into the
        # fresh live log. Written directly rather than through the SEL API because
        # SecurityEventLog is a singleton -- a second construction would hand back
        # THIS instance and update its view of the file.
        segment_dir = sel_dir / "security_events.d"
        segment_dir.mkdir(parents=True, exist_ok=True)
        os.replace(live, segment_dir / "security_events-000001-20260821T000000Z.jsonl")
        sibling_event = _make_event(event_id="sib0", resources="sibling")
        sibling_event.prev_hash = ""
        sibling_event.entry_hash = log._compute_hash(sibling_event)
        live.write_text(json.dumps(asdict(sibling_event)) + "\n", encoding="utf-8")
        sibling_tip = sibling_event.entry_hash
        assert sibling_tip != stale_tip

        # Our process still holds the pre-rotation tip and is nowhere near the cap,
        # so it never enters the rotation window at all.
        log._live_seen = pre_rotation_identity
        log._last_hash = stale_tip
        _fill(log, 1, start=9000)

        lines = live.read_text(encoding="utf-8").strip().splitlines()
        ours = json.loads(lines[-1])
        assert ours["resources"] == "seq=9000"
        assert ours["prev_hash"] == sibling_tip, (
            "chained off a tip that now lives in the closed segment"
        )
        total, valid = log.verify_integrity()
        assert total == valid, f"{total - valid} entries failed after a foreign rotation"

    def test_an_append_only_log_that_merely_grew_is_not_treated_as_replaced(self, sel_dir):
        """Growth is the normal case; re-reading the tip on it would be pure cost.

        Deliberately NOT using the shrunken-cap fixture: under the production cap
        these appends cannot trigger a rotation, so the only thing that could read
        the tip back is a spurious replacement verdict.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        assert log._segments_oldest_first() == [], "precondition: no rotation"
        with patch.object(
            SecurityEventLog,
            "_read_last_hash",
            side_effect=AssertionError("re-anchored on a plain append"),
        ):
            _fill(log, 5, start=100)
        total, valid = log.verify_integrity()
        assert total == valid


class TestLiveLogMovedOnHelper:
    """One predicate serves the pre-append check and the post-open guard.

    Answering the same question differently in those two places is how a stale tip
    survived a collision: the guard fired on growth while the re-anchor reacted only
    to a shrink, so a foreign APPEND was detected and then not corrected.
    """

    @pytest.mark.parametrize(
        ("previous", "current", "expected"),
        [
            ((1, 10, 500), (1, 10, 500), False),  # unchanged
            # Growth is somebody ELSE appending: our own writes refresh the anchor
            # from the fd, so a size difference always means a foreign write.
            ((1, 10, 500), (1, 10, 900), True),
            ((1, 10, 500), (1, 11, 40), True),  # new inode: replaced
            ((1, 10, 500), (1, 10, 40), True),  # shrank: append-only cannot
            ((1, 0, 500), (1, 0, 40), True),  # no inode available, size decides
            ((1, 0, 500), (1, 0, 500), False),  # no inode available, unchanged
            ((1, 10, 500), (2, 10, 40), True),  # same ino, different device
            ((0, 0, 0), (1, 7, 512), True),  # anchored absent, somebody wrote
            ((0, 0, 0), (1, 7, 0), False),  # anchored absent, still empty
        ],
    )
    def test_moved_on_signals(self, previous, current, expected):
        assert sel_mod._live_log_moved_on(previous, current) is expected

    def test_the_guard_delegates_to_the_same_predicate(self):
        """``_identity_changed`` must not be a second, drifting implementation."""

        class _St:
            def __init__(self, dev, ino, size):
                self.st_dev = dev
                self.st_ino = ino
                self.st_size = size

        for previous, dev, ino, size in [
            ((1, 10, 500), 1, 10, 500),
            ((1, 10, 500), 1, 10, 900),
            ((1, 10, 500), 1, 11, 500),
            ((0, 0, 0), 1, 7, 512),
        ]:
            assert sel_mod._identity_changed(previous, _St(dev, ino, size)) is (
                sel_mod._live_log_moved_on(previous, (dev, ino, size))
            )


class TestTimeWindowRead:
    """``recent()`` takes a time window, and reads only what it needs.

    A count alone could not express "the last two hours": on a busy log 6000
    entries reached 15 minutes back, so a two-hour question meant pulling ~90k
    entries and filtering client-side (issue #4843).
    """

    def test_since_and_until_bound_the_window(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 500)
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(
            limit=100, since=base + timedelta(seconds=100), until=base + timedelta(seconds=110)
        )
        assert [e["resources"] for e in got] == [f"seq={i}" for i in range(109, 99, -1)]

    def test_until_is_exclusive_and_since_inclusive(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 20)
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(
            limit=100, since=base + timedelta(seconds=5), until=base + timedelta(seconds=6)
        )
        assert [e["resources"] for e in got] == ["seq=5"]

    def test_window_reads_across_a_segment_boundary(self, sel_dir, small_segments):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 120)
        assert log._segments_oldest_first(), "precondition: rotation happened"
        base = datetime(2026, 8, 21, tzinfo=timezone.utc)
        got = log.recent(limit=500, since=base + timedelta(seconds=110))
        # Rotation records carry their own (wall-clock) timestamp and are real
        # audit entries, so they legitimately fall in the window too.
        events = [e for e in got if e["event_type"] != "sel_rotation"]
        assert [e["resources"] for e in events] == [f"seq={i}" for i in range(119, 109, -1)]

    def test_limit_still_caps_a_windowed_read(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 200)
        got = log.recent(limit=4, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert len(got) == 4
        assert got[0]["resources"] == "seq=199"

    def test_no_window_reads_the_tail_unchanged(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 50)
        assert [e["resources"] for e in log.recent(limit=3)] == ["seq=49", "seq=48", "seq=47"]

    def test_a_bounded_read_does_not_load_the_whole_file(self, sel_dir):
        """The read cost must not scale with the log's size.

        The old ``recent()`` did ``read_text().splitlines()``, so printing 20
        entries allocated the entire log — 4.09 GB on the reported install.
        """
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 4000)
        live = sel_dir / "security_events.jsonl"
        size = live.stat().st_size
        assert size > sel_mod._TAIL_CHUNK_BYTES, "precondition: log exceeds one chunk"
        read_bytes = 0
        real_read = os.read

        def counting_read(fd, n):
            nonlocal read_bytes
            chunk = real_read(fd, n)
            read_bytes += len(chunk)
            return chunk

        with patch("kiro_crew.sel.os.read", counting_read, create=True):
            with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
                assert len(log.recent(limit=5)) == 5

    def test_records_with_unreadable_timestamps_are_excluded_from_a_window(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        log.log(_make_event(event_id="bad", timestamp="not-a-time", resources="seq=bad"))
        _fill(log, 5)
        assert any(e["resources"] == "seq=bad" for e in log.recent(limit=50))
        windowed = log.recent(limit=50, since=datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert all(e["resources"] != "seq=bad" for e in windowed)

    def test_zero_limit_reads_nothing(self, sel_dir):
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        _fill(log, 5)
        assert log.recent(limit=0) == []


def _shutdown_writer(log: SecurityEventLog) -> None:
    """Deterministically stop *log*'s background writer inside the test.

    An async SecurityEventLog binds its directory into a daemon writer thread;
    without an explicit shutdown the thread outlives the test while holding a
    reference to a deleted per-test tmp_path. Flush, send the shutdown
    sentinel, and join so nothing survives teardown.
    """
    log.flush()
    log._queue.put(None)
    writer = log._writer
    if writer is not None:
        writer.join(timeout=5)


class TestMetadataRedaction:
    """The writer redacts caller-supplied metadata values before persistence.

    The acceptance is "never reaches disk": every assertion here reads the raw
    on-disk JSONL back, not a mock. The token is assembled from parts so this
    test file itself never contains a credential-shaped literal.
    """

    AKIA_TOKEN = "AKIA" + "IOSFODNN7EXAMPLE"
    EXFIL_URL = "https://attacker.example/upload/" + "AKIA" + "IOSFODNN7EXAMPLE"

    def _disk_text(self, sel_dir: Path) -> str:
        return (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")

    def test_credential_in_metadata_never_reaches_disk(self, log, sel_dir):
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_skills",
            outcome="success",
            metadata={"query": f"find {self.AKIA_TOKEN} docs", "result_count": "3"},
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["metadata"]["query"]
        # Non-secret values pass through untouched.
        assert data["metadata"]["result_count"] == "3"

    def test_exfiltration_url_in_metadata_never_reaches_disk(self, log, sel_dir):
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_mcp_servers",
            outcome="success",
            metadata={"query": self.EXFIL_URL},
        )
        raw = self._disk_text(sel_dir)
        assert self.EXFIL_URL not in raw
        assert self.AKIA_TOKEN not in raw

    def test_nested_metadata_values_are_redacted(self, log, sel_dir):
        log.log(_make_event(
            event_id="nested1",
            metadata={
                "outer": {"inner": self.AKIA_TOKEN},
                "items": [self.AKIA_TOKEN, 7, None],
            },
        ))
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        # Keys and non-string values are untouched.
        assert "outer" in data["metadata"] and "inner" in data["metadata"]["outer"]
        assert data["metadata"]["items"][1] == 7
        assert data["metadata"]["items"][2] is None

    def test_caller_metadata_dict_is_not_mutated(self, log):
        caller_metadata = {"query": self.AKIA_TOKEN, "nested": {"k": self.AKIA_TOKEN}}
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="discover_skills",
            outcome="success",
            metadata=caller_metadata,
        )
        assert caller_metadata["query"] == self.AKIA_TOKEN
        assert caller_metadata["nested"]["k"] == self.AKIA_TOKEN

    def test_redacted_events_chain_verifies(self, log):
        """The HMAC chain signs the redacted bytes, so verify stays green."""
        for i in range(3):
            log.log(_make_event(
                event_id=f"chain{i}",
                metadata={"query": f"{self.AKIA_TOKEN} #{i}"},
            ))
        total, valid = log.verify_integrity()
        assert total == 3
        assert valid == 3

    def test_async_writer_path_redacts(self, tmp_path: Path) -> None:
        """The production background-writer path redacts too, not just sync."""
        log = SecurityEventLog(base_dir=tmp_path)  # async (default)
        try:
            log.log_tool_invocation(
                session_key="dashboard:slot1",
                tool_name="discover_skills",
                outcome="success",
                metadata={"query": self.AKIA_TOKEN},
            )
            log.flush()
            raw = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8")
            assert self.AKIA_TOKEN not in raw
        finally:
            _shutdown_writer(log)

    def test_events_without_metadata_are_untouched(self, log, sel_dir):
        log.log(_make_event(event_id="plain1"))
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["metadata"] == {}

    def test_namedtuple_metadata_value_does_not_cost_the_event(self, log, sel_dir):
        """A sequence subclass with a different constructor (json-legal today)
        must not raise out of the walker; it degrades to a plain array."""
        from collections import namedtuple

        point = namedtuple("point", ["x", "y"])
        log.log(_make_event(
            event_id="nt1",
            metadata={"pos": point(self.AKIA_TOKEN, 2)},
        ))
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert data["metadata"]["pos"][1] == 2

    def test_redaction_failure_persists_placeholder_not_raw(self, log, sel_dir):
        """Fail-closed containment: if the redactor raises, the event lands
        with placeholder metadata and the raw text never reaches disk."""
        with patch(
            "kiro_crew.sel._redacted_metadata_copy",
            side_effect=RuntimeError("simulated redactor failure"),
        ):
            log.log(_make_event(
                event_id="rf1",
                metadata={"query": self.AKIA_TOKEN},
            ))
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert data["metadata"] == {"redaction_error": "RuntimeError"}

    def test_redaction_failure_does_not_drop_batch_siblings(self, tmp_path: Path) -> None:
        """One pathological metadata value must not cost unrelated events
        queued in the same writer batch."""
        log = SecurityEventLog(base_dir=tmp_path)  # async: events batch together

        def _flaky(metadata: dict) -> dict:
            if "poison" in metadata:
                raise RuntimeError("simulated redactor failure")
            return {k: v for k, v in metadata.items()}

        try:
            with patch("kiro_crew.sel._redacted_metadata_copy", side_effect=_flaky):
                log.log(_make_event(event_id="ok1", metadata={"query": "fine"}))
                log.log(_make_event(event_id="bad1", metadata={"poison": "x"}))
                log.log(_make_event(event_id="ok2", metadata={"query": "also fine"}))
                log.flush()
        finally:
            _shutdown_writer(log)
        lines = (tmp_path / "security_events.jsonl").read_text(encoding="utf-8").splitlines()
        ids = [json.loads(ln)["event_id"] for ln in lines if ln.strip()]
        assert ids == ["ok1", "bad1", "ok2"]

    def test_forward_callback_uses_shared_walker_on_namedtuple(self, log):
        """The forward path shares the walker, so a namedtuple degrades to a
        plain array there too instead of raising."""
        from collections import namedtuple

        received: list[dict] = []
        log.set_forward_callback(received.append)
        point = namedtuple("point", ["x", "y"])
        log.log(_make_event(event_id="fw1", metadata={"pos": point(self.AKIA_TOKEN, 2)}))
        assert len(received) == 1
        assert received[0]["metadata"]["pos"][1] == 2
        assert self.AKIA_TOKEN not in json.dumps(received[0])

    def test_error_and_resources_fields_never_reach_disk_raw(self, log, sel_dir):
        """Top-level free-form strings get the same write-time pass: an
        exception message quoting a credential must not persist raw."""
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="execute_bash",
            outcome="failed",
            resources=f"curl -H 'X-Key: {self.AKIA_TOKEN}'",
            error=f"RuntimeError: auth failed for {self.AKIA_TOKEN}",
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["resources"]
        assert "[REDACTED: credential]" in data["error"]
        # The plain parts of the strings survive.
        assert data["error"].startswith("RuntimeError: auth failed for ")

    def test_identity_fields_stay_verbatim(self, log, sel_dir):
        """Identity-shaped fields are constrained vocabularies, not free text —
        the writer must not rewrite them."""
        log.log(_make_event(
            event_id="ident1",
            caller_identity="dashboard:slot-AKIA-not-a-key",
            downstream_service="kirocrew-core",
        ))
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["caller_identity"] == "dashboard:slot-AKIA-not-a-key"
        assert data["downstream_service"] == "kirocrew-core"

    def test_lowercased_aws_key_never_reaches_disk(self, log, sel_dir):
        """SEL callers may normalize case before logging (the file-search
        handler lowercases its query); a lowercased key is trivially
        reversible, so the write-time pass must catch it case-insensitively."""
        lowered = self.AKIA_TOKEN.lower()
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=f"q={lowered} kinds=all",
            metadata={"query": lowered},
        )
        raw = self._disk_text(sel_dir)
        assert lowered not in raw
        data = json.loads(raw.strip())
        assert "[REDACTED: credential]" in data["resources"]
        assert "[REDACTED: credential]" in data["metadata"]["query"]

    def test_anycase_pass_leaves_ordinary_words_alone(self, log, sel_dir):
        """The case-insensitive net is bounded: prose containing 'akia' or
        'asia' inside longer tokens must not be rewritten."""
        text = "asian markets akiary search results for East Asia region"
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=text,
            metadata={"query": text},
        )
        data = json.loads(self._disk_text(sel_dir).strip())
        assert data["resources"] == text
        assert data["metadata"]["query"] == text

    def test_credential_straddling_the_truncation_boundary_is_gone(self, log, sel_dir):
        """The log_* helpers clip free-form text to _MAX_ARG_LEN. Clipping FIRST
        would cut a credential in half and leave a prefix that no full-token
        grammar matches, so redaction must run on the whole string BEFORE the
        clip. Place the key so the clip lands mid-token."""
        from kiro_crew.sel import _MAX_ARG_LEN

        # Key starts 10 chars before the clip point, so a clip-first order would
        # keep its first 10 characters and drop the rest.
        prefix = "q=" + ("x" * (_MAX_ARG_LEN - 12))
        resources = prefix + self.AKIA_TOKEN + " kinds=all"
        assert len(prefix) < _MAX_ARG_LEN < len(prefix) + len(self.AKIA_TOKEN)
        log.log_tool_invocation(
            session_key="dashboard:slot1",
            tool_name="file_search",
            outcome="allowed",
            resources=resources,
            error=resources,
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        # No partial prefix of the key survives either.
        assert self.AKIA_TOKEN[:10] not in raw
        data = json.loads(raw.strip())
        # The replacement marker is longer than the token it replaces, so the
        # clip can cut the MARKER — harmless, and proof the order is right: a
        # marker fragment sits where the credential's first characters would be
        # under a clip-first order.
        assert "[REDACTED" in data["resources"]
        assert "[REDACTED" in data["error"]
        # The clip is still enforced.
        assert len(data["resources"]) <= _MAX_ARG_LEN
        assert len(data["error"]) <= _MAX_ARG_LEN

    def test_api_access_helper_also_redacts_before_clipping(self, log, sel_dir):
        from kiro_crew.sel import _MAX_ARG_LEN

        prefix = "path=" + ("y" * (_MAX_ARG_LEN - 15))
        log.log_api_access(
            caller="token:abc",
            operation="GET /api/file-search",
            outcome="allowed",
            resources=prefix + self.AKIA_TOKEN,
        )
        raw = self._disk_text(sel_dir)
        assert self.AKIA_TOKEN not in raw
        assert self.AKIA_TOKEN[:10] not in raw
        assert len(json.loads(raw.strip())["resources"]) <= _MAX_ARG_LEN
