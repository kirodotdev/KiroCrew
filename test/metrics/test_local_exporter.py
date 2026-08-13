"""Tests for kiro_crew.metrics.local_exporter — JsonlMetricExporter."""

import json
from unittest.mock import MagicMock

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

from kiro_crew import platform_compat
from kiro_crew.metrics import local_exporter
from kiro_crew.metrics.local_exporter import JsonlMetricExporter


def _provider(tmp_path):
    # Very long interval so the daemon thread never fires mid-test; we flush
    # explicitly via provider.force_flush().
    reader = PeriodicExportingMetricReader(
        JsonlMetricExporter(tmp_path), export_interval_millis=3_600_000.0
    )
    return MeterProvider(metric_readers=[reader])


def test_export_writes_valid_jsonl(tmp_path):
    provider = _provider(tmp_path)
    try:
        provider.get_meter("test").create_counter("kirocrew.test.count").add(
            5, attributes={"ok": "yes"}
        )
        provider.force_flush()

        files = list(tmp_path.glob("metrics-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert lines, "expected at least one JSON line"
        parsed = json.loads(lines[0])  # each line is one valid JSON object
        assert "resource_metrics" in parsed
        assert "kirocrew.test.count" in lines[0]
    finally:
        provider.shutdown()


def test_export_appends_one_line_per_flush(tmp_path):
    provider = _provider(tmp_path)
    try:
        counter = provider.get_meter("test").create_counter("kirocrew.test.count")
        counter.add(1)
        provider.force_flush()
        counter.add(1)
        provider.force_flush()

        files = list(tmp_path.glob("metrics-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
    finally:
        provider.shutdown()


# ── Retention (rec #14: bounded local retention) ──────────────────────────


def _shard(tmp_path, name: str, size: int, age_days: float):
    """Create a metrics-*.jsonl shard of *size* bytes aged *age_days* days."""
    import os
    import time

    p = tmp_path / name
    p.write_bytes(b"x" * size)
    mtime = time.time() - age_days * 86400
    os.utime(p, (mtime, mtime))
    return p


def test_prune_warns_and_defers_once_without_persistent_state(tmp_path, caplog):
    exp = JsonlMetricExporter(tmp_path, retention_days=7)
    old = _shard(tmp_path, "metrics-2000-01-01-1.jsonl", 10, age_days=30)
    fresh = _shard(tmp_path, "metrics-2000-01-02-2.jsonl", 10, age_days=1)

    with caplog.at_level("WARNING"):
        exp._prune()
        assert old.exists(), "the first destructive sweep must be deferred"
        exp._prune()

    assert not old.exists(), "the next sweep must enforce the age cap"
    assert fresh.exists(), "in-window shard must survive"
    notices = [
        record.message
        for record in caplog.records
        if "local metrics retention will permanently delete" in record.message
    ]
    assert len(notices) == 1
    assert old.name not in notices[0], "the warning must not expose shard names"
    assert str(tmp_path) not in notices[0], "the warning must not expose paths"
    assert not (tmp_path / ".retention-grace-v1").exists()


def test_prune_protects_live_canonical_writer(tmp_path, monkeypatch):
    exp = JsonlMetricExporter(tmp_path, retention_days=7)
    day = local_exporter.datetime.now(
        local_exporter.timezone.utc
    ).strftime("%Y-%m-%d")
    live = _shard(tmp_path, f"metrics-{day}-123.jsonl", 10, age_days=30)
    rotated = _shard(
        tmp_path, f"metrics-{day}-123-1.jsonl", 10, age_days=30
    )
    monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == 123)

    exp._prune()
    exp._prune()

    assert live.exists(), "a live PID's canonical writer must be protected"
    assert not rotated.exists(), "closed rotated shards remain eligible"


def test_prune_enforces_total_size_cap_oldest_first(tmp_path):
    # 1 MB cap; three ~0.5 MB shards => must drop oldest until under budget.
    exp = JsonlMetricExporter(tmp_path, max_total_mb=1)
    half_mb = 512 * 1024
    oldest = _shard(tmp_path, "metrics-2001-01-01-1.jsonl", half_mb, age_days=3)
    mid = _shard(tmp_path, "metrics-2001-01-02-2.jsonl", half_mb, age_days=2)
    newest = _shard(tmp_path, "metrics-2001-01-03-3.jsonl", half_mb, age_days=1)
    exp._prune()
    assert oldest.exists(), "the first destructive size sweep must be deferred"
    exp._prune()
    assert not oldest.exists(), "oldest shard must be pruned first"
    assert newest.exists(), "newest shard is always kept"
    # Total (mid + newest = 1 MB) is now within budget.
    assert mid.exists()


def test_export_rotates_live_shard_before_it_can_grow_past_cap(
    tmp_path, monkeypatch
):
    # Fresh CI runners can have monotonic uptime below the 300s throttle.
    monkeypatch.setattr(local_exporter.time, "monotonic", lambda: 1.0)
    exp = JsonlMetricExporter(tmp_path, max_total_mb=1)
    payload = MagicMock()
    payload.to_json.side_effect = [
        "x" * (700 * 1024),
        "y" * (700 * 1024),
    ]

    assert exp.export(payload).name == "SUCCESS"
    # Pruning is intentionally throttled, so reopen its gate for this explicit
    # second cycle. The append must rotate the 700 KiB live shard first.
    exp._last_prune = float("-inf")
    assert exp.export(payload).name == "SUCCESS"

    # The first destructive sweep warns and preserves both shards. The next
    # eligible sweep enforces the cap and removes the closed rotated shard.
    assert len(list(tmp_path.glob("metrics-*.jsonl"))) == 2
    exp._prune()

    shards = list(tmp_path.glob("metrics-*.jsonl"))
    assert shards == [exp._target_file()]
    assert shards[0].stat().st_size < 1024 * 1024


def test_export_locks_only_the_prune_phase(tmp_path, monkeypatch):
    exp = JsonlMetricExporter(tmp_path, max_total_mb=1)
    payload = MagicMock()
    payload.to_json.return_value = '{"resource_metrics": []}'
    lock_held = False
    events = []

    def tracked_acquire(_fd, *, exclusive=True):
        nonlocal lock_held
        assert exclusive is True
        lock_held = True
        events.append("acquire")
        return True

    def tracked_release(_fd):
        nonlocal lock_held
        assert lock_held is True
        events.append("release")
        lock_held = False

    def assert_prune_inside_lock():
        assert lock_held is True
        events.append("prune")

    monkeypatch.setattr(platform_compat, "try_acquire_lock", tracked_acquire)
    monkeypatch.setattr(platform_compat, "release_lock", tracked_release)
    monkeypatch.setattr(exp, "_prune", assert_prune_inside_lock)

    assert exp.export(payload).name == "SUCCESS"
    assert events == ["acquire", "prune", "release"]
    assert lock_held is False
    lock_path = tmp_path / ".metrics.lock"
    assert lock_path.read_bytes() == b"\0"
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_export_writes_when_prune_lock_is_contended(tmp_path, monkeypatch):
    exp = JsonlMetricExporter(tmp_path, max_total_mb=1)
    payload = MagicMock()
    payload.to_json.return_value = '{"resource_metrics": []}'
    pruned = False

    def mark_pruned():
        nonlocal pruned
        pruned = True

    monkeypatch.setattr(
        platform_compat, "try_acquire_lock", lambda _fd, *, exclusive: False
    )
    monkeypatch.setattr(exp, "_prune", mark_pruned)

    assert exp.export(payload).name == "SUCCESS"
    shards = list(tmp_path.glob("metrics-*.jsonl"))
    assert len(shards) == 1
    assert shards[0].read_text(encoding="utf-8") == '{"resource_metrics": []}\n'
    assert pruned is False


def test_prune_does_not_warn_when_nothing_is_eligible(tmp_path, caplog):
    exp = JsonlMetricExporter(tmp_path, retention_days=7)
    fresh = _shard(tmp_path, "metrics-2000-01-02-2.jsonl", 10, age_days=1)

    with caplog.at_level("WARNING"):
        exp._prune()

    assert fresh.exists()
    assert not [
        record
        for record in caplog.records
        if "local metrics retention will permanently delete" in record.message
    ]
    assert exp._retention_notice_emitted is False


def test_prune_disabled_when_both_caps_zero(tmp_path):
    exp = JsonlMetricExporter(tmp_path, retention_days=0, max_total_mb=0)
    old = _shard(tmp_path, "metrics-1999-01-01-1.jsonl", 10, age_days=999)
    exp._maybe_prune()
    assert old.exists(), "with both caps disabled, nothing is pruned"


def test_prune_ignores_files_outside_exact_generated_shard_grammar(tmp_path):
    exp = JsonlMetricExporter(tmp_path, retention_days=1)
    names = [
        "not-a-shard.txt",
        "metrics-production.jsonl",
        "metrics-2026-13-01-123.jsonl",
        "metrics-2026-01-01-0.jsonl",
        "metrics-2026-01-01-0123.jsonl",
        "metrics-2026-01-01-123-label.jsonl",
        "metrics-2026-01-01-123-0.jsonl",
    ]
    unrelated = [_shard(tmp_path, name, 10, age_days=999) for name in names]

    exp._prune()

    assert all(path.exists() for path in unrelated)


def test_export_then_prune_never_raises(tmp_path):
    # Export path must remain best-effort even with retention enabled.
    reader = PeriodicExportingMetricReader(
        JsonlMetricExporter(tmp_path, retention_days=7, max_total_mb=1),
        export_interval_millis=3_600_000.0,
    )
    provider = MeterProvider(metric_readers=[reader])
    try:
        provider.get_meter("test").create_counter("kirocrew.test.count").add(1)
        provider.force_flush()
        assert list(tmp_path.glob("metrics-*.jsonl"))
    finally:
        provider.shutdown()
