"""Tests for SEL.prune() streaming rewrite + concurrency safety.

Covers:
  - Streaming correctness: old+new+garbage lines produce correct survivors/count
  - Atomic replacement: no partial state observable
  - Concurrency: appends from another thread during prune are not lost
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiro_crew.sel import SecurityEvent, SecurityEventLog


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the SEL singleton between tests."""
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False
    yield
    SecurityEventLog._instance = None
    SecurityEventLog._initialized = False


def _make_entry(ts: datetime, tool: str = "test_tool") -> str:
    """Create a valid SEL JSONL entry with given timestamp."""
    return json.dumps({
        "timestamp": ts.isoformat(),
        "event": "tool_call",
        "tool": tool,
        "session_key": "test:1",
        "outcome": "ok",
        "hmac": "deadbeef",
    })


class TestPruneStreamingCorrectness:
    """Verify prune streaming produces identical results to the old in-memory approach."""

    def test_prune_old_new_garbage(self, tmp_path: Path) -> None:
        """Mix of old, new, and garbage lines — correct survivors and count."""
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(days=100)
        new_ts = now - timedelta(hours=1)

        lines = [
            _make_entry(old_ts, "old1"),
            _make_entry(old_ts, "old2"),
            _make_entry(new_ts, "new1"),
            "not valid json at all",
            _make_entry(new_ts, "new2"),
            "",  # blank line
            "{malformed",
            _make_entry(new_ts, "new3"),
        ]
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        removed = log.prune(keep_days=30)

        # 2 old + 2 garbage = 4 removed
        assert removed == 4

        # Survivors: new1, new2, new3
        surviving = sel_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(surviving) == 3
        tools = [json.loads(line)["tool"] for line in surviving]
        assert tools == ["new1", "new2", "new3"]

    def test_prune_nothing_old(self, tmp_path: Path) -> None:
        """All entries are recent — file unchanged, 0 removed."""
        now = datetime.now(tz=timezone.utc)
        new_ts = now - timedelta(hours=1)

        lines = [_make_entry(new_ts, f"t{i}") for i in range(5)]
        sel_file = tmp_path / "security_events.jsonl"
        original_content = "\n".join(lines) + "\n"
        sel_file.write_text(original_content, encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        removed = log.prune(keep_days=30)

        assert removed == 0
        # File content unchanged (no temp file replace happened)
        assert sel_file.read_text(encoding="utf-8") == original_content

    def test_prune_all_old(self, tmp_path: Path) -> None:
        """All entries expired — file becomes empty."""
        old_ts = datetime.now(tz=timezone.utc) - timedelta(days=100)

        lines = [_make_entry(old_ts, f"t{i}") for i in range(3)]
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        removed = log.prune(keep_days=30)

        assert removed == 3
        # File should be empty (or contain just a newline)
        content = sel_file.read_text(encoding="utf-8").strip()
        assert content == ""

    def test_prune_nonexistent_file(self, tmp_path: Path) -> None:
        """Prune on missing file returns 0."""
        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        assert log.prune(keep_days=30) == 0


class TestPruneAtomicity:
    """Verify prune uses atomic temp-file replacement."""

    def test_no_partial_state(self, tmp_path: Path) -> None:
        """After prune, file contains only full valid lines — no partial writes."""
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(days=100)
        new_ts = now - timedelta(hours=1)

        lines = [_make_entry(old_ts)] * 50 + [_make_entry(new_ts, f"s{i}") for i in range(50)]
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        removed = log.prune(keep_days=30)

        assert removed == 50
        # Every surviving line is valid JSON
        for line in sel_file.read_text(encoding="utf-8").strip().splitlines():
            data = json.loads(line)
            assert "timestamp" in data

    def test_temp_file_cleaned_on_no_removal(self, tmp_path: Path) -> None:
        """When nothing to prune, no temp file left behind."""
        new_ts = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text(_make_entry(new_ts) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=True)
        log.prune(keep_days=30)

        # No .sel_prune_*.tmp files left
        temps = list(tmp_path.glob(".sel_prune_*"))
        assert temps == []


class TestPruneConcurrency:
    """Verify appends from another thread during prune are not lost."""

    def test_concurrent_appends_not_lost(self, tmp_path: Path) -> None:
        """Appends racing with prune are preserved and keepers survive.

        Runs prune and appends in PARALLEL threads (production sync=False
        writer). Prune calls flush() first to drain the queue, then takes the
        writer lock for the atomic replace; appends landing mid-prune are
        queued and written after the replace. Asserts both structural validity
        and data completeness: the 5 keepers survive and every append lands.
        """
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(days=100)
        new_ts = now - timedelta(hours=1)

        # Seed with old entries that will be pruned + some keepers
        lines = [_make_entry(old_ts, f"old{i}") for i in range(20)]
        lines += [_make_entry(new_ts, f"existing{i}") for i in range(5)]
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=False)

        append_count = 10
        prune_result: list[int] = []

        def do_prune() -> None:
            prune_result.append(log.prune(keep_days=30))

        def do_appends() -> None:
            for i in range(append_count):
                log.log(SecurityEvent(
                    event_id=f"concurrent_{i}",
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    event_type="tool_invocation",
                    caller_identity="test:concurrent",
                    agent="test",
                    source="test",
                    operation=f"concurrent_{i}",
                    outcome="ok",
                ))
                time.sleep(0.001)

        t_prune = threading.Thread(target=do_prune)
        t_append = threading.Thread(target=do_appends)
        t_prune.start()
        t_append.start()
        t_prune.join(timeout=10)
        t_append.join(timeout=10)
        assert not t_prune.is_alive() and not t_append.is_alive(), "threads hung"
        assert prune_result == [20], f"prune removed {prune_result}, expected [20]"

        # Flush to ensure all queued events are written
        log.flush()

        # Read back the file — every line valid JSON, keepers + appends all present
        final_lines = sel_file.read_text(encoding="utf-8").strip().splitlines()
        tools = []
        for line in final_lines:
            data = json.loads(line)  # structural validity: raises on corruption
            tools.append(data.get("operation", "") or data.get("tool", ""))

        existing_count = sum(1 for t in tools if t.startswith("existing"))
        concurrent_count = sum(1 for t in tools if t.startswith("concurrent_"))

        assert existing_count == 5, f"Expected 5 existing entries, got {existing_count}"
        assert concurrent_count == append_count, (
            f"Expected {append_count} concurrent appends, got {concurrent_count}"
        )

    def test_appends_during_prune_not_corrupted(self, tmp_path: Path) -> None:
        """Appends interleaved with prune produce valid JSONL — no corruption."""
        now = datetime.now(tz=timezone.utc)
        old_ts = now - timedelta(days=100)
        new_ts = now - timedelta(hours=1)

        lines = [_make_entry(old_ts, f"old{i}") for i in range(10)]
        lines += [_make_entry(new_ts, f"keep{i}") for i in range(5)]
        sel_file = tmp_path / "security_events.jsonl"
        sel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        log = SecurityEventLog(base_dir=tmp_path, sync=False)

        # Run prune + append in separate threads to stress interleaving
        errors: list[str] = []

        def do_prune():
            try:
                log.prune(keep_days=30)
            except Exception as e:
                errors.append(f"prune: {e}")

        def do_appends():
            for i in range(20):
                try:
                    log.log(SecurityEvent(
                        event_id=f"stress_{i}",
                        timestamp=datetime.now(tz=timezone.utc).isoformat(),
                        event_type="tool_invocation",
                        caller_identity="test:stress",
                        agent="test",
                        source="test",
                        operation=f"stress_{i}",
                        outcome="ok",
                    ))
                except Exception as e:
                    errors.append(f"append {i}: {e}")
                time.sleep(0.001)

        t_prune = threading.Thread(target=do_prune)
        t_append = threading.Thread(target=do_appends)
        t_prune.start()
        t_append.start()
        t_prune.join(timeout=10)
        t_append.join(timeout=10)
        log.flush()

        assert errors == [], f"Errors during concurrent ops: {errors}"

        # Verify file is valid JSONL — every non-empty line parses
        for line in sel_file.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                json.loads(line)  # will raise on corruption
