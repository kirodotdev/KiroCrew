"""Tests for kiro_crew.sel — Security Event Log."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.sel import SecurityEvent, SecurityEventLog, _infer_source, sel, sel_hmac_key_path


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
