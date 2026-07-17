"""Tests for kiro_crew.sel — Security Event Log."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.sel import SecurityEvent, SecurityEventLog, _infer_source, sel


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


class TestHmacKeyManagement:
    def test_creates_key_file_on_first_init(self, sel_dir):
        SecurityEventLog(base_dir=sel_dir, sync=True)
        key_path = sel_dir / "sel_hmac.key"
        assert key_path.exists()
        assert len(key_path.read_bytes()) == 32

    def test_key_file_permissions(self, sel_dir):
        SecurityEventLog(base_dir=sel_dir, sync=True)
        key_path = sel_dir / "sel_hmac.key"
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
        lines = sel_file.read_text().strip().splitlines()
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
        data = json.loads(sel_file.read_text().strip())
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
        lines = sel_file.read_text().strip().splitlines()
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
        data = json.loads(sel_file.read_text().strip())
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
        data = json.loads(sel_file.read_text().strip())
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
        data = json.loads(sel_file.read_text().strip())
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
        lines = sel_file.read_text().strip().splitlines()
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
        remaining = sel_file.read_text().strip().splitlines()
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
        assert "cb2" in sel_file.read_text()


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
        lines = sel_file.read_text().strip().splitlines()
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
        with patch("kiro_crew.sel._DEFAULT_DIR", sel_dir):
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
        assert (tmp_path / "sel_hmac.key").exists()
        assert log._hmac_key

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
        lines = path.read_text().splitlines()
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
        path.write_text(path.read_text() + "\n\n   \n")
        total, valid = log.verify_integrity()
        assert total == 1 and valid == 1

    def test_handles_malformed_json(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event())
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text() + "not-json-at-all\n")
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
        path.write_text(path.read_text() + "garbage-line\n")
        events = log.recent()
        assert len(events) == 1
        assert events[0]["event_id"] == "good"

    def test_recent_skips_blank_lines(self, tmp_path: Path) -> None:
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.log(_make_event())
        path = tmp_path / "security_events.jsonl"
        path.write_text(path.read_text() + "\n   \n")
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
        path.write_text(path.read_text() + "not-json\n")
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
        lines = sel_file.read_text().strip().splitlines()
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
