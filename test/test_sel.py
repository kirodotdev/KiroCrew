"""Tests for kiro_crew.sel — Security Event Log."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.sel import (
    _MARKER_READ_CAP,
    SecurityEvent,
    SecurityEventLog,
    _infer_source,
    _open_segment,
    sel,
    sel_hmac_key_path,
)


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


def _segdir(sel_dir):
    """Sealed-segment subdirectory, created on demand.

    Sealed segments and the eviction marker live in ``<crew>/sel/`` rather than as
    dot-suffixed siblings of the active file, so the sensitive-path floor can cover
    the whole family with one registered directory. Tests that PLANT a segment need
    the directory to exist, hence the mkdir.
    """
    d = sel_dir / "sel"
    d.mkdir(parents=True, exist_ok=True)
    return d


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


def _authentic_line(log, **overrides) -> str:
    """A segment line whose ``entry_hash`` is the product's own MAC over its fields.

    Fixtures used to plant a literal like ``"entry_hash": "old"``, which was harmless
    while nothing authenticated a segment record. Age pruning now authenticates the
    record its timestamp came from before that stamp may authorise a delete, so a
    fabricated hash is -- correctly -- indistinguishable from a forged one.

    Built through the product's own ``_compute_hash`` and then self-checked against
    ``_record_is_authentic``, so this helper cannot drift from the digest it exists
    to satisfy: if the two ever disagree, the helper fails rather than silently
    producing fixtures that no longer represent legitimate records.
    """
    ev = _make_event(**overrides)
    ev.entry_hash = log._compute_hash(ev)
    record = asdict(ev)
    assert log._record_is_authentic(record), "helper built a record the product rejects"
    return json.dumps(record) + "\n"


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


# ── SEL rotation + retention ──


def _rot_log(sel_dir, *, max_bytes, backup_count=5, retention_days=365):
    """SEL with the rotation knobs set for a test.

    The knobs are instance attributes, not constructor kwargs: nothing in
    production passes them, so exposing them on the constructor would be public
    surface serving only these tests. Assigning after construction is equivalent —
    nothing in `_init_locked` consumes them, they are only read later by
    `_maybe_rotate` and `_prune_sealed_by_age`.
    """
    log = SecurityEventLog(base_dir=sel_dir, sync=True)
    log._max_bytes = max_bytes
    log._backup_count = backup_count
    log._retention_days = retention_days
    return log


def _all_log_files(sel_dir):
    """The active file plus every sealed segment, wherever they live.

    Sealed segments moved into ``<crew>/sel/``, so a glob of the crew dir alone now
    sees only the active file -- which reads as catastrophic data loss rather than
    as a stale helper.
    """
    files = [p for p in sel_dir.glob("security_events.jsonl*") if p.is_file()]
    files += [p for p in (sel_dir / "sel").glob("security_events.jsonl.*") if p.is_file()]
    return files


def _entries_across_segments(sel_dir) -> int:
    """Total non-blank entries across the active file and every sealed segment."""
    n = 0
    for p in _all_log_files(sel_dir):
        n += sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    return n


def _fill(log, n, *, ts=None, start=0):
    """Log *n* events, optionally at a fixed timestamp."""
    for i in range(start, start + n):
        kwargs = {"event_id": f"rot-{i:06d}"}
        if ts is not None:
            kwargs["timestamp"] = ts
        log.log(_make_event(**kwargs))


def _iso(days_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestRotationBasics:
    def test_active_file_seals_to_segment_one(self, sel_dir):
        # backup_count high enough that nothing is evicted, so the FIRST sealed
        # segment keeps its number. Monotonic numbering starts at 1 and only rises,
        # so `.1` is the OLDEST segment here -- under the shift-rename layout it was
        # always the newest, which is why this needs the eviction headroom.
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()
        assert (sel_dir / "security_events.jsonl").exists()

    def test_max_bytes_zero_disables_rotation(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 40)
        # NEGATIVE CONTROL for the test above: same event volume, rotation off.
        assert not (_segdir(sel_dir) / "security_events.jsonl.1").exists()

    def test_chain_verifies_unbroken_across_the_seam(self, sel_dir):
        # backup_count is set high enough that NOTHING is evicted, so `total`
        # accounts for every event written and the assertion below is about the
        # seam rather than about retention. Events measure ~489 bytes, so
        # max_bytes=1500 seals roughly every 4th event.
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 30)
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()  # rotation happened
        assert not (_segdir(sel_dir) / "evicted").exists()  # none evicted
        total, valid = log.verify_integrity()
        assert total == 30
        assert valid == total  # the seam must not read as a chain break

    def test_verify_counts_entries_from_every_segment(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 50)
        assert len(log._sealed_segments()) >= 2  # multi-segment precondition
        assert not (_segdir(sel_dir) / "evicted").exists()
        total, valid = log.verify_integrity()
        assert total == 50
        assert valid == 50

    def test_eviction_bounds_the_log_and_still_verifies(self, sel_dir):
        """The point of rotation: a small backup_count bounds total on-disk history."""
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=2)
        _fill(log, 60)
        total, valid = log.verify_integrity()
        assert 0 < total < 60, f"log was not bounded: total={total}"
        assert valid == total, "a bounded log must still verify clean"


class TestRotationChainTip:
    def test_read_last_hash_falls_back_to_sealed_segment(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        # The tip lives in the NEWEST sealed segment, which is the HIGHEST number.
        seg1 = log._segment_path(max(log._list_sealed_indices()))
        assert seg1.exists()
        # Simulate a post-rotation restart where the active file is empty.
        (sel_dir / "security_events.jsonl").write_text("", encoding="utf-8")
        expected = json.loads(seg1.read_text(encoding="utf-8").strip().splitlines()[-1])
        SecurityEventLog._instance = None
        SecurityEventLog._initialized = False
        fresh = _rot_log(sel_dir, max_bytes=400)
        assert fresh._last_hash == expected["entry_hash"]

    def test_tip_hash_of_skips_corrupt_tail_line(self, sel_dir):
        """The corrupt-tail recovery must survive being generalized per-segment."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        path = sel_dir / "security_events.jsonl"
        good_tip = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"truncated": ')  # crash residue, no newline
        assert log._tip_hash_of(path) == good_tip["entry_hash"]

    def test_tip_hash_of_returns_empty_for_absent_file(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        assert log._tip_hash_of(sel_dir / "nope.jsonl") == ""


class TestBackupCountEviction:
    def test_overflow_evicts_oldest_and_marks_evicted(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=2)
        _fill(log, 80)
        indices = log._list_sealed_indices()
        assert indices, "expected at least one sealed segment"
        # Monotonic numbers keep RISING, so the bound is on the COUNT of retained
        # segments, never on the highest index.
        assert len(indices) <= 2, f"backup_count=2 exceeded: {indices}"
        assert (_segdir(sel_dir) / "evicted").exists()

    def test_no_marker_before_any_eviction(self, sel_dir):
        """NEGATIVE CONTROL: rotation alone must NOT set the eviction marker."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=50)
        _fill(log, 20)
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()  # did rotate
        assert not (_segdir(sel_dir) / "evicted").exists()

    def test_backup_count_zero_reanchors_to_genesis(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 40)
        assert log._list_sealed_indices() == []
        assert not (_segdir(sel_dir) / "evicted").exists()
        total, valid = log.verify_integrity()
        assert valid == total  # genesis re-anchor must verify clean

    def test_lowered_backup_count_evicts_stale_higher_segments(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=3)
        # Hand-plant stale segments an operator left behind after lowering the knob.
        for idx in (4, 5):
            (_segdir(sel_dir) / f"security_events.jsonl.{idx}").write_text("{}\n", encoding="utf-8")
        _fill(log, 40)
        surviving = log._list_sealed_indices()
        assert len(surviving) <= 3, f"backup_count=3 exceeded: {surviving}"
        # The planted stale segments are the LOWEST numbers, so they are the ones
        # eviction drops first.
        assert 4 not in surviving and 5 not in surviving


class TestGenesisAnchorGate:
    def test_never_evicted_log_enforces_genesis(self, sel_dir):
        """Head-truncation on a never-evicted log must surface as a break."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        path = sel_dir / "security_events.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        path.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        total, valid = log.verify_integrity()
        assert valid < total, "genesis anchor was not enforced"

    def test_marker_relaxes_baseline_after_real_eviction(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=1)
        _fill(log, 80)
        assert (_segdir(sel_dir) / "evicted").exists()
        total, valid = log.verify_integrity()
        assert total > 0
        assert valid == total, "evicted-prefix baseline was not relaxed"

    def test_marker_only_gate_survives_rotation_disabled_after_eviction(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=1)
        _fill(log, 80)
        assert (_segdir(sel_dir) / "evicted").exists()
        # Operator turns rotation off afterwards; the physical chain still lacks
        # its genesis prefix, so the baseline must stay relaxed.
        log._max_bytes = 0
        total, valid = log.verify_integrity()
        assert valid == total


class TestIndexGapFailsLoud:
    def test_gap_makes_verify_report_valid_less_than_total(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=250, backup_count=9)
        _fill(log, 60)
        indices = log._list_sealed_indices()
        assert len(indices) >= 3, f"need >=3 segments, got {indices}"
        # Punch a MIDDLE hole. Removing the LOWEST number would not be a gap under
        # monotonic numbering -- that is what ordinary eviction leaves behind. A
        # missing middle segment is the real fault: the segment after it chains off
        # a deleted entry, so the mismatch must surface as valid<total.
        middle = indices[len(indices) // 2]
        (_segdir(sel_dir) / f"security_events.jsonl.{middle}").unlink()
        total, valid = log.verify_integrity()
        assert valid < total, "an index gap must never read as a clean chain"

    def test_no_gap_reads_clean(self, sel_dir):
        """NEGATIVE CONTROL for the gap test: contiguous segments verify clean."""
        log = _rot_log(sel_dir, max_bytes=250, backup_count=9)
        _fill(log, 60)
        total, valid = log.verify_integrity()
        assert valid == total


class TestAgeBasedSegmentPrune:
    def test_drops_whole_aged_segments(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        # Age DESCENDS with the number now: a higher number is a NEWER segment, so
        # .1 is the oldest. Under shift-renames this ordering was reversed.
        for idx, days in ((1, 500), (2, 400), (3, 2)):
            (_segdir(sel_dir) / f"security_events.jsonl.{idx}").write_text(
                _authentic_line(log, timestamp=_iso(days), event_id=f"seg-{idx}"),
                encoding="utf-8",
            )
        with log._lock:
            removed = log._prune_sealed_by_age(30)
        assert removed == 2  # .1 and .2 dropped (oldest-first prefix)
        assert log._list_sealed_indices() == [3]

    def test_keeps_segment_straddling_the_cutoff(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        # Authentic, so RECENCY is the only reason it survives. A fabricated hash
        # would now also keep it, leaving the test over-determined and unable to
        # tell a working cutoff from a working authentication check.
        (_segdir(sel_dir) / "security_events.jsonl.1").write_text(
            _authentic_line(log, timestamp=_iso(1), event_id="recent"),
            encoding="utf-8",
        )
        with log._lock:
            removed = log._prune_sealed_by_age(30)
        assert removed == 0
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()

    def test_fails_closed_on_unparseable_segment_timestamp(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        (_segdir(sel_dir) / "security_events.jsonl.1").write_text(
            json.dumps({"timestamp": "garbage"}) + "\n", encoding="utf-8"
        )
        with log._lock:
            removed = log._prune_sealed_by_age(30)
        assert removed == 0
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists(), "fail-closed violated"

    def test_keep_days_zero_disables_age_prune(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=0)
        # Authentic and ancient, so the ONLY thing standing between it and deletion
        # is the keep_days<=0 early return this test exists to pin.
        (_segdir(sel_dir) / "security_events.jsonl.1").write_text(
            _authentic_line(log, timestamp=_iso(9999), event_id="ancient"),
            encoding="utf-8",
        )
        with log._lock:
            removed = log._prune_sealed_by_age(0)
        assert removed == 0
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()

    def test_age_prune_marks_evicted(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        (_segdir(sel_dir) / "security_events.jsonl.1").write_text(
            _authentic_line(log, timestamp=_iso(400), event_id="old"),
            encoding="utf-8",
        )
        with log._lock:
            log._prune_sealed_by_age(30)
        assert (_segdir(sel_dir) / "evicted").exists()

    # ---- prune() Stage 1 must be serialized against SEALS, not just _lock ----
    #
    # Numbering is reused: once a prune empties the sealed set the next seal
    # allocates 1 again, so a path this stage proved aged can name a different,
    # brand-new, fully-populated segment by the time it is unlinked. `_lock` is a
    # threading.Lock and orders nothing against another writer PROCESS, and with
    # count=True the gap spans a whole ~100 MB read. Stage 1 therefore takes the
    # cross-process seal lease and skips itself when a rival holds it.

    @staticmethod
    def _hold_seal_lease(sel_dir):
        """Hold the seal lease on a separate fd, as another writer process would."""
        from kiro_crew import platform_compat
        from kiro_crew.sel import _SEAL_LOCK_FILE

        lock_path = _segdir(sel_dir) / _SEAL_LOCK_FILE
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(fd, exclusive=True), (
            "could not take the seal lease, so this test would prove nothing"
        )
        return fd

    def test_prune_skips_aged_segments_while_a_rival_holds_the_seal_lease(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        # Authentic, so the segment can only survive because the LEASE was held. A
        # fabricated hash would now also make it survive, which would leave this
        # test passing for a reason that has nothing to do with the lease.
        seg.write_text(
            _authentic_line(log, timestamp=_iso(400), event_id="aged"),
            encoding="utf-8",
        )
        fd = self._hold_seal_lease(sel_dir)
        try:
            removed = log.prune(keep_days=30)
        finally:
            from kiro_crew import platform_compat

            platform_compat.release_lock(fd)
            os.close(fd)
        assert seg.exists(), (
            "prune unlinked an aged segment while a rival held the seal lease; that "
            "rival may already have replaced the path with a newly sealed segment"
        )
        assert removed == 0, f"Stage 1 reported work it must have skipped: {removed}"

    def test_prune_still_drops_aged_segments_when_no_rival_holds_the_lease(self, sel_dir):
        """NEGATIVE CONTROL: taking the lease must not disable retention outright."""
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            _authentic_line(log, timestamp=_iso(400), event_id="aged"),
            encoding="utf-8",
        )
        removed = log.prune(keep_days=30)
        assert not seg.exists(), "retention stopped working once the lease was added"
        assert removed == 1, f"the aged entry was not counted as removed: {removed}"


class TestPruneTwoStage:
    def test_prune_defaults_to_configured_retention(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        log.log(_make_event(event_id="old-1", timestamp=_iso(400)))
        log.log(_make_event(event_id="new-1", timestamp=_iso(1)))
        removed = log.prune()  # no arg — must use retention_days=30, not 365
        assert removed == 1

    def test_explicit_keep_days_wins(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        log.log(_make_event(event_id="old-1", timestamp=_iso(400)))
        assert log.prune(keep_days=9999) == 0

    def test_keep_days_zero_does_not_wipe_active_log(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=0)
        _fill(log, 5)
        before = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
        assert log.prune() == 0
        assert (sel_dir / "security_events.jsonl").read_text(encoding="utf-8") == before

    def test_prune_drops_sealed_segments_and_active_entries(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        (_segdir(sel_dir) / "security_events.jsonl.1").write_text(
            _authentic_line(log, timestamp=_iso(400), event_id="old"),
            encoding="utf-8",
        )
        log.log(_make_event(event_id="stale", timestamp=_iso(400)))
        log.log(_make_event(event_id="fresh", timestamp=_iso(1)))
        removed = log.prune()
        assert removed == 2  # 1 sealed entry + 1 active entry
        assert log._list_sealed_indices() == []

    def test_prune_fails_closed_on_unparseable_active_timestamp(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        log.log(_make_event(event_id="weird", timestamp="not-a-date"))
        assert log.prune() == 0
        assert "weird" in (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode semantics")
    def test_prune_rewrite_keeps_owner_only_mode(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        log.log(_make_event(event_id="old-1", timestamp=_iso(400)))
        log.log(_make_event(event_id="new-1", timestamp=_iso(1)))
        assert log.prune() == 1
        mode = oct((sel_dir / "security_events.jsonl").stat().st_mode & 0o777)
        assert mode == "0o600"


class TestRecentAcrossSegments:
    def test_recent_surfaces_newest_after_rotation(self, sel_dir):
        # backup_count high so nothing is evicted: the assertion is about
        # newest-first ORDER across the seam, not about retention.
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 25)
        assert (_segdir(sel_dir) / "security_events.jsonl.1").exists()
        events = log.recent(limit=5)
        assert len(events) == 5
        ids = [e["event_id"] for e in events]
        assert ids[0] == "rot-000024", f"newest-first violated: {ids}"
        assert ids == sorted(ids, reverse=True), f"not strictly newest-first: {ids}"

    def test_recent_reads_into_sealed_segments_when_active_is_short(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 25)
        active_lines = [
            ln
            for ln in (sel_dir / "security_events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        assert 0 < len(active_lines) < 25, "need a short active file for this test"
        # Ask for more than the active file holds — the remainder must come from
        # sealed segments rather than the result simply being truncated.
        want = len(active_lines) + 3
        events = log.recent(limit=want)
        assert len(events) == want


class TestRotationHelpers:
    def test_parse_ts_accepts_z_naive_and_offset(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        for raw in ("2026-05-13T00:00:00Z", "2026-05-13T00:00:00", "2026-05-13T00:00:00-05:00"):
            parsed = log._parse_ts(raw)
            assert parsed is not None, raw
            assert parsed.tzinfo is not None, raw

    def test_parse_ts_returns_none_on_garbage(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        assert log._parse_ts("nope") is None
        assert log._parse_ts("") is None

    def test_entry_count_of_ignores_blank_lines(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        p = sel_dir / "seg"
        p.write_text("a\n\nb\n\n\nc\n", encoding="utf-8")
        assert log._entry_count_of(p) == 3

    def test_entry_count_of_missing_file_is_zero(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        assert log._entry_count_of(sel_dir / "absent") == 0

    def test_newest_timestamp_reads_the_tail(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        p = sel_dir / "seg"
        p.write_text(
            json.dumps({"timestamp": "2020-01-01T00:00:00+00:00"}) + "\n"
            + json.dumps({"timestamp": "2026-01-01T00:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        assert log._newest_timestamp_of(p) == "2026-01-01T00:00:00+00:00"

    def test_newest_timestamp_skips_non_object_json_line(self, sel_dir):
        """A bare scalar line parses as JSON but has no .get — must not raise.

        It must also not fall through to the OLDER record behind it: that reports a
        segment whose newest data is unreadable as being as old as data that is,
        which is what age pruning then acts on. None is the fail-closed answer both
        callers read as "cannot prove aged".
        """
        log = _rot_log(sel_dir, max_bytes=0)
        p = sel_dir / "seg"
        p.write_text(
            json.dumps({"timestamp": "2026-01-01T00:00:00+00:00"}) + "\n123\n",
            encoding="utf-8",
        )
        assert log._newest_timestamp_of(p) is None

    def test_newest_timestamp_spans_chunk_boundary(self, sel_dir):
        """Tail scan must not mis-handle a record straddling the 4 KB window."""
        log = _rot_log(sel_dir, max_bytes=0)
        p = sel_dir / "seg"
        pad = json.dumps({"timestamp": "2020-01-01T00:00:00+00:00", "pad": "x" * 200})
        tail = json.dumps({"timestamp": "2026-01-01T00:00:00+00:00"})
        p.write_text("\n".join([pad] * 60 + [tail]) + "\n", encoding="utf-8")
        assert p.stat().st_size > 4096  # multi-chunk precondition
        assert log._newest_timestamp_of(p) == "2026-01-01T00:00:00+00:00"

    def test_sealed_segments_include_every_number_in_ascending_order(self, sel_dir):
        """A gap must NOT truncate the walk: every segment is chain-verified.

        The shift-rename layout stopped at the first gap, so segments beyond a hole
        were unreachable and needed separate orphan accounting to keep verify loud.
        Monotonic numbering makes a gap ordinary (eviction removes the lowest
        numbers), so the walk simply covers everything present, oldest first.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        for idx in (1, 2, 4):
            (_segdir(sel_dir) / f"security_events.jsonl.{idx}").write_text("{}\n", encoding="utf-8")
        assert [p.name for p in log._sealed_segments()] == [
            "security_events.jsonl.1",
            "security_events.jsonl.2",
            "security_events.jsonl.4",
        ]
        assert log._list_sealed_indices() == [1, 2, 4]

    def test_sealed_segments_sort_numerically_not_lexically(self, sel_dir):
        """`.10` must not sort between `.1` and `.2`.

        Monotonic numbers pass 9 on any busy host, and a lexical sort there would
        hand verify the segments out of chain order -- reporting a break that is an
        artefact of the sort rather than of the data.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        for idx in (1, 2, 10, 11):
            (_segdir(sel_dir) / f"security_events.jsonl.{idx}").write_text("{}\n", encoding="utf-8")
        assert log._list_sealed_indices() == [1, 2, 10, 11]

    def test_segment_path_zero_is_the_active_file(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        assert log._segment_path(0) == sel_dir / "security_events.jsonl"
        assert log._segment_path(2) == _segdir(sel_dir) / "security_events.jsonl.2"


class TestRotationFailureIsNonFatal:
    def test_append_survives_a_rotation_error(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=200)
        _fill(log, 5)
        with patch.object(
            SecurityEventLog, "_maybe_rotate", side_effect=OSError("boom")
        ):
            log.log(_make_event(event_id="after-boom"))
        text = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
        assert "after-boom" in text, "a rotation failure must not block the append"


class TestRotationKnobDefaults:
    def test_constructor_exposes_no_rotation_kwargs(self, sel_dir):
        """The knobs are not public constructor surface (0 production consumers)."""
        import inspect

        params = set(inspect.signature(SecurityEventLog.__init__).parameters)
        assert params == {"self", "base_dir", "sync"}, params
        assert not hasattr(SecurityEventLog, "_warn_ignored_rotation_kwargs")

    def test_unset_knobs_take_the_module_defaults(self, sel_dir):
        """No kwargs -> module defaults, with no config lookup.

        Pins the self-sufficiency contract: ``KiroCrewConfig`` has no ``sel``
        section, so sel.py must not read one. If a future change reintroduces a
        config dependency, this fails when the section is absent.
        """
        from kiro_crew.sel import (
            _DEFAULT_BACKUP_COUNT,
            _DEFAULT_MAX_BYTES,
            _RETENTION_DAYS,
        )

        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log._max_bytes == _DEFAULT_MAX_BYTES
        assert log._backup_count == _DEFAULT_BACKUP_COUNT
        assert log._retention_days == _RETENTION_DAYS

    def test_sel_module_does_not_import_the_config_loader(self):
        """The rotation core must carry no config-loader coupling at all.

        Structural, because the behavioural test above would still pass if a
        lookup were added inside a try/except that swallows the AttributeError —
        which is exactly the shape mypy rejected.

        Walks the AST rather than grepping the text: the module legitimately
        MENTIONS ``config.loader`` in a comment describing an import cycle, and
        it legitimately imports ``config.paths`` for ``config_dir()``. Only a real
        import of the loader (or a reference to ``KiroCrewConfig``) is coupling.
        """
        import ast
        import inspect

        import kiro_crew.sel as sel_mod

        tree = ast.parse(inspect.getsource(sel_mod))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.extend(f"{node.module}.{a.name}" for a in node.names)
            elif isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
        assert not [m for m in imported if "config.loader" in m], imported
        # Positive control on the walk itself: it must see the imports that ARE
        # there, so an empty/broken collection cannot pass this test vacuously.
        assert any("config.paths" in m for m in imported), imported
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "KiroCrewConfig" not in names | attrs


class TestVerifyPinsSegmentsByHandle:
    """A concurrent roll must not let verify report a vacuous `integrity: ok`.

    Snapshotting PATHS and reading them after releasing `_lock` was unsound: a roll
    shifts `.k` -> `.k+1` and reseals the active file as `.1`, so every snapshotted
    path still EXISTS while naming a different inode. The walk then reads a set that
    is internally chain-adjacent but omits the renamed-away segment, and the sticky
    eviction marker suppresses the genesis check that would have caught it.

    The two scenario tests below are POSIX-only, and the reason is the OS, not the
    code under test. They must rename/unlink a segment while verify holds it open,
    which POSIX allows (the inode survives for the holder) and Windows refuses with
    WinError 32. So on Windows the race those tests reproduce cannot be constructed
    at all -- the kernel blocks the rename that the race depends on, which means the
    property being asserted holds there by a stricter mechanism than the pin. What
    that costs on Windows is only that a roll competing with an in-flight verify
    FAILS instead of proceeding; `_flush_batch` already contains that (warning plus
    the `kirocrew.sel.rotation_failed.count` counter, batch still appended), and
    `test_rotation_rename_failure_is_contained` below pins that containment on every
    platform. The mechanism itself is pinned cross-platform by the two structural
    guards, so a revert to path-based reads still fails the suite on Windows.
    """

    def _entries_on_disk(self, sel_dir):
        return _entries_across_segments(sel_dir)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX open-handle rename semantics; Windows refuses with WinError 32",
    )
    def test_roll_during_verify_does_not_fake_a_clean_chain(self, sel_dir, monkeypatch):
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 30)
        assert len(log._list_sealed_indices()) >= 3, "precondition: multi-segment"
        log._mark_evicted()  # sticky marker from an earlier legitimate eviction
        assert log._has_evicted()
        truth = self._entries_on_disk(sel_dir)
        assert truth == 30

        # Roll between the under-lock pin and the unlocked reads. With handles the
        # renames cannot rebind what verify is already holding.
        state = {"rolled": False}
        orig_walk = SecurityEventLog._walk_handles

        def walk_after_roll(self, handles, ev, unreadable=None):
            if not state["rolled"]:
                state["rolled"] = True
                for idx in sorted(log._list_sealed_indices(), reverse=True):
                    log._segment_path(idx).rename(log._segment_path(idx + 1))
                log._path.rename(log._segment_path(1))
                log._path.write_text("", encoding="utf-8")
            return orig_walk(self, handles, ev, unreadable)

        monkeypatch.setattr(SecurityEventLog, "_walk_handles", walk_after_roll)
        total, valid = log.verify_integrity()
        assert state["rolled"], "the roll must actually have fired"

        # Every entry that is still on disk must have been accounted for. The old
        # path-snapshot behaviour returned total=26 valid=26 here with 30 on disk.
        assert self._entries_on_disk(sel_dir) == 30
        assert total == 30, f"skipped retained history: total={total} valid={valid}"
        assert valid == total

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX open-handle unlink semantics; Windows refuses with WinError 32",
    )
    def test_unlink_during_verify_still_reads_the_pinned_inode(self, sel_dir, monkeypatch):
        """An open handle survives unlink, so verify sees a consistent snapshot."""
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        assert log._list_sealed_indices(), "precondition: at least one sealed segment"
        orig_walk = SecurityEventLog._walk_handles
        state = {"done": False}

        def walk_after_unlink(self, handles, ev, unreadable=None):
            if not state["done"]:
                state["done"] = True
                log._segment_path(1).unlink()
            return orig_walk(self, handles, ev, unreadable)

        monkeypatch.setattr(SecurityEventLog, "_walk_handles", walk_after_unlink)
        total, valid = log.verify_integrity()
        assert state["done"]
        assert total == 20, f"pinned inode was lost: total={total}"
        assert valid == total

    def test_walk_handles_never_reads_a_segment_by_path(self):
        """Cross-platform guard for the two POSIX-only scenario tests above.

        The pin is only real if the read goes through the handle: a path-based read
        re-opens whatever the path names NOW, which is exactly the rebinding those
        scenario tests reproduce. They cannot run on Windows, so this structural
        check is what keeps that regression detectable there.
        """
        import ast
        import inspect

        import kiro_crew.sel as sel_mod

        tree = ast.parse(inspect.getsource(sel_mod))

        def attrs_of(name: str) -> set[str]:
            fn = next(
                n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name
            )
            return {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}

        # Positive control: the detector must be able to SEE an attribute inside the
        # very function under test, otherwise "read_text is absent from
        # _walk_handles" would pass vacuously. Scoped to _walk_handles itself rather
        # than to some other function, so the control cannot rot when an unrelated
        # site changes -- which is exactly how it broke twice before. Checked by AST
        # rather than substring so the comment mentioning read_text elsewhere in this
        # module cannot match.
        assert "seek" in attrs_of("_walk_handles"), "AST detector is not working"

        walk = attrs_of("_walk_handles")
        assert walk, "no attribute access found -- wrong function resolved"
        assert "read_text" not in walk, "a path-based read defeats the handle pin"
        assert "seek" in walk, "expected a handle-based seek"
        # The read itself moved into _segment_lines when it gained a per-line cap, so
        # the pin now spans both: _walk_handles must hand the HANDLE over, and the
        # helper must read from that handle rather than reopening a path.
        assert "_segment_lines" in {
            n.func.id
            for n in ast.walk(
                next(
                    f
                    for f in ast.walk(tree)
                    if isinstance(f, ast.FunctionDef) and f.name == "_walk_handles"
                )
            )
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }, "_walk_handles no longer delegates to the bounded reader"
        helper = attrs_of("_segment_lines")
        assert "readline" in helper, "the bounded reader does not read from the handle"
        assert "read_text" not in helper and "open" not in helper, (
            "a path-based read in the helper defeats the handle pin"
        )

    def test_segment_handles_are_opened_while_the_lock_is_held(self, sel_dir, monkeypatch):
        """The pin is atomic against rotation only because open() runs under _lock.

        Asserts the ORDERING rather than the OS's rename semantics, so the guarantee
        stays covered on Windows. Spied at ``_open_segment``, which is the single
        chokepoint every segment read now goes through -- patching ``builtins.open``
        no longer observes anything, because the helper opens via ``os.open``.
        """
        import kiro_crew.sel as sel_mod

        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        assert log._list_sealed_indices(), "precondition: at least one sealed segment"

        real_open_segment = sel_mod._open_segment
        locked_at_open: list[bool] = []

        def spy(path, *args, **kwargs):
            if "security_events.jsonl" in str(path):
                locked_at_open.append(log._lock.locked())
            return real_open_segment(path, *args, **kwargs)

        monkeypatch.setattr(sel_mod, "_open_segment", spy)
        log.verify_integrity()
        monkeypatch.undo()

        assert locked_at_open, "no segment was opened during verify"
        assert all(locked_at_open), f"a segment was opened outside _lock: {locked_at_open}"

    def test_rotation_rename_failure_is_contained(self, sel_dir, monkeypatch, caplog):
        """A roll that cannot rename must not lose events or break the chain.

        This is the Windows shape of the trade the handle pin makes: there a roll
        competing with an in-flight verify fails with WinError 32 rather than
        rebinding the handle. Simulated with the same exception type so the
        containment _flush_batch already provides is pinned on every platform.
        """
        import logging

        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        before_total, before_valid = log.verify_integrity()
        assert before_valid == before_total, "precondition: chain valid before the roll"

        # The seal is an os.replace of the ACTIVE path onto a claimed segment
        # number, not a Path.rename of each segment in a shift sequence.
        real_replace = os.replace
        attempted: list[str] = []

        def blocked(src_path, dst_path, *args, **kwargs):
            if "security_events.jsonl" in os.fspath(src_path):
                attempted.append(os.fspath(src_path))
                raise PermissionError(
                    32,
                    "The process cannot access the file because it is being used "
                    "by another process",
                )
            return real_replace(src_path, dst_path, *args, **kwargs)

        monkeypatch.setattr(os, "replace", blocked)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            _fill(log, 20)  # must not raise
        monkeypatch.undo()

        assert attempted, "the rotation rename must actually have been attempted"
        assert any(
            "rotation failed" in r.getMessage().lower() for r in caplog.records
        ), "a contained rotation failure must stay observable"
        total, valid = log.verify_integrity()
        assert total == before_total + 20, f"events lost: {total} vs {before_total + 20}"
        assert valid == total, "the chain must stay valid across a failed roll"

    def test_no_retry_machinery_remains(self):
        """The retry loop was a workaround for a race it could not detect."""
        import inspect

        import kiro_crew.sel as sel_mod

        src = inspect.getsource(sel_mod)
        assert "_VERIFY_RACE_RETRIES" not in src
        assert "allow_raced_retry" not in src


class TestEvictionMarkerIsAuthenticated:
    """The marker gates an integrity relaxation, so it must not be forgeable.

    `sel.md`'s threat model names an actor with write access to the log directory
    (which is why the HMAC key lives outside it). That actor must not be able to
    create a marker and thereby suppress the genesis anchor on a never-evicted log.
    """

    def test_genuine_marker_relaxes_the_baseline(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=1)
        _fill(log, 80)
        assert (_segdir(sel_dir) / "evicted").exists()
        assert log._has_evicted(), "a marker we wrote must authenticate"
        total, valid = log.verify_integrity()
        assert total > 0 and valid == total

    def test_forged_marker_is_rejected(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        (_segdir(sel_dir) / "evicted").write_text("x", encoding="utf-8")
        assert log._has_evicted() is False, "a forged marker must not authenticate"

    def test_empty_touched_marker_is_rejected(self, sel_dir):
        """`touch` is the cheapest forgery — it must not work."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        (_segdir(sel_dir) / "evicted").touch()
        assert log._has_evicted() is False

    def test_forged_marker_cannot_hide_head_truncation(self, sel_dir):
        """The end-to-end attack the signing closes."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        path = sel_dir / "security_events.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        path.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        # Forge the marker to try to make the truncation read clean.
        (_segdir(sel_dir) / "evicted").touch()
        total, valid = log.verify_integrity()
        assert valid < total, "forged marker suppressed the genesis anchor"

    def test_marker_token_is_domain_separated_from_entry_hashes(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        token = log._marker_token()
        body = (sel_dir / "security_events.jsonl").read_text(encoding="utf-8")
        assert token not in body, "marker MAC must not collide with a chain hash"


class TestHostileBytesDoNotCrashTheReadSurfaces:
    """Authentication and JSON parsing must survive bytes an attacker controls.

    Two independent crash classes, both reachable from disk:

    * ``hmac.compare_digest`` REJECTS a non-ASCII ``str`` -- measured
      ``TypeError: comparing strings with non-ASCII characters is not supported``.
      The marker is decoded with ``errors="replace"``, so any invalid byte becomes
      U+FFFD; and a segment line's ``entry_hash`` is whatever the JSON held.
    * ``json.JSONDecodeError`` IS a ``ValueError`` (measured True) but
      ``RecursionError`` is NOT (measured False), so a nesting bomb and an
      over-4300-digit integer both escaped a JSONDecodeError-only handler.

    Every assertion here checks a POSITIVE observable -- an explicit False, an
    explicit True, or the good event actually present in the output. "No exception
    was raised" would pass vacuously against a guard that rejects everything, which
    is what the over-rejection controls exist to catch.
    """

    # A nesting bomb and an oversized integer literal: valid-looking JSON text that
    # raises something other than JSONDecodeError.
    BOMB = "[" * 200000 + "]" * 200000
    BIGINT = "9" * 10000

    @staticmethod
    def _sealed_log(sel_dir):
        """A log with several sealed segments, and the segment list."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100, retention_days=0)
        _fill(log, 8)
        log.flush()
        sealed = log._list_sealed_indices()
        assert sealed, "precondition: sealed segments exist"
        return log, sealed

    # ---- B1: the marker ---------------------------------------------------

    def test_a_marker_holding_invalid_utf8_does_not_crash_has_evicted(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        # Invalid UTF-8: the replace-decode turns these into U+FFFD.
        (_segdir(sel_dir) / "evicted").write_bytes(b"\xff\xfeXX")
        assert log._has_evicted() is False, (
            "a marker holding invalid UTF-8 must read as NOT authentic; before the "
            "guard this raised TypeError out of _has_evicted and crashed verify"
        )

    def test_an_authentic_marker_still_authenticates(self, sel_dir):
        """OVER-REJECTION CONTROL: fails if the guard rejects unconditionally."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        log._mark_evicted()
        assert log._has_evicted() is True, (
            "the ASCII guard rejected a genuine marker -- the guard is now an "
            "unconditional reject and the relaxation can never be granted"
        )

    def test_a_non_ascii_marker_does_not_relax_the_genesis_anchor(self, sel_dir):
        """GENESIS-ANCHOR CONTROL: fails if the guard returns True instead of False.

        Relaxing on a merely-present marker would let an attacker head-truncate the
        oldest segment undetected -- the attack `_has_evicted`'s own log and the
        comment above `eviction_plausible` exist to refuse.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        path = sel_dir / "security_events.jsonl"
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        path.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        (_segdir(sel_dir) / "evicted").write_bytes(b"\xff\xfeXX")
        total, valid = log.verify_integrity()
        assert valid < total, (
            "a non-ASCII marker suppressed the genesis anchor, so head truncation "
            "read clean -- the guard must reject it, not admit it"
        )

    # ---- B1: the record MAC ----------------------------------------------

    def test_a_non_ascii_entry_hash_does_not_crash_record_authentication(self, sel_dir):
        log, sealed = self._sealed_log(sel_dir)
        seg = log._segment_path(sealed[0])
        record = json.loads(seg.read_text(encoding="utf-8").strip().splitlines()[0])
        record["entry_hash"] = "\u00e1" * 64  # valid JSON, non-ASCII, wrong length ok
        assert log._record_is_authentic(record) is False, (
            "a non-ASCII entry_hash must read as NOT authentic; before the guard "
            "this raised TypeError, and recent() reaches it for sealed segments"
        )

    def test_an_authentic_record_still_authenticates(self, sel_dir):
        """OVER-REJECTION CONTROL: fails if the guard rejects unconditionally.

        Uses a record the product itself wrote, so the control cannot drift from
        whatever `_record_is_authentic` computes.
        """
        log, sealed = self._sealed_log(sel_dir)
        seg = log._segment_path(sealed[0])
        record = json.loads(seg.read_text(encoding="utf-8").strip().splitlines()[0])
        assert log._record_is_authentic(record) is True, (
            "the ASCII guard rejected a record this install wrote -- every sealed "
            "segment would now be filtered out of recent()"
        )

    # ---- B2: hostile JSON on the events path ------------------------------

    def test_recent_returns_the_good_event_past_hostile_json_lines(self, sel_dir):
        """The POSITIVE observable: real data comes back, not merely no exception."""
        log, sealed = self._sealed_log(sel_dir)
        seg = log._segment_path(sealed[-1])
        good = seg.read_text(encoding="utf-8").strip().splitlines()[-1]
        good_id = json.loads(good)["event_id"]
        seg.write_text(
            self.BOMB + "\n" + self.BIGINT + "\n" + good + "\n", encoding="utf-8"
        )

        events = log.recent(limit=50)

        assert any(e.get("event_id") == good_id for e in events), (
            f"the well-formed event {good_id!r} was not returned past the hostile "
            f"lines; before the widening recent() raised and returned nothing at all"
        )

    def test_a_valid_json_non_object_line_is_still_skipped(self, sel_dir):
        """OVER-CATCH CONTROL: `123` parses fine and must be dropped by the dict check.

        Fails if the isinstance branch is bypassed -- a bare int in the output would
        break every consumer, which annotates these elements as dicts.
        """
        log, sealed = self._sealed_log(sel_dir)
        seg = log._segment_path(sealed[-1])
        good = seg.read_text(encoding="utf-8").strip().splitlines()[-1]
        good_id = json.loads(good)["event_id"]
        seg.write_text("123\n" + good + "\n", encoding="utf-8")

        events = log.recent(limit=50)

        assert all(isinstance(e, dict) for e in events), "a non-object reached output"
        assert 123 not in events, "the bare int `123` was returned as an event"
        assert any(e.get("event_id") == good_id for e in events), (
            "control precondition: the good event must still come back, otherwise "
            "this test would pass simply because nothing was returned"
        )

    def test_an_oserror_in_the_line_loop_still_surfaces(self, sel_dir):
        """OVER-CATCH CONTROL: fails if the handler is widened to `except Exception`.

        An OSError raised while parsing a line must propagate. The outer
        `except OSError` in recent() closes at the tail read, so it does not cover
        this loop -- widening the inner handler to Exception would turn a real IO
        fault into a silently short listing.
        """
        log, sealed = self._sealed_log(sel_dir)
        real_loads = json.loads

        def _boom(s, *a, **kw):
            if '"event_id"' in s:
                raise OSError("simulated IO fault while parsing a line")
            return real_loads(s, *a, **kw)

        with patch.object(sel_mod.json, "loads", _boom):
            with pytest.raises(OSError):
                log.recent(limit=50)


class TestRotationIsSerializedAcrossProcesses:
    """`_lock` is a threading.Lock, so it orders writers only within ONE process.

    SEL has more than one writer process on a normal host: the dashboard gateway and
    the MCP gateway daemon both construct `SecurityEventLog()` with no base_dir, so
    both resolve the same file. Monotonic numbering is what makes that safe without
    a cross-process lock -- each process claims its own segment number atomically
    and no existing segment is ever renamed, so the worst concurrent outcome is one
    extra small segment rather than a segment overwritten by another process.

    Distinct from the pre-existing multi-writer APPEND hazard, which this does NOT
    fix and does not claim to: concurrent appends from two processes already break
    the HMAC chain (each caches its own tip), and that is fail-loud with every entry
    still on disk.
    """

    def test_concurrent_process_rotation_preserves_every_entry(self, sel_dir):
        """Two real processes rolling the same over-cap file must lose nothing.

        SMOKE test, not the discriminating one: the interleaving window is tiny (after
        the first roll the file is under the cap, so later attempts return at the size
        check), and this still passes with the lease bypassed. Its value is showing the
        lease does not deadlock or corrupt under real multi-process use.
        test_rotation_is_skipped_while_a_rival_holds_the_lease is what actually fails
        when the lease is ignored.
        """
        import subprocess
        import sys

        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 30)
        log.flush()
        before = _entries_across_segments(sel_dir)
        assert before == 30, f"precondition: all 30 on disk, saw {before}"

        script = (
            "import sys,pathlib;"
            "from kiro_crew.sel import SecurityEventLog as S;"
            "l=S(base_dir=pathlib.Path(sys.argv[1]),sync=True);"
            "l._max_bytes=1500;l._backup_count=100;l._retention_days=365;"
            "\nfor _ in range(6):\n"
            "    l._lock.acquire()\n"
            "    try: l._maybe_rotate()\n"
            "    finally: l._lock.release()\n"
        )
        procs = [
            # cwd pinned to the SEL dir: with `-c`, Python prepends the inherited
            # working directory to sys.path, so a stray `kiro_crew` there would be
            # imported instead of the installed package under test.
            subprocess.Popen(
                [sys.executable, "-c", script, str(sel_dir)], cwd=str(sel_dir)
            )
            for _ in range(2)
        ]
        try:
            for p in procs:
                assert p.wait(timeout=120) == 0, "a rotating process failed"
        finally:
            # Reap EVERY child, including the ones after a failure. The assertion
            # above exits the loop on the first non-zero exit, and p.wait() itself
            # raises TimeoutExpired -- either way the remaining children were
            # neither signalled nor waited for and outlived the test. Kill only
            # what is still running, and never swallow the original failure: this
            # block raises nothing of its own.
            for p in procs:
                if p.poll() is None:
                    p.kill()
                try:
                    p.wait(timeout=10)
                except Exception:
                    pass

        after = _entries_across_segments(sel_dir)
        assert after == before, f"segment loss under concurrent rotation: {after} of {before}"


class TestRotationKnobsAreOperatorSettable:
    """The knobs govern DELETION on an audit surface, so they must not be source-only.

    Amazon's audit-log retention guidance requires the retention period to be
    operator-settable; a compile-time-only cap leaves a host whose volume outruns it
    with no lever. Env is the interim surface -- the config section is the follow-up.
    """

    def test_env_overrides_the_module_defaults(self, sel_dir, monkeypatch):
        monkeypatch.setenv("KIROCREW_SEL_MAX_BYTES", "4242")
        monkeypatch.setenv("KIROCREW_SEL_BACKUP_COUNT", "9")
        monkeypatch.setenv("KIROCREW_SEL_RETENTION_DAYS", "7")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert (log._max_bytes, log._backup_count, log._retention_days) == (4242, 9, 7)

    def test_env_can_disable_rotation_entirely(self, sel_dir, monkeypatch):
        """max_bytes=0 is the documented off switch, and it must be reachable."""
        monkeypatch.setenv("KIROCREW_SEL_MAX_BYTES", "0")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert log._max_bytes == 0
        _fill(log, 40)
        assert log._list_sealed_indices() == [], "rotation ran despite being disabled"

    def test_malformed_env_falls_back_to_the_default_and_says_so(self, sel_dir, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("KIROCREW_SEL_MAX_BYTES", "100MB")  # not an int
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            log = SecurityEventLog(base_dir=sel_dir, sync=True)
        from kiro_crew.sel import _DEFAULT_MAX_BYTES

        assert log._max_bytes == _DEFAULT_MAX_BYTES, "a typo must not change the cap"
        assert any("malformed" in r.getMessage().lower() for r in caplog.records)

    def test_unset_env_leaves_the_module_defaults(self, sel_dir, monkeypatch):
        for k in ("KIROCREW_SEL_MAX_BYTES", "KIROCREW_SEL_BACKUP_COUNT", "KIROCREW_SEL_RETENTION_DAYS"):
            monkeypatch.delenv(k, raising=False)
        from kiro_crew.sel import _DEFAULT_BACKUP_COUNT, _DEFAULT_MAX_BYTES, _RETENTION_DAYS

        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        assert (log._max_bytes, log._backup_count, log._retention_days) == (
            _DEFAULT_MAX_BYTES,
            _DEFAULT_BACKUP_COUNT,
            _RETENTION_DAYS,
        )

    def test_a_rejected_override_that_could_be_a_secret_is_not_echoed(
        self, sel_dir, monkeypatch, caplog
    ):
        """An operator who pastes a credential into a size knob must not have it
        copied into the log. The knob name and the default still have to survive,
        or the warning stops being actionable."""
        import logging

        secretish = "s3cret-tok3n-do-not-log-9f3a2b"
        monkeypatch.setenv("KIROCREW_SEL_MAX_BYTES", secretish)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            log = SecurityEventLog(base_dir=sel_dir, sync=True)
        from kiro_crew.sel import _DEFAULT_MAX_BYTES

        assert log._max_bytes == _DEFAULT_MAX_BYTES
        messages = [r.getMessage() for r in caplog.records]
        assert messages, "the rejection was not reported at all"
        for m in messages:
            assert secretish not in m, f"the rejected override leaked into a log record: {m!r}"
        warned = [m for m in messages if "malformed" in m.lower()]
        assert warned, "no malformed-override warning was emitted"
        assert f"<{len(secretish)} chars>" in warned[0], (
            f"the length summary replacing the value is missing: {warned[0]!r}"
        )
        assert "KIROCREW_SEL_MAX_BYTES" in warned[0], "the operator cannot tell which knob"

    def test_a_numeric_typo_is_still_echoed_so_the_warning_stays_useful(
        self, sel_dir, monkeypatch, caplog
    ):
        """The other direction: redacting EVERYTHING would leave the operator knowing
        a knob was ignored but not what they typed. A value spelled only from digits
        and integer punctuation cannot carry a secret, so it is still shown."""
        import logging

        monkeypatch.setenv("KIROCREW_SEL_MAX_BYTES", "1,048,576")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            SecurityEventLog(base_dir=sel_dir, sync=True)
        warned = [r.getMessage() for r in caplog.records if "malformed" in r.getMessage().lower()]
        assert warned, "no malformed-override warning was emitted"
        assert "1,048,576" in warned[0], (
            f"a safe numeric typo was redacted, losing the diagnostic: {warned[0]!r}"
        )


class TestEarlyEvictionIsObservable:
    """The size cap and the retention window are independent bounds, and size wins.

    On a host whose volume outruns max_bytes the log silently stops meeting its own
    retention_days, which is exactly the "audit evidence deleted before the review
    period" failure. It must be loud.
    """

    def test_evicting_inside_the_retention_window_warns_and_counts(self, sel_dir, caplog):
        import logging

        log = _rot_log(sel_dir, max_bytes=300, backup_count=1, retention_days=365)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            _fill(log, 60)  # fresh events, so anything evicted is inside the window
        assert log._early_evictions > 0, "early eviction was not counted"
        assert any(
            "newer than the 365-day retention window" in r.getMessage()
            for r in caplog.records
        ), "early eviction was silent"

    def test_no_warning_when_eviction_is_outside_the_window(self, sel_dir, caplog):
        import logging

        log = _rot_log(sel_dir, max_bytes=300, backup_count=1, retention_days=1)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            _fill(log, 60, ts="2020-01-01T00:00:00+00:00")  # long past the 1-day window
        assert log._early_evictions == 0, "aged-out eviction must not warn"
        assert not any(
            "retention window" in r.getMessage() for r in caplog.records
        ), "warned about a legitimately aged eviction"


class TestEvictionMarkerWritePathIsSymlinkSafe:
    """The marker path is agent-writable before this feature's family lands.

    An earlier round authenticated the marker's CONTENTS with a MAC. That says
    nothing about the WRITE PATH: ``write_text`` opens for truncation and follows a
    symlink, so a pre-placed link turned the marker write into a "truncate any file
    I name" primitive. These pin the write acting on the NAME and the read refusing
    to follow -- separate mechanisms, so asserted separately.
    """

    def test_marker_write_does_not_truncate_a_symlink_target(self, sel_dir, tmp_path):
        victim = tmp_path / "victim.txt"
        original = "PRECIOUS-CONTENT-THAT-MUST-SURVIVE\n" * 4
        victim.write_text(original, encoding="utf-8")

        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        marker = _segdir(sel_dir) / "evicted"
        marker.unlink(missing_ok=True)
        try:
            marker.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform/filesystem")

        log._mark_evicted()

        assert victim.read_text(encoding="utf-8") == original, (
            "the marker write followed the symlink and truncated its target: that is "
            "an arbitrary-file-truncation primitive on an agent-writable path"
        )
        assert not marker.is_symlink(), "the symlink was followed instead of replaced"
        assert log._has_evicted(), "marker write did not produce an authentic marker"

    def test_marker_read_refuses_to_follow_a_symlink(self, sel_dir, tmp_path):
        """A symlinked marker reads as absent (fail closed) rather than being followed."""
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW unavailable; the byte cap is the only guard there")
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        token = log._marker_token()
        planted = tmp_path / "planted.txt"
        planted.write_text(token, encoding="utf-8")

        marker = _segdir(sel_dir) / "evicted"
        marker.unlink(missing_ok=True)
        try:
            marker.symlink_to(planted)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform/filesystem")

        assert log._has_evicted() is False, (
            "the marker read followed a symlink; a link is not a marker this install wrote"
        )
        # Control: the SAME bytes in a regular file DO authenticate, so the False
        # above is attributable to the symlink and not to a wrong token.
        marker.unlink()
        marker.write_text(token, encoding="utf-8")
        assert log._has_evicted() is True, "control failed: the token itself is not authentic"

    def test_marker_read_is_byte_capped(self, sel_dir):
        """An oversized marker is not read whole -- the cap is a DoS bound."""
        log = SecurityEventLog(base_dir=sel_dir, sync=True)
        marker = _segdir(sel_dir) / "evicted"
        marker.write_text("x" * (_MARKER_READ_CAP * 40), encoding="utf-8")
        assert log._has_evicted() is False
        assert _MARKER_READ_CAP < 4096, "cap must stay far below a plausible payload"


class TestSegmentReadsRefuseNonRegularFiles:
    """Segment names are predictable and agent-writable before this feature's
    sensitive-path family lands, so a planted `.1` must not be followed.

    A symlink to an endless source would otherwise turn verify/events into an
    unbounded read. Each read surface is asserted separately: they are different call
    sites, and a helper that only some of them use would read as closed while one
    path stayed open.
    """

    @staticmethod
    def _plant_symlinked_segment(sel_dir, target):
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.unlink(missing_ok=True)
        try:
            seg.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform/filesystem")
        return seg

    def test_open_segment_refuses_a_symlink(self, sel_dir, tmp_path):
        # No skip: the refusal now holds on both branches -- O_NOFOLLOW where it
        # exists, an is_symlink() pre-check where it does not.
        victim = tmp_path / "regular.txt"
        victim.write_text("x\n", encoding="utf-8")
        seg = self._plant_symlinked_segment(sel_dir, victim)
        with pytest.raises(OSError):
            _open_segment(seg)
        # Control: the same helper opens a REAL segment, so the raise above is the
        # symlink and not a broken helper. write_bytes, NOT write_text: text mode
        # translates "\n" to os.linesep, so on Windows the file holds b"y\r\n" and
        # this byte comparison fails on the fixture rather than on the guard.
        real = _segdir(sel_dir) / "security_events.jsonl.2"
        real.write_bytes(b"y\n")
        with _open_segment(real) as fh:
            assert fh.read() == b"y\n"

    def test_open_segment_refuses_a_fifo(self, sel_dir):
        """S_ISREG carries the guard where O_NOFOLLOW cannot: a fifo is not a link."""
        if not hasattr(os, "mkfifo"):
            pytest.skip("mkfifo unavailable on this platform")
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.unlink(missing_ok=True)
        try:
            os.mkfifo(seg)
        except (OSError, NotImplementedError):
            pytest.skip("mkfifo unsupported on this filesystem")
        with pytest.raises(OSError):
            # Opening a fifo read-only would block forever without the guard, so this
            # is run with O_NONBLOCK semantics via the helper's own refusal path.
            _open_segment(seg)

    def test_every_segment_read_goes_through_the_helper(self):
        """Structural: no bare open() of a segment path may survive in sel.py.

        A future read site that opens by path directly would reintroduce the hole
        while every behavioural test above still passed.
        """
        import inspect

        import kiro_crew.sel as sel_mod

        src = inspect.getsource(sel_mod)
        assert 'open(path, "rb")' not in src, "a bare segment open by path survives"
        assert 'open(p, "rb")' not in src, "a bare segment open by path survives"
        # Positive control: the guarded helper IS the thing being used instead.
        assert src.count("_open_segment(") >= 5, "read sites are not routed through the helper"

    def test_verify_skips_a_planted_segment_instead_of_reading_it(self, sel_dir, tmp_path):
        """End to end: a planted segment must not be read, and verify stays loud."""
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW unavailable; S_ISREG is the remaining guard")
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        assert log._list_sealed_indices(), "precondition: a real sealed segment exists"

        endless = tmp_path / "endless.txt"
        endless.write_text("z" * 4096, encoding="utf-8")
        self._plant_symlinked_segment(sel_dir, endless)

        result = log.verify_integrity()  # must not raise and must not follow the link
        assert result is not None
        assert "z" * 64 not in str(result), "planted content reached the verify result"


class TestNonCanonicalSegmentSuffixesAreRefused:
    """`_segment_path` maps index<=0 to the ACTIVE file BY DESIGN.

    So the suffix parse is a data-loss guard, not tidiness: a
    `security_events.jsonl.0` in the segment directory resolves to the live log, and
    eviction and age-pruning both unlink whatever the listing returns. The
    zero-padded forms are the second half -- `.01` is a distinct FILE from `.1` but
    parses to the same index, so it inflates the eviction budget and makes every
    path operation act on `.1` while `.01` is what was listed.
    """

    @staticmethod
    def _plant(log, suffix):
        seg = log._segment_dir()
        seg.mkdir(parents=True, exist_ok=True)
        p = seg / f"{sel_mod._SEL_FILE}.{suffix}"
        p.write_text(
            '{"event_id":"planted","timestamp":"2020-01-01T00:00:00+00:00"}\n',
            encoding="utf-8",
        )
        return p

    def _log(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=0)
        _fill(log, 5)
        log.flush()
        return log

    def test_index_zero_would_be_the_active_file(self, sel_dir):
        """The premise, pinned: this is WHY `.0` must never parse."""
        log = self._log(sel_dir)
        assert log._segment_path(0) == log._path, (
            "_segment_path no longer maps 0 to the active file; if that changed, "
            "re-derive whether this guard is still the right shape"
        )

    @pytest.mark.parametrize("suffix", ["0", "00", "000", "01", "007"])
    def test_non_canonical_suffixes_are_not_listed(self, sel_dir, suffix):
        log = self._log(sel_dir)
        self._plant(log, suffix)
        assert log._list_sealed_indices() == [], (
            f"'.{suffix}' was accepted as a segment number"
        )

    def test_a_planted_zero_does_not_delete_the_active_log(self, sel_dir):
        """The consequence, end to end: age-pruning must not unlink the live log."""
        log = self._log(sel_dir)
        self._plant(log, "0")
        before = len(log._path.read_text(encoding="utf-8").splitlines())
        assert before == 5, f"precondition: 5 entries in the active file, saw {before}"

        with log._lock:
            log._prune_sealed_by_age(1)

        assert log._path.exists(), (
            "age-pruning deleted the ACTIVE audit log via a '.0' segment alias"
        )
        assert len(log._path.read_text(encoding="utf-8").splitlines()) == before

    def test_the_old_parse_reproduces_the_deletion(self, sel_dir):
        """NEGATIVE CONTROL: without the canonical check the active log really goes."""
        log = self._log(sel_dir)
        self._plant(log, "0")

        def old_parse(self_):
            out = []
            for entry in self_._segment_dir().iterdir():
                sfx = entry.name[len(f"{sel_mod._SEL_FILE}.") :]
                if entry.name.startswith(f"{sel_mod._SEL_FILE}.") and sfx.isdigit():
                    out.append(int(sfx))
            return sorted(out)

        with patch.object(sel_mod.SecurityEventLog, "_list_sealed_indices", old_parse):
            assert log._list_sealed_indices() == [0], "control: '.0' should parse to 0"
            with log._lock:
                log._prune_sealed_by_age(1)

        assert not log._path.exists(), (
            "the control did not reproduce the deletion, so the canonical check is "
            "not what prevents it"
        )

    def test_canonical_suffixes_still_parse(self, sel_dir):
        """NEGATIVE CONTROL: real segments must not be refused."""
        log = self._log(sel_dir)
        for s in ("1", "2", "10", "137"):
            self._plant(log, s)
        assert log._list_sealed_indices() == [1, 2, 10, 137]


class TestRetentionCountIsBounded:
    """`_entry_count_of` is reached with an oversized line NOT at the tail.

    The reachability argument that previously excused the unbounded read only holds
    when the pathological line is LAST: `_prune_sealed_by_age` gates on
    `_newest_timestamp_of`, which reads the tail. A huge line in the MIDDLE of a
    segment whose final line carries an ordinary aged stamp passes that gate.
    """

    @staticmethod
    def _segment_with_a_huge_middle_line(tmp_path):
        p = tmp_path / "seg.jsonl"
        pad = "x" * (sel_mod._SEGMENT_LINE_CAP * 3)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"event_id":"a","timestamp":"2020-01-01T00:00:00+00:00"}\n')
            f.write('{"event_id":"big","pad":"%s"}\n' % pad)
            f.write('{"event_id":"z","timestamp":"2020-01-01T00:00:00+00:00"}\n')
        return p

    def test_the_gate_really_does_let_this_through(self, tmp_path):
        """The premise: the tail stamp parses, so the count IS reached."""
        p = self._segment_with_a_huge_middle_line(tmp_path)
        assert SecurityEventLog._newest_timestamp_of(p) is not None, (
            "the timestamp gate rejected this segment, so the reachability argument "
            "would hold and this test proves nothing"
        )

    def test_an_oversized_line_stops_the_count_instead_of_being_allocated(self, tmp_path):
        p = self._segment_with_a_huge_middle_line(tmp_path)
        count = SecurityEventLog._entry_count_of(p)
        assert count == 1, (
            f"expected the count to stop at the line before the cap breach, got {count}"
        )

    def test_an_ordinary_segment_still_counts_exactly(self, tmp_path):
        """NEGATIVE CONTROL: the cap must not truncate a healthy count."""
        p = tmp_path / "ok.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for i in range(7):
                f.write('{"event_id":"e%d"}\n' % i)
            f.write("\n")  # blank line must not be counted
        assert SecurityEventLog._entry_count_of(p) == 7

    def test_counting_refuses_a_non_regular_file(self, tmp_path):
        """Routing through _open_segment picks up its S_ISREG guard."""
        d = tmp_path / "adir"
        d.mkdir()
        assert SecurityEventLog._entry_count_of(d) == 0


class TestActivePruneComparesIdentityNotJustSize:
    """Size equality is not file identity.

    A rival process that rotates -- sealing the active file away and letting appends
    recreate it -- produces a DIFFERENT file. Byte-size equality between the old and
    new one is a coincidence the pre-replace check would read as "nothing changed",
    and the stale `os.replace` would then discard the recreated file's events.
    """

    def test_a_same_size_replacement_is_detected_and_skipped(self, sel_dir):
        """Swap the active file for a same-SIZE different-INODE file mid-prune.

        The swap happens between the size capture and the pre-replace check, which is
        exactly where a rival rotate-then-recreate lands. Size alone cannot see it.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=1)
        _fill(log, 6, ts=_iso(400))  # all aged, so Stage 2 reaches the check
        log.flush()
        original = log._path.read_text(encoding="utf-8")
        st_before = log._path.stat()
        size_before = st_before.st_size
        ident_before = (st_before.st_dev, st_before.st_ino)

        real_stat = Path.stat
        swapped: dict[str, object] = {"done": False, "ident": None}

        def _stat_swapping_after_the_read_pass(self_path, *a, **kw):
            # Anchor on the temp file rather than a call count: `.sel_prune_*.tmp`
            # exists only after mkstemp, i.e. after size/identity were captured and
            # the read pass is done -- which is exactly the pre-replace window a
            # rival rotate-then-recreate lands in. Counting stat calls is fragile
            # because `exists()` stats too.
            if (
                self_path == log._path
                and not swapped["done"]
                and any(log._path.parent.glob(".sel_prune_*.tmp"))
            ):
                swapped["done"] = True
                # Allocate the replacement WHILE the original is still linked, then
                # rename it over the top. unlink-then-recreate looks like the same
                # thing but is not: ext4's inode allocator commonly hands back the
                # inode the unlink just freed, so the "new" file arrives wearing the
                # old identity and the guard under test sees nothing to skip. That
                # made this test pass on tmpfs and xfs and fail on the CI runner's
                # ext4. A file created before the unlink cannot hold that inode.
                rival = log._path.parent / ".rival_recreate"
                rival.write_text(original, encoding="utf-8")
                os.replace(rival, log._path)
                st_rival = real_stat(log._path)
                swapped["ident"] = (st_rival.st_dev, st_rival.st_ino)
            return real_stat(self_path, *a, **kw)

        with patch.object(Path, "stat", _stat_swapping_after_the_read_pass):
            removed = log.prune(keep_days=1)

        assert swapped["done"], "the swap never fired; the test proved nothing"
        # Without this the test is GREEN whenever the filesystem recycles the inode,
        # because then the guard correctly sees an unchanged file and the rewrite it
        # was supposed to skip is the right thing to do. Assert the premise held.
        assert swapped["ident"] != ident_before, (
            "the swap reused the inode -- the replacement is indistinguishable from "
            "the original, so this test proved nothing"
        )
        assert log._path.read_text(encoding="utf-8") == original, (
            "the stale rewrite replaced a file it had not read -- the recreated "
            "file's events were discarded"
        )
        assert log._path.stat().st_size == size_before
        assert isinstance(removed, int)

    def test_identity_is_captured_alongside_size(self, sel_dir):
        """Structural guard: the pre-replace check must compare identity, not size only.

        A behavioural race is not reliably constructible in-process (Stage 2 holds
        `_lock`, and the hazard is cross-process), so this pins the property at the
        source level -- which is also what a reviewer would look for.
        """
        import inspect

        src = inspect.getsource(sel_mod.SecurityEventLog.prune)
        assert "ident_before" in src and "ident_now" in src, (
            "the active-file prune no longer captures file identity; size equality "
            "alone cannot detect a rotate-then-recreate"
        )
        assert "st_ino" in src, "identity must be inode-based, not size-derived"


class TestDiscardFailsClosedOnASealedUnlinkError:
    """`backup_count=0` discard must not delete the active file it cannot finish.

    `recent()` opens segments OUTSIDE `_lock` while `_discard_leased` runs under it,
    so on Windows a sealed segment held open by a concurrent `recent()` makes its
    unlink fail with a sharing violation. `missing_ok=True` suppresses only
    FileNotFoundError, so that error propagates. With the active file deleted first,
    the failure left the events gone, the sealed segments still on disk and the tip
    still naming a deleted entry -- a broken chain, which the operator note does NOT
    promise. It promises the loss of recent EVENTS, not the loss of the CHAIN.

    The simulated sharing violation keeps this test cross-platform: it asserts the
    ORDERING invariant, which holds on every OS, rather than reproducing WinError 32.
    """

    @staticmethod
    def _discard_log(sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100, retention_days=0)
        _fill(log, 8)
        log.flush()
        assert log._list_sealed_indices(), "precondition: sealed segments exist"
        log._backup_count = 0  # the discard path
        return log

    @staticmethod
    def _refuse_sealed_unlink(log):
        """Make the sealed unlink fail the way a held-open segment does."""
        real_unlink = Path.unlink
        sealed = {log._segment_path(i) for i in log._list_sealed_indices()}

        def _wrapped(self_path, *a, **kw):
            if self_path in sealed:
                raise PermissionError(
                    32, "The process cannot access the file because it is being used "
                    "by another process"
                )
            return real_unlink(self_path, *a, **kw)

        return _wrapped

    def test_a_failed_sealed_unlink_leaves_the_active_file_and_tip_intact(self, sel_dir):
        log = self._discard_log(sel_dir)
        tip_before = log._last_hash
        assert tip_before, "precondition: a chain tip exists"

        with patch.object(Path, "unlink", self._refuse_sealed_unlink(log)):
            with pytest.raises(PermissionError):
                with log._lock:
                    log._discard_leased()

        assert log._path.exists(), (
            "the active file was deleted even though the discard could not finish -- "
            "its events are gone and the chain tip now names a deleted entry"
        )
        assert log._last_hash == tip_before, "the tip was reset on a failed discard"

    def test_the_active_first_order_reproduces_the_corruption(self, sel_dir):
        """NEGATIVE CONTROL: the OLD order loses the events and dangles the tip.

        Without this the test above would pass against either ordering, since a
        successful discard deletes the active file in both.
        """
        log = self._discard_log(sel_dir)

        def old_order(self_):
            self_._path.unlink(missing_ok=True)
            for idx in self_._list_sealed_indices():
                self_._segment_path(idx).unlink(missing_ok=True)
            self_._last_hash = ""

        with patch.object(Path, "unlink", self._refuse_sealed_unlink(log)):
            with pytest.raises(PermissionError):
                with log._lock:
                    old_order(log)

        assert not log._path.exists(), (
            "the control did not reproduce the loss, so ordering is not what fixes it"
        )
        assert log._last_hash, "control: tip should still name the now-deleted entry"

    def test_a_clean_discard_still_re_anchors_to_genesis(self, sel_dir):
        """NEGATIVE CONTROL: reordering must not break the happy path."""
        log = self._discard_log(sel_dir)

        with log._lock:
            log._discard_leased()

        # The discard TRUNCATES rather than unlinks, so a rival writer holding an
        # O_APPEND fd does not lose its bytes to an orphaned inode. The property
        # under test is that the contents are discarded, not how the file goes.
        assert log._path.stat().st_size == 0, "the active file should be emptied"
        assert log._list_sealed_indices() == [], "sealed segments should be gone"
        assert log._last_hash == "", "the chain should be re-anchored to genesis"
        assert not log._marker_path().exists(), "the eviction marker should be cleared"


class TestAPartialDiscardMarksTheEvictionItPerformed:
    """A `backup_count=0` discard that fails PART WAY still evicted a prefix.

    Segments are unlinked in ascending order, so a raise on segment k+1 leaves
    1..k already gone. That is a real eviction, and `_flush_batch` SWALLOWS the
    error ("appending without rotating"), so the process carries on past it. With
    no eviction marker, `verify_integrity` keeps enforcing the genesis anchor --
    `eviction_plausible = self._has_evicted()` -- against a surviving segment whose
    `prev_hash` names an entry this code itself deleted. That reads as a chain
    break on a host where nothing tampered with anything.

    Distinct from :class:`TestDiscardFailsClosedOnASealedUnlinkError`, which
    refuses EVERY sealed unlink and therefore never gets past the first: that is
    the nothing-was-deleted case, asserted here as a negative control.
    """

    @staticmethod
    def _discard_log(sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100, retention_days=0)
        _fill(log, 8)
        log.flush()
        sealed = log._list_sealed_indices()
        assert len(sealed) >= 3, f"precondition: need >=3 sealed segments, got {sealed}"
        log._backup_count = 0  # the discard path
        log._marker_path().unlink(missing_ok=True)
        assert not log._has_evicted(), "precondition: no marker before the discard"
        return log, sealed

    @staticmethod
    def _refuse_one(log, idx):
        """Refuse the unlink of ONE segment, so earlier ones are already gone."""
        real_unlink = Path.unlink
        victim = log._segment_path(idx)

        def _wrapped(self_path, *a, **kw):
            if self_path == victim:
                raise PermissionError(
                    32, "The process cannot access the file because it is being "
                    "used by another process"
                )
            return real_unlink(self_path, *a, **kw)

        return _wrapped

    def _partial_discard(self, sel_dir):
        log, sealed = self._discard_log(sel_dir)
        with patch.object(Path, "unlink", self._refuse_one(log, sealed[-1])):
            with pytest.raises(PermissionError):
                with log._lock:
                    log._discard_leased()
        survivors = log._list_sealed_indices()
        assert survivors and len(survivors) < len(sealed), (
            f"precondition: the discard must be PARTIAL -- sealed {sealed} became "
            f"{survivors}, which is neither a partial nor a survivable state"
        )
        return log

    def test_a_partial_discard_writes_the_eviction_marker(self, sel_dir):
        log = self._partial_discard(sel_dir)
        assert log._marker_path().exists(), (
            "a prefix of the sealed run was deleted and no eviction marker was "
            "written, so verify will enforce genesis against history this code "
            "itself removed"
        )
        assert log._has_evicted(), "the marker must AUTHENTICATE, not merely exist"

    def test_verification_does_not_report_a_spurious_break_after_a_partial_discard(
        self, sel_dir
    ):
        log = self._partial_discard(sel_dir)
        total, valid = log.verify_integrity()
        assert valid == total, (
            f"verify reported valid<total ({valid}<{total}) over segments the "
            f"discard itself deleted -- a spurious break, not tampering"
        )

    def test_no_marker_when_there_are_no_sealed_segments(self, sel_dir):
        """NEGATIVE CONTROL: nothing evicted must never mark.

        Marking a host that evicted nothing relaxes the genesis anchor for free,
        which is exactly the head-truncation relaxation verify refuses to grant on
        mere segment existence.

        Asserts on the CALL, not on the end state. This path completes, so the
        marker clear at the bottom of `_discard_leased` removes any marker before
        the method returns -- an end-state assertion here would pass even against a
        version that marked unconditionally, i.e. it could not fail for its own
        stated reason. The spy can.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=0, retention_days=0)
        _fill(log, 3)
        log.flush()
        log._marker_path().unlink(missing_ok=True)
        assert log._list_sealed_indices() == [], "precondition: no sealed segments"

        calls: list[int] = []
        real_mark = type(log)._mark_evicted

        def _spy(self_):
            calls.append(1)
            return real_mark(self_)

        with patch.object(type(log), "_mark_evicted", _spy):
            with log._lock:
                log._discard_leased()

        assert calls == [], (
            f"_mark_evicted was called {len(calls)} time(s) on a host with no "
            f"sealed segments, so nothing was evicted"
        )
        assert not log._marker_path().exists(), (
            "an eviction marker survived on a host that evicted nothing"
        )
        assert not log._has_evicted()

    def test_no_marker_when_the_first_unlink_raises(self, sel_dir):
        """NEGATIVE CONTROL: a raise BEFORE any deletion must not mark either."""
        log, sealed = self._discard_log(sel_dir)

        with patch.object(Path, "unlink", self._refuse_one(log, sealed[0])):
            with pytest.raises(PermissionError):
                with log._lock:
                    log._discard_leased()

        assert log._list_sealed_indices() == sealed, (
            "control precondition: no segment should have been deleted"
        )
        assert not log._marker_path().exists(), (
            "a marker was written although the first unlink raised and nothing "
            "was deleted"
        )
        assert not log._has_evicted()


class TestCrashLeftClaimDoesNotEvictValidHistory:
    """A number claim is created with `O_EXCL` BEFORE the `os.replace` fills it.

    A process killed in that window leaves a zero-byte segment, which inflates the
    eviction budget, so every roll for the life of the install evicts one additional
    VALID segment, silently. Excluding it from the budget is what fixes that.

    It used to be UNLINKED as part of that exclusion, and it no longer is: a
    zero-byte segment is equally consistent with a real segment that was TRUNCATED
    after sealing, nothing on disk distinguishes the two, and deleting it erased the
    only remaining evidence. So the number is dropped from the budget and the file
    stays. The consequence is deliberate and asserted below -- verify now reports the
    residue as unverifiable instead of reading clean over it.
    """

    @staticmethod
    def _crash_left_claim(log):
        """The residue a kill between claim and seal leaves behind."""
        nxt = (log._list_sealed_indices() or [0])[-1] + 1
        fd = os.open(
            log._segment_path(nxt), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        os.close(fd)
        assert log._segment_path(nxt).stat().st_size == 0, "precondition: claim is empty"
        return nxt

    def _at_budget(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=3, retention_days=0)
        _fill(log, 12)
        log.flush()
        sealed = log._list_sealed_indices()
        assert len(sealed) == 3, f"precondition: exactly at budget, got {sealed}"
        return log, sealed

    def test_a_crash_left_claim_does_not_evict_a_valid_segment(self, sel_dir):
        log, sealed = self._at_budget(sel_dir)
        claim = self._crash_left_claim(log)

        with log._lock:
            log._evict_over_budget()

        survivors = log._list_sealed_indices()
        assert set(sealed) <= set(survivors), (
            f"a valid segment was evicted to make room for an empty claim: "
            f"{sorted(set(sealed) - set(survivors))} went, budget was {sealed}"
        )
        assert log._segment_path(claim).exists(), (
            "the empty claim was unlinked; it must be kept, because a zero-byte "
            "segment is indistinguishable from a truncated real one and deleting it "
            "destroys the only evidence"
        )

    def test_bypassing_the_claim_sweep_reproduces_the_eviction(self, sel_dir):
        """NEGATIVE CONTROL: without the sweep, a valid segment really does go.

        This is what makes the test above discriminating rather than vacuous -- at
        budget with no claim there is nothing to evict either way.
        """
        log, sealed = self._at_budget(sel_dir)
        self._crash_left_claim(log)

        with patch.object(
            sel_mod.SecurityEventLog, "_drop_empty_claims", lambda self_, idx: idx
        ):
            with log._lock:
                log._evict_over_budget()

        survivors = log._list_sealed_indices()
        assert not set(sealed) <= set(survivors), (
            "the control did not reproduce the eviction, so the sweep is not what "
            f"prevents it: budget={sealed} survivors={survivors}"
        )

    def test_a_nonempty_segment_is_never_dropped(self, sel_dir):
        """The sweep must key on SIZE only -- never on being the newest number."""
        log, sealed = self._at_budget(sel_dir)
        assert all(log._segment_path(i).stat().st_size > 0 for i in sealed)

        with log._lock:
            kept = log._drop_empty_claims(sealed)

        assert kept == sealed, f"the sweep dropped a segment holding history: {kept}"
        assert all(log._segment_path(i).exists() for i in sealed)

    def test_the_sweep_does_not_sever_the_chain_and_reports_the_residue(self, sel_dir):
        """Its intent is unchanged: excluding the residue must not break the chain.

        Its EXPECTATION changed. This used to assert `valid == total`, i.e. that the
        log reads clean after the sweep -- which was only true because the sweep had
        deleted the residue. That is the behaviour the review called out: an erasure
        that then verifies clean. The residue now survives, so every real record still
        verifies (the chain is intact, which is what this test is for) and the residue
        contributes exactly one unverifiable segment on top.
        """
        log, _ = self._at_budget(sel_dir)
        claim = self._crash_left_claim(log)
        with log._lock:
            log._evict_over_budget()
        assert log._segment_path(claim).exists(), "precondition: the residue is kept"
        total, valid = log.verify_integrity()
        assert total > 1, f"nothing was walked, so the assertion below is vacuous: {total}"
        assert valid == total - 1, (
            f"expected exactly one unverifiable segment (the retained residue), "
            f"got {valid}/{total} -- a larger gap means the sweep severed the chain"
        )


class TestEvictionDeletesTheOldestNotTheNewest:
    """DIRECTIONAL guard for the size cap. Do not weaken this test.

    The ends inverted with the layout. Under shift-renames the oldest segment
    carried the HIGHEST index and eviction ran from the top; with monotonic numbers
    the oldest is the LOWEST and eviction runs from the bottom. Applying the old
    rule here would delete the NEWEST audit history and keep the oldest, which is
    silent and unrecoverable -- so this asserts the direction explicitly rather
    than only asserting that the count is bounded.
    """

    def test_the_highest_numbers_survive_and_the_lowest_are_gone(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=300, backup_count=3)
        _fill(log, 60)
        surviving = log._list_sealed_indices()
        assert len(surviving) == 3, f"backup_count=3 not enforced: {surviving}"
        # Every surviving number is above every evicted one: eviction took a PREFIX.
        assert surviving == sorted(surviving)
        assert min(surviving) > 1, (
            "nothing was evicted from the bottom, so this test proved nothing"
        )
        # The newest segment is the highest number and it MUST still be here.
        assert max(surviving) == max(log._list_sealed_indices())

    def test_the_newest_segment_holds_the_newest_events(self, sel_dir):
        """Chain order follows the numbers, so the top segment is the recent one.

        If eviction ran from the wrong end this still passes, which is why the test
        above exists; this one pins the ordering the direction depends on.
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=4)
        _fill(log, 40)
        indices = log._list_sealed_indices()
        assert len(indices) >= 2, f"need >=2 segments, got {indices}"
        first_of = {}
        for idx in indices:
            raw = log._segment_path(idx).read_text(encoding="utf-8").strip().splitlines()
            first_of[idx] = json.loads(raw[0])["event_id"]
        ordered = [first_of[i] for i in indices]
        assert ordered == sorted(ordered), (
            f"segment numbers do not follow write order: {ordered}"
        )


class TestSegmentNumbersAreMonotonic:
    def test_a_number_is_never_reused_after_eviction(self, sel_dir):
        """Reuse would put an old name in front of newer history.

        Shift-renames reused numbers on every roll, which is the whole reason the
        rotation lease existed. Numbers here are allocated once, so a segment's name
        is stable for its lifetime and eviction leaves a permanent gap.
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=2)
        _fill(log, 40)
        high_water = max(log._list_sealed_indices())
        _fill(log, 20, start=100)
        assert min(log._list_sealed_indices()) > 1, "precondition: eviction happened"
        assert max(log._list_sealed_indices()) > high_water, (
            "numbers must keep rising rather than being reused after eviction"
        )

    def test_no_existing_segment_is_renamed_by_a_roll(self, sel_dir, monkeypatch):
        """The property that retires the cross-process rotation lease.

        The lease serialised renames of EXISTING segments. If a roll renames only
        the active path, two processes cannot destroy each other's history, so the
        assertion is that no sealed segment is ever a rename SOURCE.
        """
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 10)
        assert log._list_sealed_indices(), "precondition: a roll happened"

        sources: list[str] = []
        real_replace = os.replace

        def spy(src_path, dst_path, *args, **kwargs):
            sources.append(os.fspath(src_path))
            return real_replace(src_path, dst_path, *args, **kwargs)

        monkeypatch.setattr(os, "replace", spy)
        _fill(log, 20, start=50)
        segdir = str(_segdir(sel_dir))
        moved_segments = [s for s in sources if s.startswith(segdir)]
        assert not moved_segments, f"a sealed segment was renamed: {moved_segments}"

    def test_claiming_a_number_is_exclusive(self, sel_dir):
        """Two claims must never return the same number.

        ``max(existing)+1`` alone is a read-modify-write: two processes rolling at
        once would compute the same number and the second rename would destroy the
        segment the first had just sealed. The claim uses O_CREAT|O_EXCL, so the
        loser is pushed to the next free number instead.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        claims = [log._next_segment_index() for _ in range(5)]
        assert len(set(claims)) == 5, f"claims collided: {claims}"
        assert claims == sorted(claims)
        # Each claim left a placeholder, which is what makes the next claim skip it.
        for n in claims:
            assert log._segment_path(n).exists()


class TestChainTipScanIsBounded:
    """B2: a planted newline-free segment must not be read into memory whole."""

    def test_a_huge_newline_free_segment_does_not_exhaust_memory(self, sel_dir):
        from kiro_crew.sel import _TIP_SCAN_MAX_BYTES

        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        # No newline anywhere, so no scan position ever yields a complete line.
        seg.write_bytes(b"A" * (_TIP_SCAN_MAX_BYTES + 512 * 1024))

        read_bytes = {"n": 0}
        real_open = sel_mod._open_segment

        class _Counting:
            def __init__(self, fh):
                self._fh = fh

            def read(self, *a):
                data = self._fh.read(*a)
                read_bytes["n"] += len(data)
                return data

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

        def counting_open(path):
            return _Counting(real_open(path))

        sel_mod._open_segment = counting_open
        try:
            tip = log._tip_hash_of(seg)
        finally:
            sel_mod._open_segment = real_open

        assert tip == "", "a segment with no complete record has no tip"
        # One chunk of slack for the final partial read at the floor.
        assert read_bytes["n"] <= _TIP_SCAN_MAX_BYTES + 4096, (
            f"the backward scan is unbounded: read {read_bytes['n']} bytes"
        )

    def test_a_normal_segment_tip_is_still_found(self, sel_dir):
        """POSITIVE CONTROL: the bound must not break ordinary tip discovery."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            json.dumps({"timestamp": _iso(1), "entry_hash": "tip-here"}) + "\n",
            encoding="utf-8",
        )
        assert log._tip_hash_of(seg) == "tip-here"

    def test_the_bail_out_does_not_reset_the_chain_to_genesis(self, sel_dir):
        """A hostile segment must not silently re-anchor the whole chain.

        `_read_last_hash` walks newest->oldest, so giving up on one segment must
        fall through to an older one rather than returning "" for the log.
        """
        from kiro_crew.sel import _TIP_SCAN_MAX_BYTES

        log = _rot_log(sel_dir, max_bytes=0)
        good = _segdir(sel_dir) / "security_events.jsonl.1"
        good.write_text(
            json.dumps({"timestamp": _iso(2), "entry_hash": "older-real-tip"}) + "\n",
            encoding="utf-8",
        )
        bad = _segdir(sel_dir) / "security_events.jsonl.2"
        bad.write_bytes(b"B" * (_TIP_SCAN_MAX_BYTES + 1024))
        (sel_dir / "security_events.jsonl").write_text("", encoding="utf-8")
        assert log._read_last_hash() == "older-real-tip"


class TestActivePruneRefusesToDiscardConcurrentAppends:
    """B3: stage 2 is a read-filter-replace, and `_lock` is in-process only."""

    def test_growth_during_the_filter_skips_the_rewrite(self, sel_dir, monkeypatch):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        _fill(log, 6, ts=_iso(400))  # all aged, so stage 2 would rewrite
        log.flush()
        active = sel_dir / "security_events.jsonl"
        before = active.read_text(encoding="utf-8")
        assert before.strip(), "precondition: the active file has aged entries"

        # Simulate another PROCESS appending while we stream the file: _parse_ts is
        # called per line inside the filter loop.
        real_parse = sel_mod.SecurityEventLog._parse_ts
        state = {"grown": False}

        def grow_once(ts):
            if not state["grown"]:
                state["grown"] = True
                with open(active, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"timestamp": _iso(0), "entry_hash": "fresh"}) + "\n")
            return real_parse(ts)

        monkeypatch.setattr(sel_mod.SecurityEventLog, "_parse_ts", staticmethod(grow_once))
        log.prune(keep_days=30)

        assert state["grown"], "the simulated concurrent append never fired"
        text = active.read_text(encoding="utf-8")
        assert "fresh" in text, (
            "the concurrently appended event was discarded by a stale rewrite"
        )
        # The aged entries are still here: prune SKIPPED rather than half-applying.
        assert before.strip().splitlines()[0] in text

    def test_a_quiet_log_is_still_pruned(self, sel_dir):
        """NEGATIVE CONTROL: the guard must not disable pruning outright.

        Without this, a guard that always skipped would pass the test above while
        silently retiring retention on the active file.
        """
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        _fill(log, 6, ts=_iso(400))
        log.flush()
        removed = log.prune(keep_days=30)
        assert removed == 6, f"aged active entries were not pruned: removed={removed}"


class TestSegmentDirRefusesAPlantedLink:
    """F1: `mkdir(exist_ok=True)` follows a link, so segments would land off-floor.

    The sensitive-path floor protects the registered path `<crew>/sel`, not wherever
    that path happens to point. An agent that plants `sel` as a symlink before this
    feature ships would have every sealed segment written to its own target.
    """

    def test_a_planted_link_does_not_receive_segments(self, sel_dir, tmp_path):
        attacker = tmp_path / "attacker-visible"
        attacker.mkdir()
        link = sel_dir / "sel"
        link.symlink_to(attacker, target_is_directory=True)
        assert link.is_symlink(), "precondition: the link is planted"

        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)

        assert not (sel_dir / "sel").is_symlink(), "the planted link survived"
        assert (sel_dir / "sel").is_dir(), "no real segment directory was created"
        assert log._list_sealed_indices(), "precondition: a roll happened"
        leaked = sorted(p.name for p in attacker.iterdir())
        assert leaked == [], f"audit history was written outside the floor: {leaked}"

    def test_an_ordinary_directory_is_untouched(self, sel_dir):
        """NEGATIVE CONTROL: the guard must not disturb the normal case."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        assert (sel_dir / "sel").is_dir()
        assert not (sel_dir / "sel").is_symlink()
        assert log._list_sealed_indices()

    @staticmethod
    def _plant_linked_segment_dir(sel_dir, tmp_path):
        """Point `<crew>/sel` at an attacker dir holding one AGED segment.

        Aged and parseable on purpose: `_prune_sealed_by_age` fails CLOSED on a
        stamp it cannot parse, so an unparseable one would make the deletion
        assertion below pass even with the guard removed.
        """
        attacker = tmp_path / "attacker-visible"
        attacker.mkdir()
        victim = attacker / f"{sel_mod._SEL_FILE}.1"
        victim.write_text(
            json.dumps({"event_id": "planted-0", "timestamp": _iso(400)}) + "\n",
            encoding="utf-8",
        )
        link = sel_dir / "sel"
        link.unlink(missing_ok=True)
        try:
            link.symlink_to(attacker, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform/filesystem")
        return attacker, victim

    def test_the_read_path_refuses_to_list_through_a_planted_link(self, sel_dir, tmp_path):
        """The WRITE path repairs the link; the READ paths never run that repair.

        The two tests above both ROTATE, so `_ensure_segment_dir` fires and the link
        is gone before anything lists. With rotation off (`max_bytes=0`) nothing
        repairs it, and `_list_sealed_indices` is still reached by
        `verify_integrity`, `recent` and `prune` Stage 1 -- so without the read-side
        refusal `iterdir` enumerates the TARGET and the planted file is treated as
        this log's own sealed segment.
        """
        log = _rot_log(sel_dir, max_bytes=0)  # rotation OFF: nothing repairs the link
        _fill(log, 4)
        log.flush()
        attacker, victim = self._plant_linked_segment_dir(sel_dir, tmp_path)

        assert log._list_sealed_indices() == [], (
            "listed through a symlinked segment dir; the planted file was adopted "
            "as this log's sealed segment"
        )

        # POSITIVE CONTROL: the SAME file in a REAL directory IS listed, so the []
        # above is attributable to the link and not to the name or the stamp.
        (sel_dir / "sel").unlink()
        real = sel_dir / "sel"
        real.mkdir()
        (real / f"{sel_mod._SEL_FILE}.1").write_text(
            victim.read_text(encoding="utf-8"), encoding="utf-8"
        )
        assert log._list_sealed_indices() == [1], (
            "control failed: the planted file is not listable even in a real dir, "
            "so the refusal above proves nothing"
        )

    def test_pruning_does_not_delete_through_a_planted_link(self, sel_dir, tmp_path):
        """Age-pruning unlinks whatever the listing returns -- off-floor if followed."""
        log = _rot_log(sel_dir, max_bytes=0, retention_days=1)
        _fill(log, 4)
        log.flush()
        attacker, victim = self._plant_linked_segment_dir(sel_dir, tmp_path)

        log.prune(keep_days=1)

        assert victim.exists(), (
            "age-pruning deleted a file outside the SEL directory by following the "
            "planted segment-dir link"
        )
        assert sorted(p.name for p in attacker.iterdir()) == [f"{sel_mod._SEL_FILE}.1"]

    def test_recent_does_not_surface_events_through_a_planted_link(self, sel_dir, tmp_path):
        """`recent()` feeds the dashboard, so a followed link is an exposure."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 4)
        log.flush()
        self._plant_linked_segment_dir(sel_dir, tmp_path)

        ids = [e.get("event_id") for e in log.recent(limit=100)]

        assert "planted-0" not in ids, (
            "recent() surfaced a file from outside the SEL directory as an audit event"
        )


class TestSealIsSerializedAcrossProcesses:
    """F2: the atomic number claim does not ORDER the roll.

    Without a lease: A claims N, B claims N+1, B moves the active file onto N+1,
    appends recreate the active file, and A then moves that NEWER data onto N -- so a
    lower number holds newer events. Eviction deletes the lowest numbers, so it would
    drop the newer history first.
    """

    def _hold_lease(self, log):
        """Take the seal lease from a second descriptor, as a rival process would."""
        from kiro_crew import platform_compat

        seg_dir = log._ensure_segment_dir()
        fd = os.open(seg_dir / "seal.lock", os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(fd, exclusive=True), "could not take lease"
        return fd

    def test_rotation_declines_while_a_rival_holds_the_lease(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        sealed_before = log._list_sealed_indices()
        size_before = log._path.stat().st_size
        assert size_before >= 1500, "precondition: the active file is over the cap"

        fd = self._hold_lease(log)
        try:
            with log._lock:
                log._maybe_rotate()  # must decline rather than seal unserialized
        finally:
            from kiro_crew import platform_compat

            platform_compat.release_lock(fd)
            os.close(fd)

        assert log._list_sealed_indices() == sealed_before, "sealed without the lease"
        assert log._path.stat().st_size == size_before, "the active file was resealed"

    def test_the_lease_is_released_after_a_successful_roll(self, sel_dir):
        """Otherwise the first roll would wedge every later one."""
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=100)
        _fill(log, 20)
        assert log._list_sealed_indices(), "precondition: a roll happened"
        fd = self._hold_lease(log)  # asserts internally that it can be taken
        from kiro_crew import platform_compat

        platform_compat.release_lock(fd)
        os.close(fd)

    def test_the_claim_happens_inside_the_lease(self):
        """Structural: claim + replace must both sit under the lease.

        A behavioural test cannot observe the interleaving from one process, so pin
        the containment instead -- otherwise a later edit could hoist the claim back
        out of the lease and every test above would still pass.
        """
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(sel_mod.SecurityEventLog._rotate_now)))
        withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
        leased = [
            w
            for w in withs
            if any("_seal_lease" in ast.dump(item.context_expr) for item in w.items)
        ]
        assert leased, "_rotate_now does not take the seal lease at all"
        inner = ast.dump(ast.Module(body=leased[0].body, type_ignores=[]))
        assert "_seal_leased" in inner, "the seal is not performed inside the lease"


class TestUnreadableSegmentsCannotVerifyClean:
    """F3: a verifier that skips what it cannot read reports a false clean."""

    def _unreadable_segment(self, log, sel_dir):
        _fill(log, 20)
        indices = log._list_sealed_indices()
        assert indices, "precondition: at least one sealed segment"
        seg = log._segment_path(indices[0])
        assert seg.exists()
        return seg

    def test_an_unreadable_segment_forces_valid_less_than_total(self, sel_dir):
        """The chain must NOT be able to absorb the gap on its own.

        Two details make this discriminating rather than vacuous. The OLDEST segment
        is chosen, and the eviction marker is set first, so skipping it leaves a
        chain with no broken link -- with the fold-in removed this reports
        total==valid, i.e. `integrity: ok` while a segment of history is
        unaccounted. Picking a MIDDLE segment instead would fail either way, because
        the segment after the hole chains off an entry the walk never saw, and the
        test would pass against broken code.
        """
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        indices = log._list_sealed_indices()
        assert len(indices) >= 2, f"precondition: multi-segment, got {indices}"
        log._mark_evicted()  # relaxes the genesis anchor, so no break is reported
        seg = log._segment_path(indices[0])  # OLDEST
        # Replace the regular file with a directory: it still EXISTS, and
        # _open_segment refuses a non-regular file -- the same shape as a permission
        # change or an I/O error, without needing to drop privileges in a test.
        seg.unlink()
        seg.mkdir()

        total, valid = log.verify_integrity()
        assert valid < total, "an unreadable segment must never read as a clean chain"

    def test_a_fully_readable_chain_still_reads_clean(self, sel_dir):
        """NEGATIVE CONTROL: the fold-in must not make every chain dirty."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        self._unreadable_segment(log, sel_dir)
        total, valid = log.verify_integrity()
        assert total > 0 and valid == total, f"clean chain reported dirty: {valid}/{total}"

    def test_an_absent_segment_is_not_counted_as_unverified(self, sel_dir):
        """A segment evicted between listing and opening is ordinary, not a fault.

        This is what separates the fix from over-counting: absent contributes
        nothing, present-but-unreadable forces valid<total.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        total, valid = log.verify_integrity()
        assert valid == total and total == 6


class TestVerifySnapshotSurvivesARivalSeal:
    """`_lock` is a threading.Lock, so it orders nothing against another PROCESS.

    A rival seal renames the active file onto a fresh number and only recreates it
    on the next append. Verify that opens the active path inside that window gets
    ENOENT, cannot lstat it, and takes the ordinary "no active file yet" branch --
    while the entries that were in it now live in a segment that was never listed.
    Everything else validates, so `total == valid` reports `integrity: ok` over a
    chain missing a whole segment.
    """

    @staticmethod
    def _seal_on_active_open(log, state):
        """Wrap `_open_segment` so the rival seal lands mid-snapshot, once.

        Injected at the open of the ACTIVE file, which is the exact interleaving:
        the listing already happened, so the number this creates is not in it.
        """
        real_open = sel_mod._open_segment

        def _wrapped(path):
            if Path(path) == log._path and not state["sealed"]:
                state["sealed"] = True
                nxt = (log._list_sealed_indices() or [0])[-1] + 1
                os.replace(log._path, log._segment_path(nxt))
            return real_open(path)

        return _wrapped

    def test_a_rival_seal_mid_snapshot_loses_no_entries(self, sel_dir):
        """Re-taking the snapshot must account for every entry still on disk."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        log.flush()
        assert len(log._list_sealed_indices()) >= 2, "precondition: multi-segment"
        assert _entries_across_segments(sel_dir) == 20, "precondition: all 20 on disk"

        state = {"sealed": False}
        with patch.object(sel_mod, "_open_segment", self._seal_on_active_open(log, state)):
            total, valid = log.verify_integrity()

        assert state["sealed"], "the injected seal never fired; the test proved nothing"
        assert _entries_across_segments(sel_dir) == 20, "the injected seal lost entries"
        assert total == 20, (
            f"verify accounted for {total} of 20 entries: the segment the rival seal "
            f"created was omitted from the snapshot"
        )
        assert valid == total, f"clean chain reported dirty: {valid}/{total}"

    def test_an_unstable_snapshot_cannot_report_a_clean_chain(self, sel_dir):
        """When the retries are exhausted the omission must be LOUD, not silent.

        Pinned to a single attempt so the seal always wins the race. The segment that
        appeared is then counted UNVERIFIED, which is the fail-loud floor -- the one
        outcome that must never collapse to `total == valid`.
        """
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        log.flush()
        log._mark_evicted()  # relax genesis, so a skipped tail cannot self-report

        state = {"sealed": False}
        with (
            patch.object(sel_mod, "_VERIFY_SNAPSHOT_ATTEMPTS", 1),
            patch.object(sel_mod, "_open_segment", self._seal_on_active_open(log, state)),
        ):
            total, valid = log.verify_integrity()

        assert state["sealed"], "the injected seal never fired; the test proved nothing"
        assert valid < total, (
            "a segment sealed by a rival mid-snapshot was dropped silently: "
            f"total=={total} valid=={valid} reads as `integrity: ok`"
        )

    def test_an_uncontended_verify_still_reads_clean(self, sel_dir):
        """NEGATIVE CONTROL: the re-listing must not make every chain dirty."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        log.flush()
        total, valid = log.verify_integrity()
        assert total == 20 and valid == total, f"clean chain reported dirty: {valid}/{total}"


class TestVerifySnapshotDetectsSegmentNumberReuse:
    """Numbering is monotonic only while a sealed segment SURVIVES.

    `_next_segment_index` is `max(existing)+1`, falling back to `1` on an empty set,
    so once the last segment is pruned the next seal REUSES its number. A stability
    check that compares the sealed NUMBER set therefore passes while a number names a
    DIFFERENT file -- and the handle already pinned keeps reading the unlinked inode,
    so verify vouches for history that is no longer retained while never examining
    the history that is.

    The two scenario tests below are POSIX-only, and the reason is the OS, not the
    code under test. They must unlink a segment while verify holds it OPEN -- the
    pinned handle reading the unlinked inode is the whole mechanism -- which POSIX
    allows and Windows refuses with WinError 32. So on Windows the substitution
    cannot be constructed at all: the kernel blocks the prune the race depends on,
    which means the property holds there by a stricter mechanism than the drift
    check. Same reasoning, and the same skip form, as
    TestVerifyPinsSegmentsByHandle above. The discriminator itself is pinned on
    every platform by test_snapshot_drift_detects_a_substituted_file, which drives
    `_snapshot_drift` directly and needs no unlink-under-handle.
    """

    @staticmethod
    def _ids_on_disk(log):
        out = []
        for p in [*log._sealed_segments(), log._path]:
            if p.exists():
                out += [
                    json.loads(ln)["event_id"]
                    for ln in p.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                ]
        return out

    def _log_with_one_sealed_segment(self, sel_dir):
        """6 entries sealed into `.1`, 3 more in the active file. Knobs OFF so the
        test drives rotation itself and nothing rolls behind its back."""
        log = _rot_log(sel_dir, max_bytes=0, backup_count=100, retention_days=0)
        _fill(log, 6)
        log.flush()
        with log._lock:
            log._rotate_now()
        assert log._list_sealed_indices() == [1], "precondition: exactly one sealed segment"
        _fill(log, 3, start=6)
        log.flush()
        return log

    @staticmethod
    def _prune_then_reuse_on_active_open(log, state):
        """Age-prune the only sealed segment and reseal the active file onto its
        number -- the interleaving that makes the number set look unchanged."""
        real_open = sel_mod._open_segment

        def _wrapped(path):
            if Path(path) == log._path and not state["fired"]:
                state["fired"] = True
                log._segment_path(1).unlink()
                log._mark_evicted()  # what a real eviction sets
                os.replace(log._path, log._segment_path(1))
            return real_open(path)

        return _wrapped

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX open-handle unlink semantics; Windows refuses with WinError 32",
    )
    def test_reused_number_does_not_verify_evicted_history_as_clean(self, sel_dir):
        log = self._log_with_one_sealed_segment(sel_dir)
        state = {"fired": False}

        with patch.object(
            sel_mod, "_open_segment", self._prune_then_reuse_on_active_open(log, state)
        ):
            total, valid = log.verify_integrity()

        assert state["fired"], "the injected prune+reseal never ran; the test proved nothing"
        retained = self._ids_on_disk(log)
        assert retained == ["rot-000006", "rot-000007", "rot-000008"], (
            f"precondition drifted: retained set is {retained}"
        )
        # The point: verify must account for the RETAINED history, not the evicted
        # history it happened to be holding a handle to.
        assert total == len(retained), (
            f"verify accounted for {total} entries but only {len(retained)} are "
            f"retained -- it vouched for evicted history and skipped the live segment"
        )
        assert valid == total, f"retained chain reported dirty: {valid}/{total}"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX open-handle unlink semantics; Windows refuses with WinError 32",
    )
    def test_number_only_stability_check_reproduces_the_false_clean(self, sel_dir):
        """NEGATIVE CONTROL: with drift judged by NUMBER alone, the bug returns.

        This is what pins the fix to identity rather than to the retry loop -- the
        retry existed already and did not catch this.
        """
        log = self._log_with_one_sealed_segment(sel_dir)
        state = {"fired": False}

        def number_only(self_, sealed_before, pinned):
            return (
                []
                if self_._list_sealed_indices() == sealed_before
                else [Path("sentinel")]
            )

        with (
            patch.object(sel_mod.SecurityEventLog, "_snapshot_drift", number_only),
            patch.object(
                sel_mod, "_open_segment", self._prune_then_reuse_on_active_open(log, state)
            ),
        ):
            total, valid = log.verify_integrity()

        assert state["fired"], "the injected prune+reseal never ran"
        retained = self._ids_on_disk(log)
        assert total == valid and total != len(retained), (
            "the number-only control did NOT reproduce the false clean, so the "
            f"identity check is not what fixes it: total={total} valid={valid} "
            f"retained={len(retained)}"
        )

    def test_snapshot_drift_detects_a_substituted_file(self, sel_dir):
        """Cross-platform guard for the two POSIX-only scenario tests above.

        Those tests cannot run on Windows, so this drives the discriminator directly
        -- same path, different identity -- which needs no unlink-under-handle and so
        keeps the regression detectable on every platform. It also pins the two cases
        the check must NOT fire on, since an over-eager drift check would retry (and
        on exhaustion report dirty) against a perfectly quiet log.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=100, retention_days=0)
        _fill(log, 3)
        log.flush()
        with log._lock:
            log._rotate_now()
        sealed = log._list_sealed_indices()
        assert sealed == [1], f"precondition: one sealed segment, got {sealed}"
        seg = log._segment_path(1)
        st = os.stat(seg)
        ident = (st.st_dev, st.st_ino)

        # CONTROL: what we pinned is what is on disk -> stable.
        assert log._snapshot_drift(sealed, {seg: ident}) == [], (
            "a quiet log reported drift; the check would retry and then report dirty"
        )
        # SUBSTITUTION: same path, different identity -> drift. This is the prune-then-
        # reuse shape with the OS-specific setup removed.
        assert log._snapshot_drift(sealed, {seg: (st.st_dev, st.st_ino + 1)}) == [seg], (
            "a reused number naming a different file was not detected as drift"
        )
        # BENIGN EVICTION: a pinned path that is simply gone is NOT drift -- the handle
        # still holds the bytes, which is the case the number check already covers.
        assert log._snapshot_drift(sealed, {log._segment_path(99): ident}) == [], (
            "an evicted-but-pinned segment was misreported as drift"
        )

    def test_an_uncontended_verify_is_unaffected(self, sel_dir):
        """NEGATIVE CONTROL: the identity check must not make a quiet log dirty."""
        log = self._log_with_one_sealed_segment(sel_dir)
        total, valid = log.verify_integrity()
        assert total == 9 and valid == 9, f"clean chain reported dirty: {valid}/{total}"


class TestDiscardIsLeasedAndRestatted:
    """C1: the ``backup_count=0`` path UNLINKS the active file, so it needs the
    same lease and re-stat the seal path already has.

    The size that triggers a roll is measured by the unsynchronized pre-check in
    ``_maybe_rotate``. On the seal path acting on a stale measurement only wastes a
    retained slot. On this path it deletes an active file another process has
    already rolled and appends have since recreated -- persisted events, gone.
    """

    def _hold_lease(self, log):
        """Take the seal lease from a second descriptor, as a rival process would."""
        from kiro_crew import platform_compat

        seg_dir = log._ensure_segment_dir()
        fd = os.open(seg_dir / "seal.lock", os.O_CREAT | os.O_RDWR, 0o600)
        assert platform_compat.try_acquire_lock(fd, exclusive=True), "could not take lease"
        return fd

    def _oversized_discard_log(self, sel_dir, cap=1000):
        """A backup_count=0 log whose active file is over the cap.

        Filled with rotation OFF so the fill itself cannot discard, then the cap is
        lowered -- otherwise the log would roll mid-fill and leave nothing to test.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=0)
        _fill(log, 20)
        log.flush()
        size = log._path.stat().st_size
        assert size >= cap, f"precondition: active file over the cap, got {size}"
        log._max_bytes = cap
        return log

    def test_discard_declines_while_a_rival_holds_the_lease(self, sel_dir):
        log = self._oversized_discard_log(sel_dir)
        size_before = log._path.stat().st_size

        fd = self._hold_lease(log)
        try:
            with log._lock:
                log._maybe_rotate()  # must decline rather than unlink unserialized
        finally:
            from kiro_crew import platform_compat

            platform_compat.release_lock(fd)
            os.close(fd)

        assert log._path.exists(), "the active file was discarded without the lease"
        assert log._path.stat().st_size == size_before, "the active file was truncated"

    def test_the_discard_still_happens_when_the_lease_is_free(self, sel_dir):
        """NEGATIVE CONTROL: the lease must not disable backup_count=0 entirely."""
        log = self._oversized_discard_log(sel_dir)
        with log._lock:
            log._maybe_rotate()
        size_after = log._path.stat().st_size if log._path.exists() else 0
        assert size_after == 0, f"backup_count=0 did not discard the log: {size_after} bytes"

    def test_a_stale_size_does_not_unlink_a_freshly_recreated_file(self, sel_dir, monkeypatch):
        """The re-stat is what makes a stale measurement harmless.

        Simulates the real interleaving: the pre-check measures an oversized file,
        then a rival process rolls it and appends recreate a SMALL one before this
        process gets the lease. Without the re-stat the discard unlinks that fresh
        file, so the events appended after the roll are lost.
        """
        from contextlib import contextmanager

        log = self._oversized_discard_log(sel_dir)
        fresh = json.dumps({"event_id": "appended-after-the-rival-roll"}) + "\n"
        assert len(fresh) < log._max_bytes, "precondition: the fresh file is UNDER the cap"

        @contextmanager
        def rival_rolled_first():
            # Runs after _maybe_rotate measured the old size and before the re-stat.
            log._path.write_text(fresh, encoding="utf-8")
            yield True

        monkeypatch.setattr(log, "_seal_lease", rival_rolled_first)
        with log._lock:
            log._maybe_rotate()

        assert log._path.exists(), "the freshly recreated active file was unlinked"
        assert fresh in log._path.read_text(encoding="utf-8"), "fresh events were destroyed"

    def test_the_discard_happens_inside_the_lease(self):
        """Structural: pin the containment so a later edit cannot hoist it out.

        A single-process behavioural test cannot observe the interleaving, so the
        tests above prove the lease is CONSULTED; this proves the destructive call
        sits inside it.
        """
        import ast
        import inspect
        import textwrap

        src = inspect.getsource(sel_mod.SecurityEventLog._rotate_now)
        assert src.count("_discard_leased(") == 1, "expected exactly one discard call site"
        tree = ast.parse(textwrap.dedent(src))
        withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
        leased = [
            w
            for w in withs
            if any("_seal_lease" in ast.dump(item.context_expr) for item in w.items)
        ]
        assert leased, "_rotate_now does not take the seal lease at all"
        inner = ast.dump(ast.Module(body=leased[0].body, type_ignores=[]))
        assert "_discard_leased" in inner, "the discard is not performed inside the lease"
        assert "stat" in inner, "the re-stat is not performed inside the lease"


class TestNewestTimestampScanIsBoundedAndLinear:
    """C2: ``_newest_timestamp_of`` had TWO faults its sibling ``_tip_hash_of``
    does not, and a floor alone fixes only one of them.

    (a) No scan floor: a segment with no newline never yields a complete line, so
    the held-back buffer grows to the whole file. (b) No buffer trim: ``buf`` kept
    every byte read so far, so the split re-split the entire accumulation on every
    4 KB step -- quadratic CPU even inside a bounded window. Both run on the writer
    thread under ``_lock`` (reachable from ``_evict_over_budget`` and
    ``_prune_sealed_by_age``), so either one stalls all audit logging.
    """

    class _Counting:
        """Wraps a segment handle and totals the bytes read through it."""

        def __init__(self, fh, tally):
            self._fh = fh
            self._tally = tally

        def read(self, *a):
            data = self._fh.read(*a)
            self._tally["n"] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

    def test_a_huge_newline_free_segment_is_not_read_whole(self, sel_dir):
        from kiro_crew.sel import _TIP_SCAN_MAX_BYTES

        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_bytes(b"A" * (_TIP_SCAN_MAX_BYTES + 512 * 1024))

        tally = {"n": 0}
        real_open = sel_mod._open_segment
        sel_mod._open_segment = lambda p: self._Counting(real_open(p), tally)
        try:
            assert log._newest_timestamp_of(seg) is None
        finally:
            sel_mod._open_segment = real_open

        # One chunk of slack for the final partial read at the floor.
        assert tally["n"] <= _TIP_SCAN_MAX_BYTES + 4096, (
            f"the backward scan is unbounded: read {tally['n']} bytes"
        )

    def test_the_scan_does_not_re_split_the_whole_buffer_each_step(self, sel_dir):
        """The trim, measured independently of the floor.

        Counts ``json.loads`` attempts over a segment of unparseable lines that
        fits INSIDE the floor, so the floor cannot account for the result. With the
        buffer trimmed each step examines only the lines from the newest chunk, so
        attempts track the line count. Untrimmed, step k re-examines every line
        from steps 1..k, which is the quadratic blow-up.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        line = b"x" * 63 + b"\n"  # unparseable, so the scan never short-circuits
        lines = 3200  # ~200 KB, comfortably inside _TIP_SCAN_MAX_BYTES
        seg.write_bytes(line * lines)

        calls = {"n": 0}
        real_loads = sel_mod.json.loads

        def counting_loads(*a, **kw):
            calls["n"] += 1
            return real_loads(*a, **kw)

        with patch.object(sel_mod.json, "loads", counting_loads):
            assert log._newest_timestamp_of(seg) is None

        # Linear would be ~= `lines`; quadratic is ~25x that at this size.
        assert calls["n"] <= 2 * lines, (
            f"the buffer is not trimmed: {calls['n']} parse attempts for {lines} lines"
        )

    def test_an_ordinary_segment_timestamp_is_still_found(self, sel_dir):
        """POSITIVE CONTROL: neither the floor nor the trim may break normal reads."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        older, newest = _iso(9), _iso(1)
        seg.write_text(
            json.dumps({"timestamp": older, "event_id": "old"}) + "\n"
            + json.dumps({"timestamp": newest, "event_id": "newest"}) + "\n",
            encoding="utf-8",
        )
        assert log._newest_timestamp_of(seg) == newest

    def test_a_record_spanning_a_chunk_boundary_is_still_read(self, sel_dir):
        """The trim holds back the partial first line, so a long record survives.

        A record wider than one 4 KB step is the case a careless trim would break.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        stamp = _iso(2)
        wide = json.dumps({"timestamp": stamp, "pad": "p" * 12000})
        seg.write_text(wide + "\n", encoding="utf-8")
        assert log._newest_timestamp_of(seg) == stamp

    def test_giving_up_keeps_the_segment_rather_than_deleting_it(self, sel_dir):
        """The floor's fall-through must be fail-SAFE for retention.

        None means "cannot prove aged", and `_prune_sealed_by_age` keeps such a
        segment. A floor that returned a stale or default timestamp instead would
        let retention delete a file it never actually read.
        """
        from kiro_crew.sel import _TIP_SCAN_MAX_BYTES

        log = _rot_log(sel_dir, max_bytes=0, retention_days=1)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_bytes(b"C" * (_TIP_SCAN_MAX_BYTES + 1024))
        removed = log._prune_sealed_by_age(keep_days=1)
        assert seg.exists(), "a segment whose age could not be read was deleted"
        assert removed == 0


class TestReadErrorAfterOpenCannotVerifyClean:
    """C3: ``_walk_handles`` skipped a handle it could not READ, and the comment
    there claimed the segment still counted toward ``total``. It did not.

    ``total`` is incremented only PER LINE, inside the loop the skip bypasses, and
    the ``unreadable`` floor was appended at exactly one site -- the segment OPEN
    failure in ``_walk_chain``. So an open-then-read-error segment contributed 0 to
    both counters, giving ``valid == total`` and an ``integrity: ok`` verdict over
    history that was never read.
    """

    def _failing_read_open(self, target):
        """Open normally, then raise OSError on read -- only for *target*.

        This is the distinguishing shape: the OPEN succeeds, so `_walk_chain`'s
        own unreadable accounting never fires and the defect is reachable.
        """
        real_open = sel_mod._open_segment

        class _ReadFails:
            def __init__(self, fh):
                self._fh = fh

            def read(self, *a):
                raise OSError("simulated I/O error on an already-open handle")

            def readline(self, *a):
                # The bounded reader calls readline, not read. A real I/O error on an
                # open descriptor fails both, so raising on both is the faithful spy.
                raise OSError("simulated I/O error on an already-open handle")

            def __getattr__(self, name):
                return getattr(self._fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._fh.__exit__(*exc)

        def opener(path):
            fh = real_open(path)
            return _ReadFails(fh) if Path(path) == Path(target) else fh

        return opener, real_open

    def _multi_segment_log(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        indices = log._list_sealed_indices()
        assert len(indices) >= 2, f"precondition: multi-segment, got {indices}"
        return log, indices

    def test_a_read_error_forces_valid_less_than_total(self, sel_dir):
        """Discriminating shape, for the same reason as the open-failure test.

        The OLDEST segment is targeted and the eviction marker is set first, so
        skipping it leaves a chain with NO broken link -- the next segment's
        prev_hash simply becomes the relaxed baseline. Picking a middle segment
        would report valid<total either way and pass against broken code.
        """
        log, indices = self._multi_segment_log(sel_dir)
        log._mark_evicted()  # relax the genesis anchor, so no break is reported
        target = log._segment_path(indices[0])  # OLDEST

        opener, real_open = self._failing_read_open(target)
        sel_mod._open_segment = opener
        try:
            total, valid = log.verify_integrity()
        finally:
            sel_mod._open_segment = real_open

        assert valid < total, (
            f"a segment that opened but could not be read verified clean: {valid}/{total}"
        )

    def test_the_same_chain_reads_clean_without_the_read_error(self, sel_dir):
        """NEGATIVE CONTROL: proves the assertion above is the injection's effect.

        Same log, same marker, same segment -- only the failing read removed. If
        this reported valid<total too, the test above would prove nothing.
        """
        log, indices = self._multi_segment_log(sel_dir)
        log._mark_evicted()
        total, valid = log.verify_integrity()
        assert total > 0 and valid == total, f"clean chain reported dirty: {valid}/{total}"


class TestSegmentIndexParsingIsAsciiOnly:
    """C1: ``str.isdigit()`` and ``int()`` do not agree on Unicode digits.

    Two different failures come out of that disagreement, and only an ASCII check
    closes both. A superscript is ``isdigit()`` but ``int()`` raises, so a planted
    name crashes the listing -- and the listing is reached from rotation, verify and
    ``recent()``. A non-ASCII DECIMAL digit is worse than a crash: ``int()`` accepts
    it, so the planted file is silently adopted as an existing segment number.
    """

    def test_a_superscript_suffix_is_ignored_rather_than_crashing(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        seg_dir = _segdir(sel_dir)
        (seg_dir / "security_events.jsonl.1").write_text("a\n", encoding="utf-8")
        (seg_dir / "security_events.jsonl.\u00b2").write_text("b\n", encoding="utf-8")
        # Precondition: this really is the disagreeing pair, not a stale assumption.
        assert "\u00b2".isdigit() and not "\u00b2".isascii()
        assert log._list_sealed_indices() == [1]

    def test_a_non_ascii_decimal_digit_does_not_collide_with_a_real_segment(self, sel_dir):
        """The quiet half: int() ACCEPTS this one, so it would fold onto index 3."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg_dir = _segdir(sel_dir)
        (seg_dir / "security_events.jsonl.3").write_text("real\n", encoding="utf-8")
        (seg_dir / "security_events.jsonl.\u0663").write_text("planted\n", encoding="utf-8")
        assert int("\u0663") == 3, "precondition: int() folds this onto 3"
        assert log._list_sealed_indices() == [3], "the planted name was adopted as a number"
        # And the path resolved for 3 must still be the ASCII one.
        assert log._segment_path(3).read_text(encoding="utf-8") == "real\n"

    def test_ordinary_numeric_suffixes_are_still_parsed(self, sel_dir):
        """POSITIVE CONTROL: the ASCII narrowing must not reject real segments."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg_dir = _segdir(sel_dir)
        for i in (1, 2, 10):
            (seg_dir / f"security_events.jsonl.{i}").write_text("x\n", encoding="utf-8")
        assert log._list_sealed_indices() == [1, 2, 10]


class TestEvictionMarkerOpenRefusesNonRegular:
    """C2: the marker open lacked the two guards its sibling ``_open_segment`` has.

    The marker path is agent-writable before the sensitive-path family lands, and the
    relaxation it gates is what stops a head-truncated log from verifying clean. A
    planted non-regular file there was opened and read.
    """

    def test_a_directory_at_the_marker_path_fails_closed(self, sel_dir):
        """A non-regular marker must not authenticate.

        This pins the OUTCOME, and deliberately not the mechanism: reverting the
        S_ISREG check leaves it passing, because ``os.read`` on a directory raises
        EISDIR and the existing handler already returns False. Measured, rather than
        assumed -- so S_ISREG on the marker is consistency with ``_open_segment`` and
        defence in depth, not the fix for a reachable hole. The load-bearing half of
        this change is ``O_NONBLOCK``, pinned below: without it the open itself blocks
        on a planted fifo and nothing downstream ever runs.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        marker = log._marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.unlink(missing_ok=True)
        marker.mkdir()
        assert log._has_evicted() is False, "a non-regular marker must not authenticate"

    def test_a_genuine_marker_still_authenticates(self, sel_dir):
        """POSITIVE CONTROL: the new guards must not break the real marker."""
        log = _rot_log(sel_dir, max_bytes=0)
        log._mark_evicted()
        assert log._has_evicted() is True

    def test_the_marker_open_uses_the_same_flags_as_the_segment_open(self):
        """O_NONBLOCK, pinned structurally rather than with a fifo.

        A behavioural fifo test cannot be written safely here: without the flag the
        open BLOCKS, so the control would hang the suite rather than fail it. Pin the
        flag instead, and require it to match the sibling helper so the two cannot
        drift apart again -- drifting apart is exactly what this fixed.
        """
        import ast
        import inspect
        import textwrap

        def flags_in(fn_name: str) -> set[str]:
            src = textwrap.dedent(inspect.getsource(getattr(sel_mod.SecurityEventLog, fn_name, None)
                                                    or getattr(sel_mod, fn_name)))
            tree = ast.parse(src)
            return {
                n.args[1].value
                for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "getattr"
                and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
            }

        segment = flags_in("_open_segment")
        marker = flags_in("_has_evicted")
        assert "O_NOFOLLOW" in segment and "O_NONBLOCK" in segment, (
            "control failed: the sibling helper no longer names both flags"
        )
        assert marker == segment, f"marker flags {marker} drifted from segment flags {segment}"
        assert "S_ISREG" in inspect.getsource(sel_mod.SecurityEventLog._has_evicted), (
            "the marker open does not assert a regular file"
        )


class TestSegmentReadIsLineBounded:
    """C4b: the verify walk slurped a whole segment with ``fh.read()``.

    ``_open_segment`` refuses a symlink, fifo and device, but a large REGULAR file
    passes all of it -- the same gap that needed a separate bound for the chain-tip
    scan and for the eviction marker. Here it was one allocation the size of the
    file, so a planted newline-free segment raised MemoryError out of
    ``verify_integrity()`` rather than being reported as unverifiable.
    """

    def test_an_over_cap_line_raises_rather_than_allocating(self, sel_dir):
        cap = sel_mod._SEGMENT_LINE_CAP
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_bytes(b"A" * (cap + 4096))  # no newline anywhere
        with _open_segment(seg) as fh:
            with pytest.raises(OSError):
                # Consumed, not merely called: the helper yields, so its body does not
                # run until advanced and the bare call cannot raise. Every real call
                # site iterates, so this is the shape they actually exercise.
                list(sel_mod._segment_lines(fh))

    def test_a_planted_segment_takes_the_unreadable_path(self, sel_dir, caplog):
        """Assert the PATH taken, because the verdict alone cannot discriminate.

        `valid < total` holds either way: without the cap the newline-free file is
        split into two enormous unparseable chunks, and those already count toward
        `total` and never toward `valid`. What the cap changes is that the segment is
        refused loudly and folded in through the `unreadable` floor instead of being
        allocated whole and counted as garbage records -- so the log line is the
        discriminating observation, and the memory bound is the actual point.
        """
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        indices = log._list_sealed_indices()
        assert indices, "precondition: at least one sealed segment"
        planted = log._segment_path(max(indices) + 1)
        planted.write_bytes(b"B" * (sel_mod._SEGMENT_LINE_CAP + 4096))

        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()

        assert valid < total, f"a planted segment verified clean: {valid}/{total}"
        assert any("could not read segment" in r.getMessage() for r in caplog.records), (
            "the planted segment was parsed as records rather than refused"
        )

    def test_an_ordinary_chain_still_verifies_clean(self, sel_dir):
        """NEGATIVE CONTROL: the cap must not make every chain dirty."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 20)
        total, valid = log.verify_integrity()
        assert total > 0 and valid == total, f"clean chain reported dirty: {valid}/{total}"

    def test_a_long_but_legitimate_record_is_still_read(self, sel_dir):
        """A fat record must survive: the cap is far above any real entry."""
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        payload = json.dumps({"timestamp": _iso(1), "pad": "p" * 200_000})
        seg.write_text(payload + "\n", encoding="utf-8")
        with _open_segment(seg) as fh:
            lines = list(sel_mod._segment_lines(fh))
        assert len(lines) == 1 and json.loads(lines[0])["pad"].startswith("p")


class TestEverySegmentReadIsBounded:
    """C4b remainder: the per-line cap reached one of three call sites.

    Bounding only the verify walk left ``_count_entries_in`` and ``recent()``
    slurping a whole segment, so the same planted file still had two ways in. All
    three now go through ``_segment_lines``, the way every segment OPEN goes through
    ``_open_segment``.
    """

    @staticmethod
    def _plant_oversized(sel_dir, index=1):
        seg = _segdir(sel_dir) / f"security_events.jsonl.{index}"
        seg.write_bytes(b"A" * (sel_mod._SEGMENT_LINE_CAP + 4096))  # no newline
        return seg

    def test_the_entry_count_degrades_to_zero_rather_than_allocating(self, sel_dir):
        """Its docstring already promises 0 on a read error; now that covers this."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg = self._plant_oversized(sel_dir)
        with _open_segment(seg) as fh:
            assert log._count_entries_in(fh) == 0

    def test_the_entry_count_still_counts_a_real_segment(self, sel_dir):
        """POSITIVE CONTROL: the bound must not zero out legitimate counts."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text("a\n\nb\nc\n", encoding="utf-8")
        with _open_segment(seg) as fh:
            assert log._count_entries_in(fh) == 3

    def test_recent_skips_a_planted_segment_instead_of_crashing(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 12)
        indices = log._list_sealed_indices()
        assert indices, "precondition: at least one sealed segment"
        self._plant_oversized(sel_dir, max(indices) + 1)
        events = log.recent(limit=5)
        assert events, "recent() returned nothing -- the planted segment broke the read"

    def test_recent_survives_a_non_utf8_byte(self, sel_dir):
        """A latent second bug the switch closes.

        ``recent()`` decoded with a bare ``decode("utf-8")``, and
        ``UnicodeDecodeError`` is not an ``OSError``, so one stray byte in any
        segment escaped the handler and took down the events endpoint.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        log.flush()
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_bytes(b'{"event_id": "\xff\xfe bad bytes"}\n')
        log.recent(limit=5)  # must not raise

    def test_no_unbounded_segment_read_survives_in_the_module(self):
        """Structural: the guard that stops a fourth call site being added.

        Three separate sites each slurped a whole segment, and two of them were
        missed when the first was bounded. A behavioural test per site cannot catch a
        site that does not exist yet, which is exactly how this recurred.
        """
        import inspect
        import re

        src = inspect.getsource(sel_mod)
        # Positive control: the detector must see the bounded form it permits,
        # otherwise "no bare read survives" would pass vacuously on a typo.
        assert re.search(r"_segment_lines\(fh\)", src), "detector found no bounded read"
        offenders = [
            ln.strip()
            for ln in src.splitlines()
            if re.search(r"\bfh\.read\(\)", ln) or re.search(r"\.read\(\)\.decode", ln)
        ]
        assert not offenders, f"unbounded whole-segment read(s) reintroduced: {offenders}"


class TestRotationIsHandedOffTheEventLoop:
    """A synchronous audit write can land on the asyncio loop thread.

    `critical=True` (and `sync=True`) write inline on the CALLER's thread, and at
    least one caller reaches that path directly from an `async def` with no
    executor hop, so rotation must not do its filesystem work there.
    """

    def test_an_audit_written_on_the_loop_rotates_on_another_thread(self, sel_dir):
        import asyncio

        log = _rot_log(sel_dir, max_bytes=1500, backup_count=5)
        _fill(log, 30)
        log.flush()

        seen: list[threading.Thread] = []
        real = SecurityEventLog._maybe_rotate

        def _spy(inner_self):
            seen.append(threading.current_thread())
            return real(inner_self)

        loop_thread: dict[str, threading.Thread] = {}

        async def _drive():
            loop_thread["t"] = threading.current_thread()
            log.log(_make_event(event_id="on-loop-0001"))

        with patch.object(SecurityEventLog, "_maybe_rotate", _spy):
            asyncio.run(_drive())
            # The hand-off is a helper thread, so give it a bounded moment to take
            # _lock and run. Polling rather than sleeping keeps the test fast.
            deadline = time.time() + 10
            while not seen and time.time() < deadline:
                time.sleep(0.01)

        assert seen, "rotation never ran at all -- the test proved nothing"
        # The poll above releases as soon as the spy is ENTERED, so the helper is
        # still inside the real _maybe_rotate: the seal rename, eviction, age prune
        # and the metrics emit are all pending, and all of them touch sel_dir. Let it
        # finish before the fixture removes that directory and a sibling fixture
        # resets the singleton, or the leak lands on whichever test runs next.
        rotator = seen[0]
        rotator.join(timeout=10)
        assert not rotator.is_alive(), (
            f"the rotation thread {rotator.name!r} outlived the test and is still "
            "working inside the per-test directory"
        )
        assert loop_thread["t"] not in seen, (
            "rotation ran ON the asyncio event loop thread; it must be handed off"
        )

    def test_off_the_loop_callers_still_rotate_inline(self, sel_dir):
        """Negative control: the hand-off must not fire where blocking is fine.

        Without this, routing everything to a helper thread would pass the test
        above while changing the background writer and CLI paths too.
        """
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=5)
        _fill(log, 30)
        log.flush()

        seen: list[threading.Thread] = []
        real = SecurityEventLog._maybe_rotate

        def _spy(inner_self):
            seen.append(threading.current_thread())
            return real(inner_self)

        with patch.object(SecurityEventLog, "_maybe_rotate", _spy):
            log.log(_make_event(event_id="off-loop-0001"))

        assert seen, "rotation never ran"
        assert seen[0] is threading.current_thread(), (
            "an off-loop caller was deferred to a helper thread; it should rotate inline"
        )


class TestPartialEvictionStillMarks:
    def test_a_mid_loop_unlink_failure_still_marks_the_eviction(self, sel_dir):
        """Deletions must never stand without the eviction marker.

        `missing_ok=True` suppresses only FileNotFoundError, so a permission error
        on a later segment leaves the loop -- and `_flush_batch` swallows it -- so a
        marker written after the loop would never be written while the earlier
        deletions stand, and verify would report a genesis break with nothing logged.
        """
        log = _rot_log(sel_dir, max_bytes=100000, backup_count=1, retention_days=0)
        entry = json.dumps({"timestamp": _iso(400), "event_id": "seg"}) + "\n"
        for idx in (1, 2, 3):
            seg = log._segment_path(idx)
            seg.parent.mkdir(parents=True, exist_ok=True)
            seg.write_text(entry, encoding="utf-8")

        assert not log._marker_path().exists(), "precondition: no marker yet"

        calls = {"n": 0}
        real_unlink = Path.unlink

        def _flaky(self_path, *a, **kw):
            # Only count SEGMENT unlinks (digit suffix): the marker file is
            # `security_events.jsonl.evicted`, which a prefix match would catch.
            tail = self_path.name.rsplit(".", 1)[-1]
            if self_path.name.startswith("security_events.jsonl.") and tail.isdigit():
                calls["n"] += 1
                if calls["n"] == 2:
                    raise PermissionError("simulated sharing violation")
            return real_unlink(self_path, *a, **kw)

        with patch.object(Path, "unlink", _flaky):
            with pytest.raises(PermissionError):
                with log._lock:
                    log._evict_over_budget()

        assert calls["n"] == 2, f"expected 2 segment unlinks, saw {calls['n']}"
        assert log._marker_path().exists(), (
            "a segment was deleted but the eviction marker was never written"
        )


class TestZeroBackupDiscardDoesNotDestroyAConcurrentWrite:
    def test_an_append_on_an_already_open_fd_survives_the_discard(self, sel_dir):
        """A writer holding the active fd must not lose its bytes to a discard.

        The append path opens with O_APPEND, so after an UNLINK a concurrent
        writer's bytes go to an orphaned inode and vanish silently. Truncating in
        place keeps the fd pointing at the live file, so the write lands. This does
        not make multi-writer append CORRECT -- it stops the discard destroying a
        write that already succeeded.
        """
        log = _rot_log(sel_dir, max_bytes=1500, backup_count=0)
        _fill(log, 10)
        log.flush()
        assert log._path.exists(), "precondition: active file present"

        # Stand in for the rival process: hold an O_APPEND fd across the discard.
        fd = os.open(log._path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            with log._lock:
                log._discard_leased()
            os.write(fd, b'{"event_id":"rival-after-discard"}\n')
        finally:
            os.close(fd)

        assert log._path.exists(), "the active file should still exist after a discard"
        body = log._path.read_text(encoding="utf-8")
        assert "rival-after-discard" in body, (
            "the concurrent append was destroyed by the discard"
        )


class TestGpt56Round26Blockers:
    """Three independent reads of a planted or corrupt segment, one test each.

    They share a shape -- a segment the process did not write itself -- but they fail
    in three different directions: an unbounded read, a deletion of recent evidence,
    and a crash in the CLI. A single test covering "a bad segment" would leave two of
    them able to regress while it still passed.
    """

    def test_symlinked_segment_is_refused_when_nofollow_is_unavailable(
        self, sel_dir, tmp_path, monkeypatch
    ):
        """`S_ISREG` cannot cover for a missing `O_NOFOLLOW`.

        `fstat` runs on a descriptor the open already followed, so it reports the
        TARGET: a link aimed at a regular file passes it. Windows has no
        `O_NOFOLLOW`, so that is the platform where the guard silently vanished.
        Simulated here by removing the flag rather than claimed from a Windows run
        this host cannot do.
        """
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
        assert not hasattr(os, "O_NOFOLLOW"), "the branch under test was not reached"
        victim = tmp_path / "big_regular_file.txt"
        victim.write_text("planted\n" * 10, encoding="utf-8")
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.unlink(missing_ok=True)
        try:
            seg.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation unavailable on this platform/filesystem")
        assert stat.S_ISREG(os.stat(seg).st_mode), (
            "the target is not a regular file, so S_ISREG would have refused it "
            "anyway and this test would prove nothing"
        )
        with pytest.raises(OSError):
            _open_segment(seg)
        # Control: a REAL segment still opens with the flag removed, so the raise
        # above is the symlink and not the missing flag breaking every read.
        # write_bytes, NOT write_text: see the sibling control in
        # TestSegmentReadsRefuseNonRegularFiles -- text mode would make this
        # b"y\r\n" on Windows, which is the one platform this test exists for.
        real = _segdir(sel_dir) / "security_events.jsonl.2"
        real.write_bytes(b"y\n")
        with _open_segment(real) as fh:
            assert fh.read() == b"y\n"

    def test_corrupt_newest_record_does_not_make_a_recent_segment_look_aged(self, sel_dir):
        """A crash-truncated tail must not resolve to the older record behind it."""
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            json.dumps({"timestamp": _iso(500), "entry_hash": "aged"})
            + "\n"
            + '{"timestamp": "%s", "entry_ha' % _iso(0)
            + "\n",
            encoding="utf-8",
        )
        assert log._newest_timestamp_of(seg) is None, (
            "the walk fell through to the aged record, so this segment reports an "
            "age its newest data contradicts"
        )
        with log._lock:
            removed = log._prune_sealed_by_age(30)
        assert removed == 0
        assert seg.exists(), "age pruning deleted a segment whose tail it could not read"

    def test_recent_skips_non_object_json_lines(self, sel_dir):
        """`recent()` is annotated -> list[dict]; every consumer calls .get."""
        log = _rot_log(sel_dir, max_bytes=0)
        log.log_tool_invocation(
            session_key="cli_chat",
            tool_name="t",
            outcome="approved",
        )
        log.flush()
        with open(log._path, "a", encoding="utf-8") as fh:
            fh.write("123\n")
        events = log.recent(limit=10)
        assert events, "the real event vanished, so this test proves nothing"
        assert all(isinstance(e, dict) for e in events), (
            "a bare scalar survived into the result; the events CLI calls .get on "
            "these and would raise AttributeError"
        )
        # The CLI's own access pattern, so the assertion above is not merely a type
        # claim about a value nothing reads.
        assert [e.get("operation") for e in events] == ["t"]


class TestDeferredRotationStillEmitsCounters:
    """The loop-thread hand-off must not silence the eviction telemetry.

    `_flush_batch` only SPAWNS the rotate thread, so its own post-lock comparison
    runs before that thread has evicted anything. Left there, `early_eviction.count`
    -- the signal that audit evidence was dropped before its retention window --
    reads as no-change on every deferred roll, i.e. on the path most gateway audits
    take.
    """

    @staticmethod
    def _recording_recorder(monkeypatch):
        seen: list[tuple[str, int]] = []

        class _Rec:
            def counter(self, name, value=1):
                seen.append((name, value))

        provider = type(sys)("kiro_crew.metrics.provider")
        provider.get_recorder = lambda: _Rec()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kiro_crew.metrics.provider", provider)
        return seen

    def test_deferred_rotation_emits_the_early_eviction_counter(self, sel_dir, monkeypatch):
        seen = self._recording_recorder(monkeypatch)
        log = _rot_log(sel_dir, max_bytes=1, backup_count=1, retention_days=365)
        # Force the deferred branch without needing a real event loop.
        monkeypatch.setattr(type(log), "_on_event_loop", staticmethod(lambda: True))
        # Two rolls with backup_count=1 so the second evicts a segment the retention
        # window still wants -- which is what increments _early_evictions.
        for i in range(3):
            log.log_tool_invocation(
                session_key="cli_chat", tool_name=f"t{i}", outcome="approved"
            )
            log.flush()
            for t in threading.enumerate():
                if t.name == "sel-rotate":
                    t.join(timeout=10)
        assert log._early_evictions > 0, (
            "no early eviction happened, so this test cannot observe the counter "
            "and proves nothing"
        )
        names = [n for n, _ in seen]
        assert "kirocrew.sel.early_eviction.count" in names, (
            "the deferred roll evicted retained evidence and emitted no counter; "
            f"observed={names}"
        )

    def test_inline_rotation_still_emits_exactly_once(self, sel_dir, monkeypatch):
        """NEGATIVE CONTROL: the off-loop path must not lose or double its emit."""
        seen = self._recording_recorder(monkeypatch)
        log = _rot_log(sel_dir, max_bytes=1, backup_count=1, retention_days=365)
        monkeypatch.setattr(type(log), "_on_event_loop", staticmethod(lambda: False))
        for i in range(3):
            log.log_tool_invocation(
                session_key="cli_chat", tool_name=f"t{i}", outcome="approved"
            )
            log.flush()
        assert log._early_evictions > 0, "no early eviction to observe"
        evictions = [v for n, v in seen if n == "kirocrew.sel.early_eviction.count"]
        assert evictions, "the inline path stopped emitting"
        assert sum(evictions) == log._early_evictions, (
            f"emitted {sum(evictions)} for {log._early_evictions} early evictions"
        )


class TestAppendSurvivesConcurrentSealAndPrune:
    """A rotation in another PROCESS must not silently swallow an in-flight append.

    The append path holds no cross-process lease -- taking one per append would put
    a file lock on the hot path -- so a rival can seal the active file and then
    age-prune the resulting segment while our fd is open. A seal ALONE is harmless
    (rename keeps the link, and the record lands in that segment correctly
    chained); a seal FOLLOWED by the prune's unlink leaves the bytes reachable by
    nobody. The rival is driven from inside the real append window rather than
    simulated after the fact.
    The race is POSIX-ONLY, so the two tests that construct it are skipped on
    Windows: it refuses both to rename and to unlink a file any process still holds
    open, which is exactly why ``_fd_is_unlinked`` can never see ``st_nlink == 0``
    there. Constructing it anyway makes the seal raise ``WinError 32`` from inside
    the append's own ``with``, so the append is abandoned and the assertion fails on
    a fixture that cannot exist on that platform rather than on the behaviour under
    test. The Windows side is covered instead by
    ``test_a_seal_that_fails_does_not_lose_the_append``, which runs everywhere.
    """

    @staticmethod
    def _rival_seals_and_prunes(monkeypatch, log, sel_dir):
        """Seal + age-prune from inside the append's open->write window, once."""
        fired = {"done": False}
        real = type(log)._ends_without_newline

        def hook(self):
            if not fired["done"]:
                fired["done"] = True
                seg = _segdir(sel_dir) / "security_events.jsonl.1"
                os.replace(self._path, seg)  # the seal another process performs
                seg.unlink()                 # the age prune that strands our fd
            return real(self)

        monkeypatch.setattr(type(log), "_ends_without_newline", hook)
        return fired

    @pytest.mark.skipif(os.name == "nt", reason="POSIX inode semantics: Windows refuses to rename or unlink an open file")
    def test_stranded_append_is_rewritten_and_still_readable(self, sel_dir, monkeypatch):
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=30)
        log.log_tool_invocation(session_key="cli_chat", tool_name="before", outcome="approved")
        log.flush()
        fired = self._rival_seals_and_prunes(monkeypatch, log, sel_dir)
        log.log_tool_invocation(session_key="cli_chat", tool_name="stranded", outcome="approved")
        log.flush()
        assert fired["done"], "the rival never ran, so the race was never created"
        ops = [e.get("operation") for e in log.recent(limit=20)]
        assert "stranded" in ops, (
            "the audit record written during a concurrent seal+prune is unreachable; "
            f"observed operations={ops}"
        )

    def test_a_normal_append_is_not_written_twice(self, sel_dir):
        """NEGATIVE CONTROL: a spurious strand verdict would DUPLICATE audit records."""
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=30)
        for name in ("a", "b", "c"):
            log.log_tool_invocation(session_key="cli_chat", tool_name=name, outcome="approved")
        log.flush()
        ops = [e.get("operation") for e in log.recent(limit=50)]
        assert sorted(ops) == ["a", "b", "c"], f"records duplicated or lost: {ops}"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX inode semantics: Windows refuses to rename or unlink an open file")
    def test_a_seal_without_a_prune_needs_no_rewrite(self, sel_dir, monkeypatch):
        """The other half of the discrimination: rename alone must NOT count as loss.

        Without this, a fix keyed on 'the path changed' rather than on the link count
        would re-append on every concurrent roll and double those records.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=365)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        fired = {"done": False}
        real = type(log)._ends_without_newline

        def hook(self):
            if not fired["done"]:
                fired["done"] = True
                os.replace(self._path, seg)  # seal only -- no unlink
            return real(self)

        monkeypatch.setattr(type(log), "_ends_without_newline", hook)
        log.log_tool_invocation(session_key="cli_chat", tool_name="sealed", outcome="approved")
        log.flush()
        assert fired["done"], "the rival never ran"
        ops = [e.get("operation") for e in log.recent(limit=20)]
        assert ops.count("sealed") == 1, (
            f"a seal without a prune loses or duplicates the record: {ops}"
        )

    def test_a_seal_that_fails_does_not_lose_the_append(self, sel_dir, monkeypatch):
        """The WINDOWS side of the same family, and it runs on every platform.

        On Windows a rival that holds the active file open makes OUR seal raise
        ``WinError 32`` instead of stranding our fd -- so the loss the sibling tests
        guard against cannot happen, but a different one could: if a refused seal
        took the append down with it, every audit event during a contended roll
        would vanish. Rotation runs BEFORE the append in ``_flush_batch``, so the
        refusal must degrade to "appended without rotating" and nothing more.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=365)
        # Give the active file real content FIRST. With an absent or 0-byte file
        # _maybe_rotate returns before the seal, so the refusal below would never
        # fire and this test would pass no matter what the guard does.
        log.log_tool_invocation(session_key="cli_chat", tool_name="priming", outcome="approved")
        log.flush()
        assert log._path.stat().st_size > 1, "no content to roll, so no seal is attempted"
        log._max_bytes = 1  # now the next flush WILL try to seal
        real = os.replace
        refused = {"n": 0}

        def refuse_the_seal(src, dst, *a, **kw):
            if str(src).endswith("security_events.jsonl") and f"{os.sep}sel{os.sep}" in str(dst):
                refused["n"] += 1
                raise PermissionError(32, "used by another process")
            return real(src, dst, *a, **kw)

        monkeypatch.setattr(os, "replace", refuse_the_seal)
        log.log_tool_invocation(session_key="cli_chat", tool_name="kept", outcome="approved")
        log.flush()
        assert refused["n"] > 0, (
            "the seal was never attempted, so this test never exercised the refusal"
        )
        ops = [e.get("operation") for e in log.recent(limit=20)]
        assert ops.count("kept") == 1, (
            f"a refused seal lost or duplicated the audit record it raced: {ops}"
        )
        assert not list(_segdir(sel_dir).glob("security_events.jsonl.[0-9]*")), (
            "a refused seal still left a sealed segment behind"
        )
        # The roll did NOT happen -- that is the correct outcome, and the active
        # file must still hold the record rather than a half-sealed segment.
        assert log._path.exists(), "the active log vanished on a refused seal"
        assert "kept" in log._path.read_text(encoding="utf-8")

    # ---- a re-append that cannot place the bytes must not report success ----
    #
    # `critical=True` is the audit-or-deny contract: the caller writes the audit
    # FIRST and only performs the action (a permission grant) if the write did not
    # raise. So if the re-append silently gives up, that caller grants the
    # permission with no record of it anywhere -- fail-OPEN, the one direction the
    # contract exists to prevent. Both ways the retry can fail are covered, plus
    # the opposite error: raising at a best-effort caller that asked not to.

    @staticmethod
    def _strand_then_break_the_retry(monkeypatch, log, sel_dir):
        """Strand the first append, then make the retry's open fail."""
        fired = {"done": False}
        real_ends = type(log)._ends_without_newline
        real_open = os.open

        def hook(self):
            if not fired["done"]:
                fired["done"] = True
                seg = _segdir(sel_dir) / "security_events.jsonl.1"
                os.replace(self._path, seg)
                seg.unlink()
            return real_ends(self)

        def failing_open(path, *a, **k):
            if fired["done"] and str(path).endswith("security_events.jsonl"):
                raise PermissionError(13, "re-append target unwritable")
            return real_open(path, *a, **k)

        monkeypatch.setattr(type(log), "_ends_without_newline", hook)
        monkeypatch.setattr(os, "open", failing_open)
        return fired

    @pytest.mark.skipif(os.name == "nt", reason="POSIX inode semantics: Windows refuses to rename or unlink an open file")
    def test_a_critical_audit_raises_when_the_retry_cannot_be_written(self, sel_dir, monkeypatch):
        """The retry's open fails -> the fail-closed caller must SEE the failure."""
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=30)
        log.log_tool_invocation(session_key="cli_chat", tool_name="before", outcome="approved")
        log.flush()
        tip_before = log._last_hash
        fired = self._strand_then_break_the_retry(monkeypatch, log, sel_dir)
        with pytest.raises(OSError):
            log.log_tool_invocation(
                session_key="cli_chat", tool_name="grant", outcome="approved", critical=True
            )
        assert fired["done"], "the rival never ran, so the race was never created"
        assert log._last_hash == tip_before, (
            "the chain tip still points at records no reader can reach, so the next "
            "batch will chain off a hash that is not on disk"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX inode semantics: Windows refuses to rename or unlink an open file")
    def test_a_critical_audit_raises_when_the_retry_is_stranded_too(self, sel_dir, monkeypatch):
        """The other failure mode: the retry writes, but is stranded in turn.

        Nothing raises on this path -- the loss is DETECTED, not signalled -- so it
        needs its own test. `_reappend_stranded` performs a bare write with no
        `_ends_without_newline` call, so there is no hook inside its window; the
        detector itself is stubbed to report the second strand instead.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=30)
        log.log_tool_invocation(session_key="cli_chat", tool_name="before", outcome="approved")
        log.flush()
        monkeypatch.setattr(sel_mod, "_fd_is_unlinked", lambda fd: True)
        with pytest.raises(OSError):
            log.log_tool_invocation(
                session_key="cli_chat", tool_name="grant", outcome="approved", critical=True
            )

    def test_a_best_effort_append_still_does_not_raise_when_the_retry_fails(self, sel_dir, monkeypatch):
        """NEGATIVE CONTROL for the opposite error: over-eager raising.

        A default (non-critical) caller deliberately asked for best-effort, and the
        async writer thread must survive a log hiccup rather than die on it. Making
        the retry failure propagate unconditionally would turn every such caller
        into a hard failure -- so this must stay silent on the same fixture the
        fail-closed test raises on.
        """
        log = _rot_log(sel_dir, max_bytes=0, backup_count=5, retention_days=30)
        log.log_tool_invocation(session_key="cli_chat", tool_name="before", outcome="approved")
        log.flush()
        monkeypatch.setattr(sel_mod, "_fd_is_unlinked", lambda fd: True)
        log.log_tool_invocation(session_key="cli_chat", tool_name="best-effort", outcome="approved")
        log.flush()  # must not raise


class TestATruncatedSealedSegmentCannotVerifyClean:
    """A sealed segment emptied after sealing must not be silently walked past.

    `_read_last_hash` deliberately falls back newest->oldest, because right after a
    rotation the ACTIVE file is legitimately empty while the tip lives in `.1`. The
    hazard is that the same fallback swallowed a SEALED segment truncated to zero:
    the chain re-anchored to an older tip and verify_integrity reported `total ==
    valid` over history that was gone -- destroying the one property the local chain
    provides, tamper-EVIDENCE. Sealing only ever moves a NON-EMPTY active file onto a
    number, so a recordless sealed segment is proof of truncation, and that is the
    discrimination the fix keys on.
    """

    @staticmethod
    def _seal_current(log, index):
        log.flush()
        log._path.replace(_segdir(log._path.parent) / f"security_events.jsonl.{index}")

    def test_a_truncated_newest_sealed_segment_does_not_verify_clean(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _segdir(sel_dir)
        _fill(log, 2)
        self._seal_current(log, 1)
        _fill(log, 2, start=2)
        self._seal_current(log, 2)
        healthy_total, healthy_valid = _rot_log(sel_dir, max_bytes=0).verify_integrity()
        assert healthy_total == healthy_valid == 4, (
            f"the fixture was not healthy to begin with: {healthy_total}/{healthy_valid}"
        )

        (_segdir(sel_dir) / "security_events.jsonl.2").write_text("", encoding="utf-8")

        total, valid = _rot_log(sel_dir, max_bytes=0).verify_integrity()
        assert valid < total, (
            "verify_integrity reported a clean chain after the newest sealed segment "
            f"was truncated; its history is omitted with no signal ({total}/{valid})"
        )

    def test_an_empty_active_file_after_rotation_still_verifies_clean(self, sel_dir):
        """OPPOSITE DIRECTION: the legitimate case the fallback exists for.

        Removing the fallback outright -- the literal remedy -- would make this fail
        with a manufactured break at every rotation seam, which is the false alarm the
        fallback was written to prevent. Only the ACTIVE file may be empty here.
        """
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        self._seal_current(log, 1)
        log._path.write_text("", encoding="utf-8")  # post-rotation empty active file

        total, valid = _rot_log(sel_dir, max_bytes=0).verify_integrity()
        assert total == valid == 3, (
            f"an empty active file after rotation was reported as a defect: {total}/{valid}"
        )


class TestRecentAuthenticatesSealedRecords:
    """`recent()` feeds `/api/sel/events` and `kirocrew security events`.

    A numbered segment is a plain file in a directory an agent may already be able to
    write, so one can be PLANTED. Parsing it without checking the MAC lets an
    attacker-authored approval surface as an audit record on a security surface. The
    drop-and-continue contract is preserved: `recent()` is annotated `-> list[dict]`
    and its consumers call `.get` on every element, so a bad record is skipped rather
    than raised through the API.
    """

    @staticmethod
    def _plant(sel_dir, *, operation, entry_hash="deadbeef"):
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            json.dumps(
                {
                    "event_id": "planted-1",
                    "timestamp": _iso(0),
                    "event_type": "tool_approval",
                    "caller_identity": "attacker",
                    "agent": "x",
                    "source": "cli",
                    "operation": operation,
                    "outcome": "approved",
                    "prev_hash": "",
                    "entry_hash": entry_hash,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return seg

    def test_a_planted_sealed_record_is_not_served_as_an_audit_event(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        log.log_tool_invocation(session_key="cli_chat", tool_name="genuine", outcome="approved")
        log.flush()
        self._plant(sel_dir, operation="forged-approval")

        ops = [e.get("operation") for e in log.recent(limit=50)]

        assert "forged-approval" not in ops, (
            f"an unauthenticated planted segment was served as an audit event: {ops}"
        )
        assert "genuine" in ops, f"authentication dropped a real record too: {ops}"

    def test_genuine_sealed_records_survive_authentication(self, sel_dir):
        """OPPOSITE DIRECTION: rejecting everything would empty the security surface."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        log.flush()
        log._path.replace(_segdir(sel_dir) / "security_events.jsonl.1")
        log.log_tool_invocation(session_key="cli_chat", tool_name="active-one", outcome="approved")
        log.flush()

        events = log.recent(limit=50)

        assert len(events) == 4, (
            f"authentication dropped genuine sealed records: {len(events)} of 4 served"
        )

    def test_recent_does_not_raise_on_a_planted_record(self, sel_dir):
        """The drop must not become a raise: consumers call .get on every element."""
        log = _rot_log(sel_dir, max_bytes=0)
        self._plant(sel_dir, operation="forged")
        events = log.recent(limit=50)
        assert all(isinstance(e, dict) for e in events)


class TestSegmentReadsAreBoundedInAggregate:
    """The per-LINE cap left the AGGREGATE unbounded.

    ``_segment_lines`` capped each line at 1 MiB and then appended every line to a
    list, so a segment of many ordinary short lines tripped no cap and still
    materialised whole: measured, a 3.2 MB segment drove peak allocation to 22.9 MB
    (a decoded ``str`` costs several times its bytes) on both ``/api/sel/verify`` and
    ``/api/sel/events``. At the 100 MB active-file cap that is the gateway OOM.

    ``_entry_count_of`` had already hand-rolled a streaming loop for exactly this
    reason, and its docstring said so -- the residue was that the other three call
    sites still went through the accumulating helper.

    The bound is asserted by MEASUREMENT, not by shape alone: peak traced allocation
    must come in under the segment's own size. Before the fix peak ran ~7x size, so
    the threshold has margin in both directions.
    """

    PAD_LINES = 40_000

    @staticmethod
    def _peak_bytes(fn):
        """Run *fn*, returning (result, peak traced bytes)."""
        import tracemalloc

        tracemalloc.start()
        try:
            result = fn()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return result, peak

    def test_the_bounded_reader_yields_rather_than_accumulating(self, sel_dir):
        """Shape guard: a `list(...)` slipped back into the helper re-opens the hole.

        Cheap and narrow, and it pins the mechanism the two measurements below
        depend on -- a helper that materialises internally would bound nothing no
        matter how its callers consume it.
        """
        import collections.abc

        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
        with _open_segment(seg) as fh:
            produced = sel_mod._segment_lines(fh)
            assert not isinstance(produced, list), (
                "_segment_lines returned a list: every call site is O(file) again"
            )
            assert isinstance(produced, collections.abc.Iterator)
            assert len(list(produced)) == 2, "the lazy reader lost lines"

    def test_recent_does_not_materialise_a_whole_segment(self, sel_dir, caplog):
        """/api/sel/events on a large segment, with the returned events pinned too."""
        caplog.set_level(logging.CRITICAL)  # the padding is not chain-valid; not the point
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 5)
        log.flush()
        # Padding NEWER than the real events, so the retained tail is what recent()
        # actually needs -- this is the steady-state shape of a long-lived segment.
        with open(log._path, "ab") as fh:
            fh.write(b'{"event_id": "pad", "operation": "pad"}\n' * self.PAD_LINES)
        size = log._path.stat().st_size
        assert size > 500_000, f"padding too small to discriminate: {size} bytes"

        events, peak = self._peak_bytes(lambda: log.recent(limit=5))

        assert len(events) == 5, "recent() lost events -- the bound broke the read"
        assert all(e.get("operation") == "pad" for e in events), (
            "recent() no longer returns the NEWEST events first"
        )
        assert peak < size, (
            f"recent() retained {peak} bytes for a {size}-byte segment; it is "
            "materialising the whole file rather than the requested tail"
        )

    def test_verify_does_not_materialise_a_whole_segment(self, sel_dir):
        """/api/sel/verify on a large segment, with the verdict pinned to a control."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 5)
        log.flush()
        baseline = log.verify_integrity()
        # Blank padding: skipped by the accounting loop, so the VERDICT must not move,
        # while the old code still appended every one of these lines to its list.
        with open(log._path, "ab") as fh:
            fh.write(b"\n" * self.PAD_LINES * 20)
        size = log._path.stat().st_size
        assert size > 500_000, f"padding too small to discriminate: {size} bytes"

        verdict, peak = self._peak_bytes(log.verify_integrity)

        assert verdict == baseline, (
            f"verify verdict moved from {baseline} to {verdict} on blank padding"
        )
        assert peak < size, (
            f"verify_integrity() retained {peak} bytes for a {size}-byte segment; "
            "it is materialising the whole file"
        )

    def test_the_entry_count_is_bounded_too(self, sel_dir):
        """The third call site. Already lazy at the call site, now lazy underneath."""
        log = _rot_log(sel_dir, max_bytes=0)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_bytes(b'{"a": 1}\n' * self.PAD_LINES)
        size = seg.stat().st_size

        with _open_segment(seg) as fh:
            count, peak = self._peak_bytes(lambda: log._count_entries_in(fh))

        assert count == self.PAD_LINES, f"count wrong: {count}"
        assert peak < size, (
            f"_count_entries_in retained {peak} bytes for a {size}-byte segment"
        )


class TestAgePruneRequiresAnAuthenticTimestamp:
    """Proving a segment aged is not the same as trusting the proof.

    ``_prune_sealed_by_age`` decided deletion from the newest record's ``timestamp``,
    and that record was never authenticated. The existing fail-closed guard defends
    only against a stamp that is ABSENT or UNPARSEABLE -- a forged stamp is neither.
    A writer with the segment directory open (agent-writable before the
    sensitive-path floor lands) edits the oldest segment's final record to read older
    than any cutoff; the stamp parses, compares aged, and the segment is unlinked on
    the CORRECT code path. Worse, the unlink is followed by ``_mark_evicted()``, and
    verify_integrity() treats a genuine marker as licence to adopt the next segment's
    own ``prev_hash`` as its baseline -- so the chain reports `integrity: ok` over
    records that were deleted on a forged stamp. The erasure authenticates itself.

    Both failure directions are silent and err oppositely, so all three cases below
    are pinned: too weak leaves forged-age eviction reachable, too strong lets a
    segment we cannot authenticate hold retention open forever.
    """

    @staticmethod
    def _sole_record(seg) -> dict:
        return json.loads(seg.read_text(encoding="utf-8").strip())

    def test_a_forged_older_timestamp_does_not_authorise_a_prune(self, sel_dir, caplog):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            _authentic_line(log, timestamp=_iso(1), event_id="recent"), encoding="utf-8"
        )
        # Precondition: a genuine RECENT segment is kept, so a later "it survived"
        # cannot be satisfied by a log that was never prunable in the first place.
        assert log.prune(keep_days=30) == 0 and seg.exists()

        forged = self._sole_record(seg)
        forged["timestamp"] = _iso(500)  # MAC left untouched -> now stale
        seg.write_text(json.dumps(forged) + "\n", encoding="utf-8")

        # Precondition: the forged stamp is PRESENT and PARSEABLE, so the pre-existing
        # fail-closed guard cannot be what keeps the segment.
        stamp = log._newest_timestamp_of(seg)
        assert stamp is not None, "forged stamp should still be readable"
        assert log._parse_ts(stamp) is not None, "forged stamp should still parse"

        with caplog.at_level(logging.ERROR):
            removed = log.prune(keep_days=30)

        assert seg.exists(), "a forged older timestamp deleted the sealed segment"
        assert removed == 0, f"forged stamp counted entries as removed: {removed}"
        assert not (_segdir(sel_dir) / "evicted").exists(), (
            "eviction was marked for a segment deleted on a forged timestamp, which "
            "is what lets verify_integrity() report clean over the erased history"
        )
        assert any("does NOT authenticate" in r.getMessage() for r in caplog.records), (
            "the refusal must be reported -- it means tampering or a key mismatch"
        )

    def test_a_genuinely_aged_authentic_segment_is_still_pruned(self, sel_dir):
        """OPPOSITE DIRECTION: authentication must not disable retention outright."""
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            _authentic_line(log, timestamp=_iso(500), event_id="aged"), encoding="utf-8"
        )
        removed = log.prune(keep_days=30)
        assert not seg.exists(), "authentication broke legitimate age-based retention"
        assert removed == 1, f"the aged entry was not counted as removed: {removed}"

    def test_only_the_mac_distinguishes_the_forged_stamp_from_a_genuine_one(self, sel_dir):
        """The tightest control: identical record bytes apart from ``entry_hash``.

        Phase 1 keeps the segment and phase 2 prunes it while the TIMESTAMP is byte
        identical in both, so the discriminator can only be the MAC -- not some
        incidental property of a hand-edited file.
        """
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        seg = _segdir(sel_dir) / "security_events.jsonl.1"
        seg.write_text(
            _authentic_line(log, timestamp=_iso(1), event_id="x"), encoding="utf-8"
        )
        forged = self._sole_record(seg)
        forged["timestamp"] = _iso(500)
        seg.write_text(json.dumps(forged) + "\n", encoding="utf-8")
        assert log.prune(keep_days=30) == 0 and seg.exists(), "phase 1: forged kept"

        resigned = SecurityEvent(**{k: v for k, v in forged.items() if k != "entry_hash"})
        resigned.entry_hash = log._compute_hash(resigned)
        record = asdict(resigned)
        assert record["timestamp"] == forged["timestamp"], "the stamp must be unchanged"
        assert record["entry_hash"] != forged["entry_hash"], "only the MAC may differ"
        seg.write_text(json.dumps(record) + "\n", encoding="utf-8")

        assert log.prune(keep_days=30) == 1, "phase 2: re-signed record was not pruned"
        assert not seg.exists(), "re-signing the same aged stamp did not restore prunability"


class TestATruncatedRetainedSegmentIsNotErasedByEviction:
    """The erasure that authenticated itself.

    ``_drop_empty_claims`` unlinked any zero-byte sealed segment so it would not
    inflate the eviction budget. A zero-byte segment has two possible provenances --
    a crash-left number claim that never held history, and a real segment TRUNCATED
    after sealing -- and nothing on disk distinguishes them: there is no sealed
    manifest, and an empty file has no content to authenticate, so the self-HMAC that
    guards age pruning is unavailable here.

    The in-file argument for deleting it was that a truncated real segment "stays
    loud either way", because its successor's ``prev_hash`` still names the tip that
    was truncated away. That holds for a MID-CHAIN segment and fails at the head of
    the run, which is exactly where eviction deletes from: baseline relaxation covers
    the OLDEST surviving entry, so once the oldest is gone the next one's own
    ``prev_hash`` is adopted as the baseline and the chain reads clean. Measured on a
    40-entry log with an authenticated eviction marker present: retained reported
    ``total=40 valid=39``, unlinked reported ``total=39 valid=39``.

    Both directions are silent and err oppositely, so all four cases below are pinned:
    deleting loses history invisibly, while never stepping past a retained empty
    segment would defer retention forever.
    """

    def _rotated(self, sel_dir):
        """A log with several sealed segments of real history, and a prior eviction."""
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100, retention_days=0)
        _fill(log, 12)
        log.flush()
        sealed = log._list_sealed_indices()
        assert len(sealed) >= 2, f"precondition: need sealed segments, got {sealed}"
        # A prior eviction really happened, so the authenticated marker is present.
        # Without it the baseline is not relaxed and the erasure would not read clean,
        # which is the condition the finding names.
        log._mark_evicted()
        assert (_segdir(sel_dir) / "evicted").exists(), "precondition: marker present"
        return log, sealed

    def test_the_oldest_segment_truncated_after_sealing_survives_eviction(self, sel_dir):
        log, sealed = self._rotated(sel_dir)
        oldest = log._segment_path(min(sealed))
        assert oldest.stat().st_size > 0, "precondition: it holds real history"
        oldest.write_bytes(b"")  # sealed, then truncated

        with log._lock:
            log._evict_over_budget()

        assert oldest.exists(), (
            "a segment truncated after sealing was unlinked by the budget sweep, "
            "erasing the only evidence that its history is gone"
        )

    def test_verification_reports_the_truncated_segment_rather_than_reading_clean(
        self, sel_dir
    ):
        """The other half of the remedy: the evidence has to be ACTED on, not just kept."""
        log, sealed = self._rotated(sel_dir)
        before_total, before_valid = log.verify_integrity()
        assert before_total == before_valid, (
            f"precondition: the fixture must start clean, got {before_valid}/{before_total}"
        )

        log._segment_path(min(sealed)).write_bytes(b"")
        with log._lock:
            log._evict_over_budget()

        total, valid = log.verify_integrity()
        assert valid < total, (
            f"verification read clean ({valid}/{total}) over a truncated segment -- "
            "the erasure authenticated itself"
        )

    def test_an_empty_segment_still_does_not_consume_the_eviction_budget(self, sel_dir):
        """OPPOSITE DIRECTION: keeping the file must not re-break the budget.

        The exclusion is the property the sweep exists for. If keeping the file also
        put its number back into the accounting, every roll would resume evicting one
        extra VALID segment -- the original defect this method was written to fix.
        """
        log = _rot_log(sel_dir, max_bytes=400, backup_count=3, retention_days=0)
        _fill(log, 12)
        log.flush()
        sealed = log._list_sealed_indices()
        assert len(sealed) == 3, f"precondition: exactly at budget, got {sealed}"
        nxt = sealed[-1] + 1
        fd = os.open(log._segment_path(nxt), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)

        with log._lock:
            log._evict_over_budget()

        survivors = log._list_sealed_indices()
        assert set(sealed) <= set(survivors), (
            f"an empty segment consumed budget and evicted real history: "
            f"{sorted(set(sealed) - set(survivors))} went, budget was {sealed}"
        )

    def test_retention_still_runs_past_a_retained_empty_segment(self, sel_dir):
        """OPPOSITE DIRECTION: keeping the file must not defer retention forever.

        An empty segment is chain-transparent -- it holds no entries, so nothing
        chains through it -- but age pruning stops at the first segment it cannot
        prove aged, and a zero-byte segment can never be proved aged. At the oldest
        position that would block retention permanently, trading a silent data-loss
        path for a silent denial of the feature. It must be stepped past, and NOT
        deleted.
        """
        log = _rot_log(sel_dir, max_bytes=0, retention_days=30)
        empty = _segdir(sel_dir) / "security_events.jsonl.1"
        empty.write_bytes(b"")
        for idx in (2, 3):
            (_segdir(sel_dir) / f"security_events.jsonl.{idx}").write_text(
                _authentic_line(log, timestamp=_iso(400), event_id=f"aged-{idx}"),
                encoding="utf-8",
            )

        with log._lock:
            removed = log._prune_sealed_by_age(30)

        assert removed == 2, f"retention was blocked by the empty segment: {removed}"
        assert log._list_sealed_indices() == [1], (
            f"expected only the empty evidence segment to survive, got "
            f"{log._list_sealed_indices()}"
        )
        assert empty.exists(), "retention deleted the evidence it was told to step past"


class TestZeroBackupDiscardIsAttributableNotSpurious:
    """A backup_count=0 roll must not make a legitimate concurrent append read as
    tampering -- and must not stop distinguishing it from head truncation.

    `_discard_leased` TRUNCATES the active file rather than unlinking it, on purpose:
    after an unlink a rival process's O_APPEND write goes to an orphaned inode and
    vanishes with no error (silent audit loss), while after a truncate the same write
    lands at the new EOF and survives. Its `prev_hash` then names the tip the discard
    destroyed, and verify used to report that as `SEL chain break at entry 1`
    (measured total=1 valid=0) -- the identical verdict a head truncation produces.

    The fix attributes that ONE hash instead of changing what gets deleted. Rotation
    stays enabled at backup_count=0 (so the active file stays bounded) and the sticky
    `evicted` marker is NOT written (so the wholesale genesis relaxation, and the
    head-truncation refusal it gates, are untouched).
    """

    @staticmethod
    def _rival_record(log, prev_hash, event_id):
        """A record linked to *prev_hash*, self-HMAC'd with the install's key."""
        rec = {
            "timestamp": "2026-08-20T00:00:00+00:00",
            "event_id": event_id,
            "prev_hash": prev_hash,
        }
        payload = json.dumps(rec, sort_keys=True).encode()
        rec["entry_hash"] = hmac.new(log._hmac_key, payload, hashlib.sha256).hexdigest()
        return rec

    @classmethod
    def _discard_with_rival(cls, sel_dir, *, extra_rivals=0):
        """Model the race: a rival holds an O_APPEND fd across a zero-backup discard.

        Returns (log, discarded_tip, [rival records written]).
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 40)
        log.flush()
        discarded_tip = log._last_hash
        assert discarded_tip, "precondition: a non-empty tip must exist to be discarded"
        # Opened BEFORE the discard, exactly as a rival writer in another process
        # would already hold it.
        fd = os.open(log._path, os.O_WRONLY | os.O_APPEND)
        try:
            with log._lock:
                with log._seal_lease():
                    log._discard_leased()
            assert log._path.stat().st_size == 0, "precondition: the discard truncated"
            written = []
            prev = discarded_tip
            for i in range(1 + extra_rivals):
                rec = cls._rival_record(log, prev, f"rival-{i}")
                os.write(fd, (json.dumps(rec) + "\n").encode())
                written.append(rec)
                prev = rec["entry_hash"]
        finally:
            os.close(fd)
        return log, discarded_tip, written

    # ---- the discriminator: the two verdicts must NOT collapse into one ----

    def test_a_concurrent_append_after_a_zero_backup_discard_is_attributed(
        self, sel_dir, caplog
    ):
        log, tip, _ = self._discard_with_rival(sel_dir)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert (total, valid) == (1, 1), f"attribution failed: total={total} valid={valid}"
        assert any("roll discarded" in m for m in msgs), msgs
        assert not any("chain break at entry" in m for m in msgs), msgs
        # The sticky marker must NOT have been used to buy this.
        assert not (_segdir(sel_dir) / "evicted").exists()

    def test_head_truncation_on_a_never_evicted_log_reports_the_other_verdict(
        self, sel_dir, caplog
    ):
        """The OTHER side of the discriminator, asserted as a DIFFERENT verdict."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 6)
        log.flush()
        lines = log._path.read_text(encoding="utf-8").strip().splitlines()
        log._path.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert valid < total, "genesis anchor was not enforced"
        assert any("chain break at entry" in m for m in msgs), msgs
        assert not any("roll discarded" in m for m in msgs), (
            "head truncation was wrongly attributed to a discard"
        )

    def test_truncating_after_a_discard_to_another_point_still_breaks(self, sel_dir, caplog):
        """The attribution is scoped to ONE hash, not to 'a discard happened'."""
        log, tip, written = self._discard_with_rival(sel_dir, extra_rivals=1)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        assert (total, valid) == (2, 2), f"precondition failed: {total} {valid}"
        # Drop the entry that legitimately carries the discarded tip, leaving the
        # NEXT one as entry 1. Its prev_hash is a different value, so nothing
        # authenticates it.
        body = log._path.read_text(encoding="utf-8").strip().splitlines()
        log._path.write_text(body[1] + "\n", encoding="utf-8")
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert valid < total, "a non-attributable truncation point read clean"
        assert any("chain break at entry" in m for m in msgs), msgs

    def test_a_forged_discarded_tip_record_cannot_attribute_a_break(self, sel_dir, caplog):
        """Unauthenticated contents must not buy the single-hash relaxation."""
        log, tip, _ = self._discard_with_rival(sel_dir)
        # Right shape, wrong MAC -- the cheapest forgery.
        (_segdir(sel_dir) / "discarded-tip").write_text(f"{'0' * 64} {tip}", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        assert valid < total, "a forged discarded-tip record suppressed the break"
        assert log._authentic_discarded_tip() == ""

    def test_a_discard_that_destroyed_nothing_records_no_tip(self, sel_dir):
        """The guard, in the other direction: no tip means nothing to attribute."""
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        log._last_hash = ""
        with log._lock:
            with log._seal_lease():
                log._discard_leased()
        assert not (_segdir(sel_dir) / "discarded-tip").exists()
        assert log._authentic_discarded_tip() == ""

    # ---- the seam: the discard leaves TWO legitimate anchors, not one ----

    @classmethod
    def _discard_with_rival_and_owner(cls, sel_dir, *, owner_first):
        """The race as it actually runs: the OWNER keeps logging after the roll.

        `_discard_with_rival` leaves the rival's record as the only entry, which is
        the one shape the entry-1 adoption already covered. In real operation the
        process that rolled the log carries on appending -- re-anchored to genesis --
        so the active file ends up holding a genesis-anchored chain AND a chain
        linked to the destroyed tip, in whichever order the two writers landed.
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 40)
        log.flush()
        tip = log._last_hash
        assert tip, "precondition: a non-empty tip must exist to be discarded"
        fd = os.open(log._path, os.O_WRONLY | os.O_APPEND)
        try:
            with log._lock:
                with log._seal_lease():
                    log._discard_leased()
            assert log._path.stat().st_size == 0, "precondition: the discard truncated"
            assert log._last_hash == "", "precondition: the owner re-anchored to genesis"

            def owner_append():
                _fill(log, 1, start=900)
                log.flush()

            def rival_append():
                rec = cls._rival_record(log, tip, "rival-seam")
                os.write(fd, (json.dumps(rec) + "\n").encode())

            if owner_first:
                owner_append()
                rival_append()
            else:
                rival_append()
                owner_append()
        finally:
            os.close(fd)
        return log, tip

    def test_b_owner_then_rival_across_the_seam_is_attributed(self, sel_dir, caplog):
        """The rival lands at entry 2, where the entry-1 adoption cannot reach it.

        Measured before the fix: ``total=2 valid=1`` with "SEL chain break at entry 2"
        and NO attribution message at all.
        """
        log, _ = self._discard_with_rival_and_owner(sel_dir, owner_first=True)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert (total, valid) == (2, 2), f"seam not attributed: total={total} valid={valid}"
        assert not any("chain break at entry" in m for m in msgs), msgs
        assert any("re-anchors at the seam" in m for m in msgs), msgs
        # Bought with the single-hash record, NOT the wholesale sticky marker.
        assert not (_segdir(sel_dir) / "evicted").exists()

    def test_b_rival_then_owner_across_the_seam_is_attributed(self, sel_dir, caplog):
        """The owner's genesis anchor lands at entry 2, after the rival took entry 1.

        Measured before the fix: entry 1 was attributed and the walk then reported
        "SEL chain break at entry 2" on the owner's own record (``total=2 valid=1``).
        """
        log, _ = self._discard_with_rival_and_owner(sel_dir, owner_first=False)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert (total, valid) == (2, 2), f"seam not attributed: total={total} valid={valid}"
        assert not any("chain break at entry" in m for m in msgs), msgs
        assert not (_segdir(sel_dir) / "evicted").exists()

    # ---- negative controls for the seam, one per direction it could go wrong ----

    def test_head_truncation_on_a_log_that_did_discard_still_breaks(self, sel_dir, caplog):
        """The control the seam relaxation most plausibly masks.

        An authenticated discard record EXISTS here, so the gate is open -- and a head
        truncation must still be reported, because its surviving first entry claims a
        predecessor that is neither anchor the discard created.
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 40)
        log.flush()
        with log._lock:
            with log._seal_lease():
                log._discard_leased()
        assert log._authentic_discarded_tip(), "precondition: the gate must be OPEN"
        # Rotation off from here, so the entries below stay in the ACTIVE file and are
        # actually present to be head-truncated (at max_bytes=300 they roll away).
        log._max_bytes = 10_000_000
        _fill(log, 6, start=500)
        log.flush()
        lines = log._path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 6, f"precondition: expected 6 owner entries, got {len(lines)}"
        log._path.write_text("\n".join(lines[2:]) + "\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert valid < total, "head truncation read clean on a log that had discarded"
        assert any("chain break at entry" in m for m in msgs), msgs
        assert not any("re-anchors at the seam" in m for m in msgs), (
            "a head truncation was wrongly attributed to the discard seam"
        )

    def test_a_mid_file_genesis_anchor_without_a_discard_record_still_breaks(
        self, sel_dir, caplog
    ):
        """The authenticated-record GATE is load-bearing, isolated from the spent-once
        guard.

        On a log whose baseline anchored at genesis, `""` is already in the spent set,
        so that guard alone refuses a mid-file genesis anchor and the gate is never
        consulted. After a REAL eviction the baseline anchors on the oldest survivor's
        claimed prev_hash instead, leaving `""` unspent -- so only the gate stands
        between a spliced genesis-anchored record and a masked break. This log evicted
        via the seal path (`backup_count=1`), so no discard record exists.
        """
        log = _rot_log(sel_dir, max_bytes=300, backup_count=1)
        _fill(log, 80)
        log.flush()
        assert (_segdir(sel_dir) / "evicted").exists(), "precondition: real eviction"
        assert log._authentic_discarded_tip() == "", "precondition: gate must be CLOSED"
        spliced = self._rival_record(log, "", "genesis-splice")
        with log._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(spliced) + "\n")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert valid < total, "a mid-file genesis anchor was adopted with no discard record"
        assert any("chain break at entry" in m for m in msgs), msgs
        assert not any("re-anchors at the seam" in m for m in msgs), msgs

    def test_the_seam_is_spent_once_so_a_third_anchor_still_breaks(self, sel_dir, caplog):
        """One discard yields at most two anchors, so a repeat of one still breaks."""
        log, tip = self._discard_with_rival_and_owner(sel_dir, owner_first=True)
        # A SECOND record linked to the destroyed tip: the tip anchor is already spent
        # by the rival at entry 2, so this one has no seam left to claim.
        extra = self._rival_record(log, tip, "rival-second")
        with log._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(extra) + "\n")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert valid < total, f"a third anchor was adopted: total={total} valid={valid}"
        assert any("chain break at entry" in m for m in msgs), msgs

    # ---- negative control, DIRECTION (i): the silent-loss direction stays fixed ----

    def test_a_rival_append_after_the_discard_still_lands_in_the_live_file(self, sel_dir):
        """FAILS if the truncate is swapped back to an unlink (orphaned inode)."""
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 40)
        log.flush()
        fd = os.open(log._path, os.O_WRONLY | os.O_APPEND)
        try:
            with log._lock:
                with log._seal_lease():
                    log._discard_leased()
            os.write(fd, b'{"probe": "survives"}\n')
        finally:
            os.close(fd)
        assert log._path.exists(), "the discard must not remove the active file"
        assert "survives" in log._path.read_text(encoding="utf-8"), (
            "the rival's bytes went to an orphaned inode -- silent audit loss"
        )

    # ---- negative control, DIRECTION (iii): rotation stays ENABLED at zero ----

    def test_rotation_is_still_enabled_at_backup_count_zero(self, sel_dir):
        """FAILS if backup_count<=0 is treated as 'rotation disabled'."""
        log = _rot_log(sel_dir, max_bytes=300, backup_count=0)
        _fill(log, 60)
        log.flush()
        size = log._path.stat().st_size
        assert size <= 300 * 4, (
            f"the active file is no longer bounded by max_bytes: {size} bytes"
        )
        assert log._list_sealed_indices() == []


class TestVerifyWalkAuthenticationIsGuarded:
    """B1: the verify walk's MAC compare must not misclassify a hostile hash.

    `_segment_lines` decodes with ``errors="replace"`` (PR-own), so an invalid byte
    in a segment becomes U+FFFD, survives `json.loads` inside a JSON string, and
    reaches the compare as a non-ASCII ``str`` -- on which `hmac.compare_digest`
    raises TypeError (measured). That was caught by the walk's `except Exception`, so
    the observable was a MISCLASSIFICATION: the entry was logged as a parse error
    rather than an HMAC mismatch. The counts were already right; the reason was not.
    """

    def test_a_non_ascii_entry_hash_reports_hmac_mismatch_not_parse_error(
        self, sel_dir, caplog
    ):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 3)
        log.flush()
        lines = log._path.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        rec["entry_hash"] = "\u00e1" + rec["entry_hash"][1:]
        log._path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.sel"):
            total, valid = log.verify_integrity()
        msgs = [r.getMessage() for r in caplog.records]
        assert (total, valid) == (1, 0), f"total={total} valid={valid}"
        assert any("HMAC mismatch" in m for m in msgs), msgs
        assert not any("parse error" in m for m in msgs), (
            "a corrupted hash is still misreported as a parse error"
        )


class TestUnparseableLinesDoNotStopRetentionOrEraseAudit:
    """B2/B3: widen the JSONDecodeError-only handlers, WITHOUT making a line
    self-erasing.

    `json.JSONDecodeError` IS a `ValueError` (measured True); `RecursionError` is NOT
    (measured False). So a nesting bomb and an over-4300-digit integer both escaped
    `prune()`'s handler, left the `with`, and hit `except BaseException` -- which
    unlinks the temp file and RE-RAISES. No audit data was lost, but the age prune
    aborted, so `retention_days` stopped being enforced while that one line sat there.

    The new arm KEEPS such a line rather than adding those types to the DROP tuple:
    widening the drop would let an attacker make an audit line self-erasing by making
    it unparseable in a newer way.
    """

    BOMB = "[" * 200000 + "]" * 200000
    BIGINT = "9" * 10000

    @staticmethod
    def _log_with(sel_dir, crafted):
        log = _rot_log(sel_dir, max_bytes=0, retention_days=1)
        _fill(log, 2, ts=_iso(400))  # aged -> removable
        _fill(log, 1, ts=_iso(0), start=90)  # fresh -> retained
        log.flush()
        with log._path.open("a", encoding="utf-8") as fh:
            fh.write(crafted + "\n")
        return log

    @pytest.mark.parametrize("name", ["bomb", "bigint"])
    def test_prune_completes_and_keeps_the_unparseable_line(self, sel_dir, name):
        crafted = self.BOMB if name == "bomb" else self.BIGINT
        log = self._log_with(sel_dir, crafted)
        removed = log.prune()
        body = log._path.read_text(encoding="utf-8")
        assert removed == 2, f"the age prune did not complete: removed={removed}"
        assert crafted in body, "the unparseable line was silently erased"
        assert "rot-000090" in body, "the fresh entry was dropped"

    def test_prune_still_drops_an_ordinary_malformed_line(self, sel_dir):
        """CONTROL: the pre-existing JSONDecodeError DROP semantics are untouched."""
        log = self._log_with(sel_dir, "not-json")
        removed = log.prune()
        body = log._path.read_text(encoding="utf-8")
        assert removed == 3, f"expected 2 aged + 1 malformed dropped, got {removed}"
        assert "not-json" not in body

    @pytest.mark.parametrize("name", ["bomb", "bigint"])
    def test_newest_record_of_treats_an_unparseable_tail_as_unknown_age(self, sel_dir, name):
        """B3: same fail-closed direction the arm already took -- None means KEEP."""
        crafted = self.BOMB if name == "bomb" else self.BIGINT
        log = _rot_log(sel_dir, max_bytes=400, backup_count=100)
        _fill(log, 8)
        log.flush()
        seg = log._segment_path(log._list_sealed_indices()[0])
        with seg.open("a", encoding="utf-8") as fh:
            fh.write(crafted + "\n")
        assert log._newest_record_of(seg) is None, (
            "an unparseable newest record must read as unknown age, not raise"
        )


class TestStrandedReappendDoesNotGlueOntoATornTail:
    """A re-append after a mid-append roll must not destroy the record's readability.

    `_reappend_stranded` opens whatever is now the active file -- a file this call
    never wrote -- so its tail can be a torn fragment from another process crashing
    mid-append. Writing straight into that with O_APPEND glues the first re-written
    record onto the fragment, forming ONE unparseable line: the `critical=True` audit
    returns normally while its evidence cannot be read back at all.

    This is a DIFFERENT failure from the chain break `_reappend_stranded` documents
    and accepts. A reported break leaves every record readable; this destroys
    readability, so the caller's fail-closed branch never runs.

    The primary append path already inserts the same newline boundary via
    `_ends_without_newline()`; this pins the sibling.
    """

    @staticmethod
    def _torn(log):
        """Leave the active file ending mid-record, with NO trailing newline."""
        with log._path.open("a", encoding="utf-8") as fh:
            fh.write('{"timestamp": "2026-08-20T00:00:00+00:00", "event_id": "torn')

    @staticmethod
    def _critical_line():
        return (
            json.dumps(
                {
                    "timestamp": "2026-08-20T00:00:01+00:00",
                    "event_id": "CRITICAL-AUDIT",
                    "prev_hash": "",
                    "entry_hash": "x",
                }
            )
            + "\n"
        )

    @staticmethod
    def _parseable_ids(path):
        ids, unparseable = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (ValueError, RecursionError):
                unparseable += 1
                continue
            if isinstance(rec, dict) and rec.get("event_id"):
                ids.append(rec["event_id"])
        return ids, unparseable

    def test_a_reappended_record_is_readable_past_a_torn_tail(self, sel_dir):
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 1)
        log.flush()
        self._torn(log)
        assert log._ends_without_newline(), "precondition: the tail must lack a newline"

        log._reappend_stranded([self._critical_line()])

        ids, unparseable = self._parseable_ids(log._path)
        # POSITIVE observable: the record can actually be read back.
        assert "CRITICAL-AUDIT" in ids, (
            f"the re-appended critical record is unreadable; parseable ids={ids}"
        )
        # The torn fragment is PRESERVED as its own skipped line, not truncated away.
        assert unparseable == 1, f"expected the fragment to survive as one line, got {unparseable}"

    def test_a_reappend_onto_a_clean_tail_adds_no_blank_line(self, sel_dir):
        """CONTROL: the guard must be conditional, not an unconditional newline."""
        log = _rot_log(sel_dir, max_bytes=0)
        _fill(log, 2)
        log.flush()
        assert not log._ends_without_newline(), "precondition: tail already newline-terminated"
        before = log._path.read_text(encoding="utf-8")

        log._reappend_stranded([self._critical_line()])

        after = log._path.read_text(encoding="utf-8")
        assert "\n\n" not in after, "an unconditional newline inserted a blank line"
        assert after.startswith(before), "the existing records were disturbed"
        ids, unparseable = self._parseable_ids(log._path)
        assert "CRITICAL-AUDIT" in ids
        assert unparseable == 0, f"a clean tail produced {unparseable} unparseable line(s)"


class TestFailedMarkerCleanupCannotMaskHeadTruncation:
    """The evicted marker must be cleared BEFORE anything destructive.

    Clearing it last means an unlink failure leaves an AUTHENTIC marker standing over
    a chain that was just re-anchored to genesis, so the relaxation is permanent and
    a later head truncation verifies CLEAN. Measured before the fix: truncating 2 of 6
    fresh entries gave ``total=4 valid=4`` with nothing logged, against
    ``total=4 valid=3`` and "SEL chain break at entry 1" once the marker was cleared.

    Clearing first turns both failure modes loud: a failed clear aborts the discard
    before any deletion, and a failed truncate leaves verify enforcing genesis.
    """

    @staticmethod
    def _evicted_log(sel_dir):
        """A log carrying an AUTHENTIC eviction marker from a real eviction."""
        log = _rot_log(sel_dir, max_bytes=300, backup_count=2)
        _fill(log, 40)
        log.flush()
        assert log._has_evicted(), "precondition: an authentic marker must exist"
        return log

    @staticmethod
    def _discard_with_failing_clear(log):
        """Force the marker clear to fail during a backup_count=0 discard."""
        log._backup_count = 0
        marker = log._marker_path()
        real_unlink = Path.unlink

        def flaky(self, *a, **kw):
            if self == marker:
                raise OSError(13, "simulated marker cleanup failure")
            return real_unlink(self, *a, **kw)

        raised = False
        with patch.object(Path, "unlink", flaky):
            try:
                with log._lock:
                    with log._seal_lease():
                        log._discard_leased()
            except OSError:
                raised = True
        return raised

    def test_a_failed_marker_clear_aborts_before_destroying_anything(self, sel_dir):
        log = self._evicted_log(sel_dir)
        sealed_before = log._list_sealed_indices()
        tip_before = log._last_hash
        size_before = log._path.stat().st_size
        assert sealed_before and tip_before and size_before > 0, "precondition"

        raised = self._discard_with_failing_clear(log)

        assert raised, "a marker clear that cannot complete must abort the discard"
        # Nothing destructive ran: the abort happens BEFORE the truncate.
        assert log._path.stat().st_size == size_before, "the active file was truncated anyway"
        assert log._last_hash == tip_before, "the chain tip was re-anchored anyway"

    def test_a_completed_reanchor_never_coexists_with_an_authentic_marker(self, sel_dir):
        """The precise invariant, and the one the lane's exposure violates.

        A marker is only ILLEGITIMATE once the chain has been re-anchored to genesis:
        on a host that merely evicted, an authentic marker relaxing the anchor is the
        documented design (see the `eviction_plausible` gate), and head truncation is
        masked there by intent. So the forbidden state is the CONJUNCTION --
        ``_last_hash == ""`` (re-anchor done) AND ``_has_evicted()`` (marker still
        authentic). Clearing last produces exactly that pair when the unlink fails;
        measured pre-fix: ``marker_authentic=True tip='' size=0``.

        Either outcome is acceptable on its own: abort with the tip intact, or
        complete with the marker gone. Only the pair is a masked truncation.
        """
        log = self._evicted_log(sel_dir)
        tip_before = log._last_hash

        self._discard_with_failing_clear(log)

        reanchored = log._last_hash == ""
        marker_authentic = log._has_evicted()
        assert not (reanchored and marker_authentic), (
            "FORBIDDEN STATE: the chain was re-anchored to genesis while an authentic "
            "eviction marker survived, so verify will relax the anchor permanently and "
            "a later head truncation reads clean"
        )
        # And the abort must be a real abort, not a partial one.
        if not reanchored:
            assert log._last_hash == tip_before, "tip moved despite not re-anchoring"

    def test_a_successful_discard_leaves_no_authentic_marker(self, sel_dir):
        """CONTROL: the ordinary path must still end with the marker gone."""
        log = self._evicted_log(sel_dir)
        log._backup_count = 0
        with log._lock:
            with log._seal_lease():
                log._discard_leased()
        assert log._last_hash == "", "the discard did not re-anchor"
        assert not log._has_evicted(), "the marker survived a successful discard"
