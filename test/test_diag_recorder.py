"""Tests for :mod:`kiro_crew.diag.recorder`.

Every host read is injected — a fixture ``/proc`` tree, a fake clock, a fake
``statvfs`` — so the assertions are about the recorder's logic and not about
whatever the machine running the suite happens to be doing. The one exception is
:func:`kiro_crew.diag.recorder._read_posture`, which is stubbed where a test
needs a specific posture.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
from pathlib import Path

import pytest

from kiro_crew.diag import recorder as rec

# ── fixtures ─────────────────────────────────────────────────────────────────


class _PerfClock:
    """Stand-in for the module's ``time``, with a hand-advanced perf counter.

    A sleeping source would make the back-off tests depend on how loaded the
    host is, and this suite shares a machine with other work. Advancing a
    counter makes "this sample cost 300 ms" a fact rather than a hope.
    """

    def __init__(self, wall: float = 1_700_000_000.0) -> None:
        self.perf = 0.0
        self.wall = wall

    def perf_counter(self) -> float:
        return self.perf

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.perf

    def advance(self, seconds: float) -> float:
        self.perf += seconds
        return self.perf


@pytest.fixture()
def perf_clock(monkeypatch: pytest.MonkeyPatch) -> _PerfClock:
    clock = _PerfClock()
    monkeypatch.setattr(rec, "time", clock)
    return clock


def _write_procfs(root: Path, *, mem_available_kb: int = 8_000_000) -> Path:
    """A minimal ``/proc`` with the three files the recorder reads."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "loadavg").write_text("1.50 2.25 3.75 2/900 12345\n", encoding="utf-8")
    (root / "meminfo").write_text(
        "MemTotal:       16000000 kB\n"
        f"MemAvailable:   {mem_available_kb} kB\n"
        "SwapTotal:       2000000 kB\n"
        "SwapFree:        1500000 kB\n",
        encoding="utf-8",
    )
    self_dir = root / "self"
    self_dir.mkdir(exist_ok=True)
    (self_dir / "status").write_text("Name:\tpython\nThreads:\t7\n", encoding="utf-8")
    fd_dir = self_dir / "fd"
    fd_dir.mkdir(exist_ok=True)
    for i in range(4):
        (fd_dir / str(i)).write_text("", encoding="utf-8")
    return root


def _fake_statvfs(used_pct: float):
    class _Result:
        f_frsize = 4096
        f_bsize = 4096
        f_blocks = 1000
        f_bavail = int(1000 * (1.0 - used_pct / 100.0))

    def call(_path: str) -> "_Result":
        return _Result()

    return call


@pytest.fixture(autouse=True)
def _data_home(tmp_path: Path) -> None:
    """Create the data home every recorder in this module is pointed at.

    The gateway's ``start`` creates it before the first append. Tests reach
    ``_append`` without going through ``start``, and the branch without the POSIX
    primitives resolves this directory rather than creating it, so an absent home
    makes those appends write nothing -- invisibly on POSIX, where the other
    branch creates the directory itself. Autouse rather than a line in ``_make``
    because several tests construct a Recorder directly.
    """
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)


def _make(tmp_path: Path, **kwargs):
    procfs = kwargs.pop("procfs", None) or _write_procfs(tmp_path / "proc")
    return rec.Recorder(
        config_dir=tmp_path / "home",
        env=dict(kwargs.pop("env", {}) or {}),
        clock=kwargs.pop("clock", lambda: 1_700_000_000.0),
        procfs=procfs,
        statvfs=kwargs.pop("statvfs", None) or _fake_statvfs(10.0),
        **kwargs,
    )


def _rows(recorder: rec.Recorder) -> list[dict]:
    path = Path(recorder.health()["file"])
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── the contract worker C's routes depend on ─────────────────────────────────


def test_contract_names_exist_and_accept_the_documented_arguments() -> None:
    """The route-facing names, with the keywords the spec documents.

    This is the assertion that lets the debug server drop its HTTP 501 stubs: a
    route calls exactly these names with exactly these keywords, so a rename
    here must fail in this repository rather than at runtime in another PR.
    """
    for name in (
        "start",
        "stop",
        "sample_now",
        "query",
        "health",
        "register_source",
        "emit_event",
    ):
        assert callable(getattr(rec.Recorder, name)), name
    assert callable(rec.get_recorder)

    query_params = set(inspect.signature(rec.Recorder.query).parameters)
    assert {
        "since",
        "until",
        "around",
        "radius",
        "fields",
        "events_only",
        "cursor",
    } <= query_params


def test_get_recorder_is_none_before_start_and_the_instance_after(tmp_path: Path) -> None:
    rec._recorder = None
    assert rec.get_recorder() is None
    recorder = _make(tmp_path, env={rec.ENV_ENABLED: "0"})
    recorder.start(None)  # disabled, so no loop is needed
    assert rec.get_recorder() is recorder
    rec._recorder = None


def test_health_reports_the_documented_keys(tmp_path: Path) -> None:
    health = _make(tmp_path).health()
    for key in ("running", "last_sample_ts", "interval", "backoff", "file", "rows_today"):
        assert key in health, key


# ── sampling reads what was injected, and nulls what is missing ──────────────


def test_sample_reads_the_injected_procfs(tmp_path: Path) -> None:
    row = _make(tmp_path).sample_now()
    assert (row["load1"], row["load5"], row["load15"]) == (1.50, 2.25, 3.75)
    assert row["mem_total_mb"] == pytest.approx(15625.0, abs=1.0)
    assert row["mem_available_mb"] == pytest.approx(7812.5, abs=1.0)
    # SwapTotal 2000000 kB - SwapFree 1500000 kB = 500000 kB used.
    assert row["swap_used_mb"] == pytest.approx(488.3, abs=1.0)
    assert row["process"]["threads"] == 7
    assert row["process"]["fds"] == 4


def test_missing_procfs_yields_null_fields_not_guesses(tmp_path: Path) -> None:
    """An unreadable field is ``None``. This is the macOS/Windows contract.

    Zero would be a lie that reads as a measurement: "0 MB available" and "we
    could not tell" lead to opposite conclusions, and only one of them is true.
    """
    row = _make(tmp_path, procfs=tmp_path / "no-such-proc").sample_now()
    assert row["mem_available_mb"] is None
    assert row["swap_used_mb"] is None
    assert row["mem_total_mb"] is None


def test_mount_usage_is_reported_per_watched_mount(tmp_path: Path) -> None:
    recorder = rec.Recorder(
        config_dir=tmp_path / "home",
        env={},
        clock=lambda: 1_700_000_000.0,
        procfs=_write_procfs(tmp_path / "proc"),
        statvfs=_fake_statvfs(42.0),
        mounts=("/tmp",),
    )
    usage = recorder.sample_now()["mounts"]["/tmp"]
    assert usage["used_pct"] == pytest.approx(42.0, abs=0.5)


def test_a_platform_without_statvfs_constructs_and_reports_null_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows has no ``os.statvfs`` at all.

    Naming it while building the recorder raises ``AttributeError`` on the gateway
    boot path, which takes the whole gateway down rather than degrading one field.
    So the probe is resolved defensively and the mount figures report "not
    measured", like every other platform-specific field.
    """
    monkeypatch.delattr(os, "statvfs", raising=False)
    recorder = rec.Recorder(
        config_dir=tmp_path / "home",
        env={},
        clock=lambda: 1_700_000_000.0,
        procfs=_write_procfs(tmp_path / "proc"),
        mounts=("/tmp",),
    )
    row = recorder.sample_now()
    assert row["mounts"]["/tmp"] == {"total_mb": None, "used_mb": None, "used_pct": None}
    # The rest of the row is unaffected: one absent probe costs one field.
    assert row["load1"] == 1.50
    assert row["process"]["threads"] == 7


def test_a_mount_that_cannot_be_stat_ed_reports_null(tmp_path: Path) -> None:
    def refuse(_path: str):
        raise OSError("no such filesystem")

    recorder = rec.Recorder(
        config_dir=tmp_path / "home",
        env={},
        clock=lambda: 1_700_000_000.0,
        procfs=_write_procfs(tmp_path / "proc"),
        statvfs=refuse,
        mounts=("/nope",),
    )
    assert recorder.sample_now()["mounts"]["/nope"]["used_pct"] is None


def test_a_source_that_raises_does_not_cost_the_row(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    recorder.register_source("boom", lambda: 1 / 0)
    recorder.register_source("fine", lambda: {"value": 3})
    row = recorder.sample_now()
    assert "ZeroDivisionError" in row["boom"]["error"]
    assert row["fine"]["value"] == 3
    assert row["load1"] == 1.50  # the rest of the row survived


def test_a_source_returning_a_non_dict_is_recorded_as_an_error(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    recorder.register_source("wrong", lambda: ["not", "a", "dict"])
    assert "not dict" in recorder.sample_now()["wrong"]["error"]


def test_register_source_rejects_a_non_callable(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        _make(tmp_path).register_source("bad", object())


# ── the budget back-off rule ─────────────────────────────────────────────────


def test_three_consecutive_slow_samples_back_off_to_sixty_seconds(
    tmp_path: Path, perf_clock: _PerfClock, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _make(tmp_path)
    recorder.register_source("slow", lambda: (perf_clock.advance(0.3), {"ok": True})[1])

    with caplog.at_level("WARNING"):
        for _ in range(rec.SAMPLE_SLOW_STREAK - 1):
            recorder.sample_now()
            assert recorder.health()["backoff"] is False
        recorder.sample_now()

    health = recorder.health()
    assert health["backoff"] is True
    assert health["interval"] == rec.BACKOFF_SAMPLE_SECS
    assert sum("cadence reduced" in r.message for r in caplog.records) == 1


def test_the_back_off_warning_is_logged_once_not_per_sample(
    tmp_path: Path, perf_clock: _PerfClock, caplog: pytest.LogCaptureFixture
) -> None:
    recorder = _make(tmp_path)
    recorder.register_source("slow", lambda: (perf_clock.advance(0.3), {})[1])
    with caplog.at_level("WARNING"):
        for _ in range(rec.SAMPLE_SLOW_STREAK + 4):
            recorder.sample_now()
    assert sum("cadence reduced" in r.message for r in caplog.records) == 1


def test_a_fast_sample_resets_the_slow_streak(tmp_path: Path, perf_clock: _PerfClock) -> None:
    """Two slow samples then a fast one must not back off.

    The rule is about sustained cost. Without the reset, three slow samples
    spread over an hour would read the same as three in a row, and the cadence
    would drop on a host that was briefly busy twice.
    """
    recorder = _make(tmp_path)
    cost = {"secs": 0.3}
    recorder.register_source("var", lambda: (perf_clock.advance(cost["secs"]), {})[1])

    recorder.sample_now()
    recorder.sample_now()
    cost["secs"] = 0.0
    recorder.sample_now()
    cost["secs"] = 0.3
    recorder.sample_now()

    assert recorder.health()["backoff"] is False


def test_a_sample_over_budget_but_under_the_slow_line_is_flagged_not_backed_off(
    tmp_path: Path, perf_clock: _PerfClock
) -> None:
    recorder = _make(tmp_path)
    # 50 ms: over the 20 ms budget, well under the 200 ms back-off line.
    recorder.register_source("mid", lambda: (perf_clock.advance(0.05), {})[1])
    for _ in range(rec.SAMPLE_SLOW_STREAK + 1):
        row = recorder.sample_now()
    assert row["over_budget"] is True
    assert recorder.health()["backoff"] is False


def test_every_row_carries_its_own_cost(tmp_path: Path) -> None:
    row = _make(tmp_path).sample_now()
    assert isinstance(row["cost_ms"], float)
    assert "over_budget" in row


# ── retention ────────────────────────────────────────────────────────────────


def _day_file(recorder: rec.Recorder, day: str) -> Path:
    path = recorder._dir() / f"{rec.FILE_PREFIX}{day}{rec.FILE_SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": 1}\n', encoding="utf-8")
    return path


def test_retention_deletes_files_past_the_window_and_keeps_the_rest(tmp_path: Path) -> None:
    # The injected clock pins every day key these files use, so the retention
    # boundary below is exact rather than relative to the day the suite runs.
    recorder = _make(tmp_path, env={rec.ENV_RETAIN_DAYS: "7"})
    stale = _day_file(recorder, "20231101")
    fresh = _day_file(recorder, "20231113")
    today = _day_file(recorder, "20231114")

    assert recorder._prune() == 1
    assert not stale.exists()
    assert fresh.exists() and today.exists()


@pytest.mark.skipif(
    not rec._pinned_dir_fd_supported(), reason="pinned dir-fd operations are POSIX-only"
)
def test_append_refuses_a_snapshot_symlink(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    snapshot = Path(recorder.health()["file"])
    snapshot.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    snapshot.symlink_to(outside)

    recorder._append({"ts": 1_700_000_000.0, "value": "must not escape"})

    assert outside.read_text(encoding="utf-8") == "keep"
    assert snapshot.is_symlink()
    assert recorder.health()["samples_written"] == 0


def test_append_refuses_a_planted_link_without_an_atomic_no_follow_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _make(tmp_path)
    snapshot = Path(recorder.health()["file"])
    snapshot.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    snapshot.symlink_to(outside)
    monkeypatch.setattr(rec, "_pinned_dir_fd_supported", lambda: False)

    recorder._append({"ts": 1_700_000_000.0, "value": "must not escape"})

    assert outside.read_text(encoding="utf-8") == "keep"
    assert snapshot.is_symlink()
    assert recorder.health()["samples_written"] == 0


def test_append_writes_an_ordinary_path_without_an_atomic_no_follow_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform without the POSIX primitives still records.

    The recorder is on by default, so a branch that refused every write there
    would make the whole component a silent no-op on that platform.
    """
    recorder = _make(tmp_path)
    snapshot = Path(recorder.health()["file"])
    # Only the data home, which ``start`` creates before any append runs. The leaf
    # directory is deliberately absent: on this branch ``append_line`` creates it
    # under the anchor it resolved, which is why the append path itself does not
    # need to ask for the directory by path first.
    snapshot.parent.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rec, "_pinned_dir_fd_supported", lambda: False)

    recorder._append({"ts": 1_700_000_000.0, "load1": 1.5})

    assert snapshot.parent.is_dir()

    assert recorder.health()["samples_written"] == 1
    assert Path(recorder.health()["file"]).exists()
    out = recorder.query(since=0, until=1_800_000_000.0)
    assert [row["load1"] for row in out["series"]] == [1.5]


def test_fallback_append_and_retention_refuse_a_redirected_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _make(tmp_path, env={rec.ENV_RETAIN_DAYS: "7"})
    recorder._config_dir().mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    stale = outside / f"{rec.FILE_PREFIX}20231101{rec.FILE_SUFFIX}"
    stale.write_text('{"ts":1}\n', encoding="utf-8")
    recorder._dir().symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(rec, "_pinned_dir_fd_supported", lambda: False)

    recorder._append({"ts": 1_700_000_000.0, "value": "must not escape"})

    assert recorder.health()["samples_written"] == 0
    assert recorder._prune() == 0
    assert stale.read_text(encoding="utf-8") == '{"ts":1}\n'
    assert list(outside.iterdir()) == [stale]


@pytest.mark.skipif(
    not rec._pinned_dir_fd_supported(), reason="pinned dir-fd operations are POSIX-only"
)
def test_append_refuses_a_hard_linked_snapshot_name(tmp_path: Path) -> None:
    """A hard link passes S_ISREG, so the link count is what refuses it.

    O_NOFOLLOW covers a symlink and nothing else. A hard link planted at the
    snapshot name is a regular file by every other measure, and appending through
    it writes the gateway's diagnostics into whatever else carries that inode.
    """
    recorder = _make(tmp_path)
    snapshot = Path(recorder.health()["file"])
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    victim = tmp_path / "home" / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    os.link(victim, snapshot)

    recorder._append({"ts": 1_700_000_000.0, "value": "must not escape"})

    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert recorder.health()["samples_written"] == 0


@pytest.mark.skipif(
    not rec._pinned_dir_fd_supported(), reason="pinned dir-fd operations are POSIX-only"
)
def test_retention_stays_with_the_opened_directory_during_a_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _make(tmp_path, env={rec.ENV_RETAIN_DAYS: "7"})
    stale = _day_file(recorder, "20231101")
    diag_dir = recorder._dir()
    detached = tmp_path / "detached-diag"
    replacement = diag_dir / stale.name
    real_listdir = os.listdir

    def swap_after_listing(dir_fd: int) -> list[str]:
        entries = real_listdir(dir_fd)
        diag_dir.rename(detached)
        diag_dir.mkdir()
        replacement.write_text("replacement", encoding="utf-8")
        return entries

    monkeypatch.setattr(rec, "_pinned_dir_fd_supported", lambda: True)
    monkeypatch.setattr(os, "listdir", swap_after_listing)

    assert recorder._prune() == 1
    assert not (detached / stale.name).exists()
    assert replacement.read_text(encoding="utf-8") == "replacement"


def test_retention_is_keyed_on_the_filename_date_not_the_mtime(tmp_path: Path) -> None:
    """A file appended to today still describes the day in its name.

    Rows are appended all day, so every retained file's mtime is recent — an
    mtime rule would keep the oldest file forever and the directory would grow
    without bound while appearing to have retention.
    """
    recorder = _make(tmp_path)
    stale = _day_file(recorder, "20200101")
    os.utime(stale, None)  # mtime = now, name = long past
    assert recorder._prune() == 1
    assert not stale.exists()


def test_retention_ignores_files_it_does_not_own(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    recorder._dir().mkdir(parents=True, exist_ok=True)
    bystander = recorder._dir() / "notes.txt"
    bystander.write_text("keep me", encoding="utf-8")
    malformed = recorder._dir() / f"{rec.FILE_PREFIX}notaday{rec.FILE_SUFFIX}"
    malformed.write_text("{}", encoding="utf-8")

    recorder._prune()
    assert bystander.exists()
    assert malformed.exists()


def test_retain_days_comes_from_the_environment(tmp_path: Path) -> None:
    assert _make(tmp_path, env={rec.ENV_RETAIN_DAYS: "3"}).retain_days == 3


@pytest.mark.parametrize("value", ["inf", "-inf"])
def test_non_finite_retain_days_fall_back_to_the_default(tmp_path: Path, value: str) -> None:
    recorder = _make(tmp_path, env={rec.ENV_RETAIN_DAYS: value})
    assert recorder.retain_days == rec.DEFAULT_RETAIN_DAYS


# ── events ───────────────────────────────────────────────────────────────────


def test_emit_event_writes_an_event_row_with_its_kind_and_payload(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    recorder.emit_event(rec.EVENT_LOOP_STALL, {"drift_ms": 1200.0})
    rows = _rows(recorder)
    assert len(rows) == 1
    assert rows[0]["kind"] == rec.EVENT_LOOP_STALL
    assert rows[0]["payload"]["drift_ms"] == 1200.0


def test_a_memory_posture_change_emits_an_event_and_the_first_sample_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first sample establishes the baseline; only a CHANGE is news."""
    posture = {
        "posture": "ample",
        "available_gb": 30.0,
        "pressure_gb": 4.0,
        "critical_gb": 2.0,
        "cpu_count": 8,
        "load_per_cpu": 0.2,
    }
    monkeypatch.setattr(rec, "_read_posture", lambda: dict(posture))
    recorder = _make(tmp_path)

    recorder.sample_now()
    assert [r for r in _rows(recorder) if r.get("kind")] == []

    posture["posture"] = "tight"
    recorder.sample_now()
    change = next(r for r in _rows(recorder) if r.get("kind") == rec.EVENT_MEMORY_POSTURE)
    assert change["payload"] == {"from": "ample", "to": "tight", "available_gb": 30.0}


def test_memory_below_the_pressure_line_emits_a_threshold_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rec,
        "_read_posture",
        lambda: {
            "posture": "tight",
            "available_gb": 1.5,
            "pressure_gb": 4.0,
            "critical_gb": 2.0,
            "cpu_count": 8,
            "load_per_cpu": 0.2,
        },
    )
    recorder = _make(tmp_path)
    recorder.sample_now()
    signals = [
        r["payload"]["signal"] for r in _rows(recorder) if r.get("kind") == rec.EVENT_THRESHOLD
    ]
    assert "memory_below_pressure" in signals


def test_a_full_mount_emits_a_threshold_event(tmp_path: Path) -> None:
    recorder = rec.Recorder(
        config_dir=tmp_path / "home",
        env={},
        clock=lambda: 1_700_000_000.0,
        procfs=_write_procfs(tmp_path / "proc"),
        statvfs=_fake_statvfs(95.0),
        mounts=("/tmp",),
    )
    recorder.sample_now()
    events = [r for r in _rows(recorder) if r.get("kind") == rec.EVENT_THRESHOLD]
    full = [e for e in events if e["payload"]["signal"] == "mount_full"]
    assert full and full[0]["payload"]["mount"] == "/tmp"


def test_load_above_twice_the_cpu_count_emits_a_threshold_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rec,
        "_read_posture",
        lambda: {
            "posture": "ample",
            "available_gb": 30.0,
            "pressure_gb": 4.0,
            "critical_gb": 2.0,
            "cpu_count": 4,
            "load_per_cpu": 3.0,
        },
    )
    procfs = _write_procfs(tmp_path / "proc")
    # load1 = 12.0 against 4 CPUs, i.e. 3x — past the 2x line. The shared fixture
    # reports 1.50, which no CPU count can put over 2x, so this test owns its own.
    (procfs / "loadavg").write_text("12.00 9.00 6.00 8/900 12345\n", encoding="utf-8")

    recorder = _make(tmp_path, procfs=procfs)
    recorder.sample_now()
    signals = [
        r["payload"]["signal"] for r in _rows(recorder) if r.get("kind") == rec.EVENT_THRESHOLD
    ]
    assert "load_high" in signals


def test_load_within_twice_the_cpu_count_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rec,
        "_read_posture",
        lambda: {
            "posture": "ample",
            "available_gb": 30.0,
            "pressure_gb": 4.0,
            "critical_gb": 2.0,
            "cpu_count": 4,
            "load_per_cpu": 0.4,
        },
    )
    recorder = _make(tmp_path)  # load1 = 1.50 against 4 CPUs
    recorder.sample_now()
    assert [r for r in _rows(recorder) if r.get("kind") == rec.EVENT_THRESHOLD] == []


def test_threshold_events_fire_once_per_active_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posture = {
        "posture": "tight",
        "available_gb": 1.5,
        "pressure_gb": 4.0,
        "critical_gb": 2.0,
        "cpu_count": 4,
        "load_per_cpu": 3.0,
    }
    mount_pct = {"value": 95.0}
    monkeypatch.setattr(rec, "_read_posture", lambda: dict(posture))
    procfs = _write_procfs(tmp_path / "proc")
    loadavg = procfs / "loadavg"
    loadavg.write_text("12.00 9.00 6.00 8/900 12345\n", encoding="utf-8")
    recorder = rec.Recorder(
        config_dir=tmp_path / "home",
        env={},
        clock=lambda: 1_700_000_000.0,
        procfs=procfs,
        statvfs=lambda path: _fake_statvfs(mount_pct["value"])(path),
        mounts=("/tmp",),
    )

    recorder.sample_now()
    recorder.sample_now()

    posture["available_gb"] = 30.0
    mount_pct["value"] = 10.0
    loadavg.write_text("1.50 1.00 0.50 2/900 12345\n", encoding="utf-8")
    recorder.sample_now()

    posture["available_gb"] = 1.5
    mount_pct["value"] = 95.0
    loadavg.write_text("12.00 9.00 6.00 8/900 12345\n", encoding="utf-8")
    recorder.sample_now()

    signals = [
        row["payload"]["signal"]
        for row in _rows(recorder)
        if row.get("kind") == rec.EVENT_THRESHOLD
    ]
    assert signals.count("memory_below_pressure") == 2
    assert signals.count("mount_full") == 2
    assert signals.count("load_high") == 2


def test_the_adaptive_cap_falling_emits_an_event_and_climbing_does_not(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    cap = {"value": 8}
    recorder.register_source(
        "gateway", lambda: {"adaptive_cap": cap["value"], "adaptive_reason": "memory"}
    )

    recorder.sample_now()  # baseline
    cap["value"] = 3
    recorder.sample_now()  # a drop: news
    cap["value"] = 9
    recorder.sample_now()  # a climb: the system recovering, not an event

    drops = [r for r in _rows(recorder) if r.get("kind") == rec.EVENT_ADAPTIVE_CAP_LOWERED]
    assert len(drops) == 1
    assert drops[0]["payload"] == {"from": 8, "to": 3, "reason": "memory"}


def test_a_rewritten_config_file_emits_an_event_without_reading_its_bytes(
    tmp_path: Path,
) -> None:
    """The event is driven by ``stat`` alone.

    ``config.local.json`` is watched and can hold credentials, so the secret below
    is the assertion: if any code path read the file to detect the change, its
    contents would reach a snapshot row.
    """
    recorder = _make(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    config_file = home / "config.local.json"
    secret = "SUPER_SECRET_TOKEN_VALUE_9f3a"
    config_file.write_text(f'{{"token": "{secret}"}}', encoding="utf-8")

    recorder.sample_now()  # baseline stat
    config_file.write_text(f'{{"token": "{secret}", "extra": 1}}', encoding="utf-8")
    recorder.sample_now()

    rewrites = [r for r in _rows(recorder) if r.get("kind") == rec.EVENT_CONFIG_REWRITTEN]
    assert [r["payload"]["file"] for r in rewrites] == ["config.local.json"]
    assert rewrites[0]["payload"]["exists_now"] is True
    written = Path(recorder.health()["file"]).read_text(encoding="utf-8")
    assert secret not in written


def test_the_boot_gate_spells_the_same_off_values_as_the_module() -> None:
    """The gate on the boot path repeats this module's off vocabulary.

    It has to repeat it rather than import it, because avoiding the import IS the
    point of the gate. That leaves two copies of one fact, so this pins them
    together: an off value added here without the other is a switch that stops
    working at the only place an operator sets it.
    """
    import re

    source = (Path(rec.__file__).parent.parent / "dashboard" / "server.py").read_text(
        encoding="utf-8"
    )
    gate = re.search(
        r'os\.environ\.get\("KIROCREW_DIAG_RECORDER".*?\)\s*\.strip\(\)\.lower\(\)\s*in\s*\((.*?)\)',
        source,
        re.DOTALL,
    )
    assert gate is not None, "the boot gate is not where this test expects it"
    spelled = set(re.findall(r'"([^"]+)"', gate.group(1)))
    assert spelled == set(rec._TRUTHY_OFF)


def test_the_env_file_is_not_watched_at_all(tmp_path: Path) -> None:
    """Not even its metadata.

    The sandbox hides ``.env`` in every mode. Its size and modification time in an
    agent-readable diagnostic leaf would describe a file that mask exists to keep
    out of reach, down to when its secrets were last rotated, so the watched set
    leaves it out rather than recording it carefully.
    """
    assert ".env" not in rec.WATCHED_CONFIG_FILES

    recorder = _make(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env_file = home / ".env"
    env_file.write_text("TOKEN=abc\n", encoding="utf-8")

    recorder.sample_now()
    env_file.write_text("TOKEN=abc\nEXTRA=1\n", encoding="utf-8")
    recorder.sample_now()

    files = [
        r["payload"]["file"] for r in _rows(recorder) if r.get("kind") == rec.EVENT_CONFIG_REWRITTEN
    ]
    assert ".env" not in files
    # No events at all means no file, which is the strongest form of "not
    # recorded"; when other events did land, the name must still be absent.
    snapshot = Path(recorder.health()["file"])
    if snapshot.exists():
        assert ".env" not in snapshot.read_text(encoding="utf-8")


def test_a_deleted_config_file_is_reported_as_gone(tmp_path: Path) -> None:
    """The zero-byte ``.env`` incident: disappearance must be an event too."""
    recorder = _make(tmp_path)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text("{}", encoding="utf-8")

    recorder.sample_now()
    (home / "config.json").unlink()
    recorder.sample_now()

    rewrites = [r for r in _rows(recorder) if r.get("kind") == rec.EVENT_CONFIG_REWRITTEN]
    assert rewrites and rewrites[0]["payload"]["exists_now"] is False
    assert rewrites[0]["payload"]["existed_before"] is True


# ── query ────────────────────────────────────────────────────────────────────


def _seed(recorder: rec.Recorder, count: int, start_ts: float = 1_700_000_000.0) -> None:
    for i in range(count):
        recorder._append({"ts": start_ts + i, "load1": float(i), "process": {"rss_mb": 100.0 + i}})


def test_the_diag_package_resolves_its_exports_lazily() -> None:
    """``kiro_crew.diag`` defers its imports (PEP 562).

    The recorder starts on the gateway boot path, so importing the package must
    not drag in the thread module and its transitive imports; a route asking for
    ``diag.get_recorder`` is what pays for them.
    """
    import kiro_crew.diag as diag

    for name in ("Recorder", "get_recorder"):
        diag.__dict__.pop(name, None)  # force the lazy path rather than a cache hit

    assert diag.Recorder is rec.Recorder
    assert diag.get_recorder is rec.get_recorder
    # Resolved once, then cached into the module globals.
    assert "Recorder" in diag.__dict__
    assert diag.Recorder is rec.Recorder
    assert diag.__dir__() == ["Recorder", "get_recorder"]


def test_the_diag_package_rejects_an_unknown_attribute() -> None:
    """A typo must be an AttributeError, not an accidental import."""
    import kiro_crew.diag as diag

    with pytest.raises(AttributeError):
        diag.no_such_name


def test_an_out_of_range_window_is_answered_not_raised(tmp_path: Path) -> None:
    """``since=0`` is an ordinary "give me everything" request.

    The window is widened by a day at each end before day files are selected, so
    ``since=0`` asks for a negative epoch -- and Windows answers that with
    ``OSError: [Errno 22]`` instead of a date. The window comes from an HTTP
    caller, so raising there would turn a wide query into a 500.
    """
    recorder = _make(tmp_path)
    _seed(recorder, 3)

    wide = recorder.query(since=0, until=1_800_000_000.0)
    assert len(wide["series"]) == 3

    # Both ends unrepresentable, in both directions. The magnitudes are out of
    # range on every platform; a mere negative epoch is refused only by Windows,
    # so asserting on one of those would pass or fail by operating system.
    assert recorder.query(since=-1e18, until=-1e17)["series"] == []
    assert recorder.query(since=1e18, until=1e19)["series"] == []
    assert recorder._day_key(-1e18) == "00000000"
    assert recorder._day_key(1e18) == "99999999"


def test_stats_are_tallied_rather_than_collected(tmp_path: Path) -> None:
    """A wide window must not hold one float per numeric leaf per row.

    The accumulator carries ``[n, total, low, high]`` per field, so its size
    tracks the number of fields and not the number of rows; the reported numbers
    stay exact either way.
    """
    recorder = _make(tmp_path)
    for i in range(200):
        recorder._append({"ts": 1_700_000_000.0 - i, "load1": float(i)})

    accumulators: dict[str, list[float]] = {}
    dropped: set[str] = set()
    for row in recorder._iter_rows(0.0, 1_800_000_000.0):
        recorder._accumulate(row, accumulators, dropped)
    assert accumulators["load1"] == [200.0, sum(range(200)), 0.0, 199.0]
    assert dropped == set()

    stats = recorder.query(since=0, until=1_800_000_000.0)["stats"]["load1"]
    assert stats["n"] == 200
    assert stats["min"] == 0.0
    assert stats["max"] == 199.0
    assert stats["avg"] == round(sum(range(200)) / 200, 3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"around": "1700000000", "radius": "5m"},
        {"around": "2023-11-14T22:13:20+00:00", "radius": "300s"},
        {"around": "2023-11-14T22:13:20", "radius": 300},
        {"since": "2023-11-14T00:00:00Z", "until": "1800000000"},
    ],
)
def test_the_window_accepts_the_shapes_its_callers_document(tmp_path: Path, kwargs) -> None:
    """Durations and ISO instants, not only numbers.

    The MCP schema documents ``radius`` as ``"5m"`` and ``around`` as ISO 8601,
    and a route passes the query string through untouched, so a plain ``float()``
    on these turned a documented call into a 500.
    """
    recorder = _make(tmp_path)
    _seed(recorder, 3)
    assert recorder.query(**kwargs)["series"]


@pytest.mark.parametrize(
    "kwargs,field",
    [
        ({"radius": "banana", "around": 1_700_000_000.0}, "radius"),
        ({"around": "not-a-time"}, "around"),
        ({"since": ""}, "since"),
        ({"until": "5 o'clock"}, "until"),
    ],
)
def test_an_unparseable_window_value_names_the_field_it_came_from(
    tmp_path: Path, kwargs, field: str
) -> None:
    """A caller's mistake is a 400, and it has to say which value was wrong."""
    recorder = _make(tmp_path)
    with pytest.raises(ValueError) as caught:
        recorder.query(**kwargs)
    assert field in str(caught.value)


def test_the_stats_block_is_capped_in_distinct_fields(tmp_path: Path) -> None:
    """The tally is bounded by field COUNT, not only by value count.

    The page is byte-capped; the stats block is not billed against that cap, and
    its size follows the distinct names present in rows read from a file under the
    data home. One row carrying a name per column would otherwise grow the answer
    without limit.
    """
    recorder = _make(tmp_path)
    wide = {f"field_{i}": float(i) for i in range(rec.STATS_FIELD_CAP + 44)}
    recorder._append({"ts": 1_700_000_000.0, **wide})

    answer = recorder.query(since=0, until=1_800_000_000.0)

    assert len(answer["stats"]) == rec.STATS_FIELD_CAP
    assert answer["stats_truncated"] is True
    assert answer["stats_fields_dropped"] == 44


def test_a_narrow_answer_says_its_stats_are_complete(tmp_path: Path) -> None:
    """The flag is off when nothing was dropped, so a reader can trust the block."""
    recorder = _make(tmp_path)
    _seed(recorder, 2)
    answer = recorder.query(since=0, until=1_800_000_000.0)
    assert answer["stats_truncated"] is False
    assert answer["stats_fields_dropped"] == 0


def test_many_rows_sharing_one_instant_paginate_without_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every row is served once, across more pages than the instant has room for.

    The cursor names an instant plus how many rows at it were already served, and
    that count has to accumulate across pages. Counting only the current page's
    rows makes page three resume where page two began, so the rows past it are
    unreachable and the ones before it arrive twice.
    """
    recorder = _make(tmp_path)
    shared = 1_700_000_000.0
    for i in range(9):
        recorder._append({"ts": shared, "seq": float(i), "pad": {"blob": "y" * 80}})
    monkeypatch.setattr(rec, "QUERY_BYTE_CAP", 260)

    seen: list[float] = []
    cursor = None
    for _ in range(30):
        page = recorder.query(since=0, until=1_800_000_000.0, cursor=cursor)
        seen.extend(row["seq"] for row in page["series"])
        cursor = page["cursor"]
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert sorted(seen) == [float(i) for i in range(9)], seen


@pytest.mark.parametrize("field", ["since", "until", "around", "radius"])
def test_a_non_finite_window_value_is_refused_by_name(tmp_path: Path, field: str) -> None:
    """nan slips past every comparison guard, so it is refused where it enters."""
    recorder = _make(tmp_path)
    kwargs: dict[str, object] = {field: float("nan")}
    if field == "radius":
        kwargs["around"] = 1_700_000_000.0
    with pytest.raises(ValueError) as caught:
        recorder.query(**kwargs)
    assert field in str(caught.value)


def test_a_json_line_that_is_not_a_record_is_skipped(tmp_path: Path) -> None:
    """Valid JSON is not necessarily a row.

    A bare list decodes cleanly and then has no ``get``, so reading one without
    this guard raised AttributeError out of a query and answered 500.
    """
    recorder = _make(tmp_path)
    _seed(recorder, 2)
    snapshot = Path(recorder.health()["file"])
    with snapshot.open("a", encoding="utf-8") as handle:
        handle.write("[]\n")
        handle.write('"a string"\n')
        handle.write("17\n")

    answer = recorder.query(since=0, until=1_800_000_000.0)
    assert len(answer["series"]) == 2


def test_query_returns_the_rows_in_the_window_and_their_stats(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 5)
    out = recorder.query(since=1_700_000_000.0, until=1_700_000_004.0)
    assert len(out["series"]) == 5
    assert out["stats"]["load1"] == {"min": 0.0, "max": 4.0, "avg": 2.0, "n": 5}


def test_query_around_and_radius_bound_the_window(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 11)
    out = recorder.query(around=1_700_000_005.0, radius=2.0)
    assert [r["ts"] for r in out["series"]] == [
        1_700_000_003.0,
        1_700_000_004.0,
        1_700_000_005.0,
        1_700_000_006.0,
        1_700_000_007.0,
    ]


def test_query_projects_requested_fields_and_always_keeps_ts(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 2)
    out = recorder.query(since=0, until=1_800_000_000.0, fields=["load1"])
    assert set(out["series"][0]) == {"ts", "load1"}


def test_query_projects_a_nested_leaf(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 2)
    out = recorder.query(since=0, until=1_800_000_000.0, fields=["process.rss_mb"])
    assert out["series"][0]["process"] == {"rss_mb": 100.0}
    assert "load1" not in out["series"][0]


def test_events_only_returns_events_and_no_series(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 3)
    recorder.emit_event(rec.EVENT_GATEWAY_START, {"pid": 1})
    out = recorder.query(since=0, until=1_800_000_000.0, events_only=True)
    assert out["series"] == []
    assert [e["kind"] for e in out["events"]] == [rec.EVENT_GATEWAY_START]


def test_a_large_window_is_cut_on_a_row_boundary_and_resumes_from_the_cursor(
    tmp_path: Path,
) -> None:
    """The cap must never return half a JSON object, and must be resumable.

    Statistics are computed over the whole window rather than the returned page,
    so a truncated answer still reports the true maximum — the number a "what
    happened at T" question is usually actually asking for.
    """
    recorder = _make(tmp_path)
    filler = {"blob": "x" * 500}
    for i in range(400):
        recorder._append({"ts": 1_700_000_000.0 + i, "load1": float(i), "pad": filler})

    first = recorder.query(since=0, until=1_800_000_000.0)
    assert first["truncated"] is True
    assert first["cursor"] is not None
    assert len(first["series"]) < 400
    # Every returned row parsed, i.e. nothing was cut mid-row.
    assert all("ts" in row for row in first["series"])
    # Stats span the window, not the page.
    assert first["stats"]["load1"]["max"] == 399.0
    assert first["rows_in_window"] == 400

    second = recorder.query(since=0, until=1_800_000_000.0, cursor=first["cursor"])
    assert second["series"]
    assert second["series"][0]["ts"] > first["series"][-1]["ts"]


@pytest.mark.parametrize("filler", [117, 118, 119])
def test_a_threshold_crossing_at_a_page_boundary_is_returned_exactly_once(
    tmp_path: Path, filler: int
) -> None:
    """Paginating the whole window returns every row once, cut wherever it falls.

    A sample and the event its numbers reveal share a sampling instant, and a
    cursor naming only that instant cannot say which of the two a page ended on.
    At 118 filler rows the cut lands between them, which is the arrangement that
    served one of them twice; the neighbours cut elsewhere and guard the ordinary
    case.
    """
    recorder = _make(tmp_path)
    pad = {"blob": "x" * 500}
    for i in range(filler):
        recorder._append({"ts": 1_700_000_000.0 + i, "load1": float(i), "pad": pad})
    crossing = 1_700_000_000.0 + filler
    # The order the sampler writes them in: the sample, then its event.
    recorder._append({"ts": crossing, "load1": 99.0, "pad": pad})
    recorder._append(
        {"ts": crossing, "kind": rec.EVENT_THRESHOLD, "payload": {"signal": "mount_full"}}
    )

    seen: list[tuple[float, str]] = []
    cursor = None
    for _ in range(20):
        page = recorder.query(since=0, until=1_800_000_000.0, cursor=cursor)
        for row in page["series"] + page["events"]:
            seen.append((row["ts"], row.get("kind") or "sample"))
        cursor = page["cursor"]
        if cursor is None:
            break

    assert cursor is None, "pagination did not terminate"
    assert len(seen) == len(set(seen)), f"a row was served twice: {sorted(seen)[-4:]}"
    assert (crossing, "sample") in seen
    assert (crossing, rec.EVENT_THRESHOLD) in seen


def test_query_skips_a_partially_written_trailing_line(tmp_path: Path) -> None:
    recorder = _make(tmp_path)
    _seed(recorder, 2)
    with open(recorder.health()["file"], "a", encoding="utf-8") as handle:
        handle.write('{"ts": 1700000009, "load1":')  # torn append, no newline
    out = recorder.query(since=0, until=1_800_000_000.0)
    assert len(out["series"]) == 2


def test_query_of_an_empty_store_is_an_empty_answer_not_an_error(tmp_path: Path) -> None:
    out = _make(tmp_path).query(since=0, until=1_800_000_000.0)
    assert out["series"] == [] and out["events"] == [] and out["stats"] == {}


def test_stats_exclude_booleans(tmp_path: Path) -> None:
    """``over_budget`` is a flag; a min/max/avg of it is noise, not a statistic."""
    recorder = _make(tmp_path)
    recorder._append({"ts": 1_700_000_000.0, "over_budget": True, "load1": 1.0})
    out = recorder.query(since=0, until=1_800_000_000.0)
    assert "over_budget" not in out["stats"]
    assert "load1" in out["stats"]


# ── the off switch ───────────────────────────────────────────────────────────


def test_the_recorder_is_on_by_default(tmp_path: Path) -> None:
    assert _make(tmp_path).enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF"])
def test_the_documented_off_switch_disables_it(tmp_path: Path, value: str) -> None:
    assert _make(tmp_path, env={rec.ENV_ENABLED: value}).enabled is False


def test_start_writes_nothing_when_disabled(tmp_path: Path) -> None:
    recorder = _make(tmp_path, env={rec.ENV_ENABLED: "0"})
    recorder.start(None)
    assert recorder.health()["running"] is False
    assert _rows(recorder) == []
    rec._recorder = None


def test_lifecycle_blocking_work_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.diag import threads as diag_threads

    recorder = _make(tmp_path)
    diag_dir = recorder._dir()
    mkdir_threads: list[int] = []
    event_threads: list[tuple[str, int]] = []
    probe_start_threads: list[int] = []
    probe_stop_threads: list[int] = []
    real_mkdir = Path.mkdir

    def record_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == diag_dir:
            mkdir_threads.append(threading.get_ident())
        real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    monkeypatch.setattr(
        recorder,
        "emit_event",
        lambda kind, _payload: event_threads.append((kind, threading.get_ident())),
    )
    monkeypatch.setattr(
        diag_threads,
        "start_probe",
        lambda **_kwargs: probe_start_threads.append(threading.get_ident()),
    )
    monkeypatch.setattr(
        diag_threads, "stop_probe", lambda: probe_stop_threads.append(threading.get_ident())
    )

    async def exercise() -> tuple[int, list[asyncio.Task[object]]]:
        loop_thread = threading.get_ident()
        loop = asyncio.get_running_loop()
        recorder.start(loop)
        while not any(kind == rec.EVENT_GATEWAY_START for kind, _thread in event_threads):
            await asyncio.sleep(0)
        active_tasks = list(recorder._tasks)
        recorder.stop()
        while not probe_stop_threads:
            await asyncio.sleep(0)
        return loop_thread, active_tasks

    loop_thread, active_tasks = asyncio.run(exercise())

    assert mkdir_threads and all(thread != loop_thread for thread in mkdir_threads)
    assert event_threads and all(thread != loop_thread for _kind, thread in event_threads)
    assert probe_stop_threads[0] != loop_thread
    assert probe_start_threads == [loop_thread]
    assert len(active_tasks) == 2
    assert all(task.get_loop().is_closed() for task in active_tasks)


def test_start_does_not_launch_the_probe_when_directory_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.diag import threads as diag_threads

    config_file = tmp_path / "config-file"
    config_file.write_text("not a directory", encoding="utf-8")
    recorder = _make(tmp_path)
    recorder._config_dir_override = config_file
    starts: list[bool] = []
    monkeypatch.setattr(diag_threads, "start_probe", lambda **_kwargs: starts.append(True))

    # An explicit, non-running loop: ``start`` requires one when nothing runs.
    loop = asyncio.new_event_loop()
    try:
        recorder.start(loop)
    finally:
        loop.close()

    assert starts == []
    assert recorder._running is False
    rec._recorder = None


def test_start_without_any_loop_raises_before_touching_state(tmp_path: Path) -> None:
    """No running loop and no explicit loop is a caller error, not a silent no-op.

    ``asyncio.get_event_loop`` would have MADE a loop nothing runs, so the
    recorder would look started and record nothing. The replacement raises, and
    raises before any state moves, so the caller can retry with a loop.
    """
    recorder = _make(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="running event loop or an explicit loop"):
            recorder.start(None)
        assert recorder._running is False
        assert recorder._tasks == []
    finally:
        rec._recorder = None


def test_stop_hands_back_the_task_that_writes_the_closing_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that awaits ``stop`` sees the closing event written.

    The finish runs in a thread, so a hook that calls ``stop`` and returns lets
    loop teardown race it and the closing event is the row that never lands.
    """
    from kiro_crew.diag import threads as diag_threads

    monkeypatch.setattr(diag_threads, "start_probe", lambda **_kwargs: None)
    monkeypatch.setattr(diag_threads, "stop_probe", lambda: None)
    recorder = _make(tmp_path)
    kinds: list[str] = []
    monkeypatch.setattr(recorder, "emit_event", lambda kind, _payload: kinds.append(kind))

    async def exercise() -> object:
        recorder.start(asyncio.get_running_loop())
        while rec.EVENT_GATEWAY_START not in kinds:
            await asyncio.sleep(0)
        pending = recorder.stop()
        assert pending is not None
        await pending
        return pending

    finished = asyncio.run(exercise())

    assert rec.EVENT_GATEWAY_STOP in kinds
    assert finished.done()
    # Idempotent, and a second call has nothing left to hand back.
    assert recorder.stop() is None
    rec._recorder = None


def test_startup_prune_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _make(tmp_path)
    loop_thread = threading.get_ident()
    prune_threads: list[int] = []

    def record_prune() -> int:
        prune_threads.append(threading.get_ident())
        return 0

    def finish_warmup() -> dict:
        recorder._running = False
        return {}

    monkeypatch.setattr(recorder, "_prune", record_prune)
    monkeypatch.setattr(rec, "_read_posture", finish_warmup)
    recorder._running = True

    asyncio.run(recorder._sampler_loop())

    assert len(prune_threads) == 1
    assert prune_threads[0] != loop_thread


def test_the_sample_interval_comes_from_the_environment(tmp_path: Path) -> None:
    assert _make(tmp_path, env={rec.ENV_SAMPLE_SECS: "5"}).health()["interval"] == 5.0


def test_a_malformed_interval_falls_back_to_the_default(tmp_path: Path) -> None:
    assert _make(tmp_path, env={rec.ENV_SAMPLE_SECS: "banana"}).health()["interval"] == (
        rec.DEFAULT_SAMPLE_SECS
    )
