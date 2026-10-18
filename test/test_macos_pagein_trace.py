"""Tests for :mod:`kiro_crew.diag.macos_pagein_trace` -- the standalone sampler.

Every kernel read is injected. The four ``platform_compat`` readers the sampler
calls are monkeypatched, the output path is a ``tmp_path`` file, the sleep is a
no-op, and the signal registration is captured rather than installed. So the
assertions are about the sampler's own logic -- what it writes, that a failed
read becomes a ``None`` field and not a crash, that a write failure does not kill
the loop, and that ``main`` gates on macOS -- and not about the machine running
the suite, which is often not macOS.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.diag import macos_pagein_trace as mpt
from kiro_crew.testing.clock import ManualClock

# -- platform_compat injection ------------------------------------------------


@pytest.fixture()
def fake_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the four kernel readers to fixed values, so a sample is a fact."""
    monkeypatch.setattr(platform_compat, "memory_pressure_level", lambda: 4)
    monkeypatch.setattr(platform_compat, "proc_pageins_for_pid", lambda pid: 123)
    monkeypatch.setattr(platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: 456_000)
    monkeypatch.setattr(platform_compat, "memory_pressure_name", lambda level: "warn")


# -- default_output_path ------------------------------------------------------


def test_default_output_path_honours_kirocrew_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIROCREW_HOME", "/data/kc")
    assert mpt.default_output_path() == Path("/data/kc") / "logs" / "pagein-trace.jsonl"


def test_default_output_path_falls_back_to_dot_kiro_crew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(mpt.Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert (
        mpt.default_output_path()
        == Path("/home/u") / ".kiro" / "crew" / "logs" / "pagein-trace.jsonl"
    )


def test_default_output_path_ignores_blank_kirocrew_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", "   ")
    monkeypatch.setattr(mpt.Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert (
        mpt.default_output_path()
        == Path("/home/u") / ".kiro" / "crew" / "logs" / "pagein-trace.jsonl"
    )


# -- sample -------------------------------------------------------------------


def test_sample_builds_record_from_the_readers(fake_readers: None) -> None:
    rec = mpt.sample(777)
    assert rec["pid"] == 777
    assert rec["ri_pageins"] == 123
    assert rec["phys_footprint_bytes"] == 456_000
    assert rec["pressure_level"] == 4
    assert rec["pressure_name"] == "warn"
    assert isinstance(rec["ts"], str) and rec["ts"].endswith("+00:00")
    assert isinstance(rec["mono"], float)


def test_sample_threads_the_real_level_into_the_name_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard against the name lookup being fed the wrong value (e.g. the pid).
    monkeypatch.setattr(platform_compat, "memory_pressure_level", lambda: 2)
    monkeypatch.setattr(platform_compat, "proc_pageins_for_pid", lambda pid: 0)
    monkeypatch.setattr(platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: 0)
    seen: list[int] = []

    def _name(level: int) -> str:
        seen.append(level)
        return "normal"

    monkeypatch.setattr(platform_compat, "memory_pressure_name", _name)
    rec = mpt.sample(999)
    assert seen == [2]
    assert rec["pressure_name"] == "normal"


def test_sample_keeps_none_when_a_read_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_compat, "memory_pressure_level", lambda: None)
    monkeypatch.setattr(platform_compat, "proc_pageins_for_pid", lambda pid: None)
    monkeypatch.setattr(platform_compat, "proc_phys_footprint_bytes_for_pid", lambda pid: None)
    monkeypatch.setattr(platform_compat, "memory_pressure_name", lambda level: None)
    rec = mpt.sample(1)
    assert rec["ri_pageins"] is None
    assert rec["phys_footprint_bytes"] is None
    assert rec["pressure_level"] is None
    assert rec["pressure_name"] is None


# -- _append_jsonl ------------------------------------------------------------


def test_append_jsonl_writes_one_line_and_makes_the_parent(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "dir" / "trace.jsonl"
    mpt._append_jsonl(out, {"a": 1, "b": "x"})
    assert out.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"a": 1, "b": "x"}


def test_append_jsonl_appends_rather_than_truncates(tmp_path: Path) -> None:
    out = tmp_path / "trace.jsonl"
    mpt._append_jsonl(out, {"n": 1})
    mpt._append_jsonl(out, {"n": 2})
    lines = out.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


# -- run ----------------------------------------------------------------------


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    ManualClock().install(monkeypatch, mpt)


@pytest.fixture()
def captured_signals(monkeypatch: pytest.MonkeyPatch) -> dict[int, object]:
    """Capture the sampler's SIGINT/SIGTERM handlers instead of installing them."""
    handlers: dict[int, object] = {}

    def _capture(signum: int, handler: object) -> object:
        handlers[signum] = handler
        return None

    monkeypatch.setattr(mpt.signal, "signal", _capture)
    return handlers


def test_run_takes_count_samples_then_returns_zero(
    tmp_path: Path, no_sleep: None, captured_signals: dict[int, object], fake_readers: None
) -> None:
    out = tmp_path / "trace.jsonl"
    rc = mpt.run(pid=42, interval=0.01, count=3, out=out)
    assert rc == 0
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["pid"] == 42


def test_run_survives_a_write_failure(
    tmp_path: Path,
    no_sleep: None,
    captured_signals: dict[int, object],
    fake_readers: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _boom(_path: Path, _record: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mpt, "_append_jsonl", _boom)
    rc = mpt.run(pid=1, interval=0.01, count=2, out=tmp_path / "t.jsonl")
    assert rc == 0
    assert "write failed" in capsys.readouterr().err


def test_run_stops_promptly_on_a_signal(
    tmp_path: Path,
    captured_signals: dict[int, object],
    fake_readers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "trace.jsonl"

    # The interruptible sleep fires the captured SIGTERM handler after the first
    # sample, so the run should stop even though count is unbounded. The call
    # counter is a safety net: if a regression dropped the outer-loop stop check,
    # the loop would never terminate, so we raise rather than hang a worker.
    import signal as _signal

    calls = {"n": 0}

    def _sleep_then_signal(_s: float) -> None:
        calls["n"] += 1
        if calls["n"] > 3:
            raise AssertionError("run did not stop after the signal")
        handler = captured_signals[_signal.SIGTERM]
        handler(_signal.SIGTERM, None)  # type: ignore[operator]

    # Rebind the sampler's own ``time`` name to a stand-in rather than mutating
    # the shared stdlib module: a clocked ``monotonic`` for the sample record and
    # a non-blocking ``sleep`` that fires the captured SIGTERM after sample one.
    clock = ManualClock()
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic, sleep=_sleep_then_signal)
    monkeypatch.setattr(mpt, "time", fake_time)
    rc = mpt.run(pid=5, interval=10.0, count=None, out=out)
    assert rc == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1


# -- main ---------------------------------------------------------------------


def test_main_refuses_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_MACOS", False)
    assert mpt.main([]) == 2


def test_main_rejects_a_nonpositive_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    assert mpt.main(["--interval", "0"]) == 2


def test_main_wires_defaults_into_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    monkeypatch.setattr(mpt, "default_output_path", lambda: Path("/tmp/out.jsonl"))
    seen: dict[str, object] = {}

    def _fake_run(pid: int, interval: float, count: object, out: Path) -> int:
        seen.update(pid=pid, interval=interval, count=count, out=out)
        return 0

    monkeypatch.setattr(mpt, "run", _fake_run)
    assert mpt.main([]) == 0
    assert seen["pid"] == os.getpid()
    assert seen["interval"] == 5.0
    assert seen["count"] is None
    assert seen["out"] == Path("/tmp/out.jsonl")


def test_main_passes_explicit_arguments_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_MACOS", True)
    seen: dict[str, object] = {}

    def _fake_run(pid: int, interval: float, count: object, out: Path) -> int:
        seen.update(pid=pid, interval=interval, count=count, out=out)
        return 0

    monkeypatch.setattr(mpt, "run", _fake_run)
    rc = mpt.main(["--pid", "6584", "--interval", "1.5", "--count", "600", "--out", "/x/y.jsonl"])
    assert rc == 0
    assert seen == {"pid": 6584, "interval": 1.5, "count": 600, "out": Path("/x/y.jsonl")}
