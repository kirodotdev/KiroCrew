"""Tests for :mod:`kiro_crew.diag.threads`.

The procfs parsers and the interpretation rule are pure functions and are tested
as such. The probe thread is exercised for real but only ever asserted on shape
and ordering, never on a latency value: this suite shares a host with other
agents' work, so "p95 was under 5 ms" would be a test of the machine's mood.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import perf_sampler
from kiro_crew.diag import threads as dt


@pytest.fixture(autouse=True)
def _reset_ledger_baseline():
    """Clear the cross-sample CPU baseline between tests.

    ``_prev_reading`` is module state ON PURPOSE — the recorder runs for the
    life of the gateway and each sample's deltas are measured against the
    previous one. That makes it shared between tests, so a test asserting "the
    first call has no delta" would otherwise pass or fail on the order the suite
    happened to run in.
    """
    with dt._prev_lock:
        dt._prev_reading.clear()
    yield
    with dt._prev_lock:
        dt._prev_reading.clear()


@pytest.fixture()
def probe():
    """Start the probe for one test and always stop it.

    Without the teardown a failing test leaks a daemon thread AND leaves its gc
    callback installed, which would make every later test in the session pay for
    it and would make gc assertions depend on test order.
    """
    dt.start_probe()
    try:
        yield dt._probe
    finally:
        dt.stop_probe()


# ── the contract worker C's routes depend on ─────────────────────────────────


def test_contract_names_exist_with_the_documented_signatures() -> None:
    """The names the debug routes call, so their 501 stubs can be dropped."""
    for name in (
        "start_probe",
        "stop_probe",
        "ledger_now",
        "window_stats",
        "sample",
        "list_dumps",
        "read_dump",
    ):
        assert callable(getattr(dt, name)), name

    sample_params = list(inspect.signature(dt.sample).parameters)
    assert sample_params[:3] == ["seconds", "hz", "deep"]
    assert list(inspect.signature(dt.read_dump).parameters) == ["name"]


def test_start_probe_is_idempotent_and_stop_removes_the_gc_callback() -> None:
    import gc

    before = len(gc.callbacks)
    dt.start_probe()
    dt.start_probe()  # second call must not add a second thread or hook
    try:
        assert dt._probe.is_running()
        assert len(gc.callbacks) == before + 1
    finally:
        dt.stop_probe()
    assert len(gc.callbacks) == before
    assert not dt._probe.is_running()


def test_stop_probe_without_start_is_harmless() -> None:
    dt.stop_probe()  # must not raise


# ── layer 1: the probe distribution ──────────────────────────────────────────


def test_the_probe_produces_a_distribution_with_the_documented_keys(probe) -> None:
    time.sleep(0.4)  # ~20 readings at a 20 ms sleep
    stats = dt.window_stats()["gil_wait"]
    for key in ("samples", "p50_ms", "p95_ms", "max_ms", "frac_over_5ms"):
        assert key in stats, key
    assert stats["samples"] > 0
    assert stats["p50_ms"] <= stats["p95_ms"] <= stats["max_ms"]
    assert 0.0 <= stats["frac_over_5ms"] <= 1.0


def test_consecutive_windows_are_disjoint(probe) -> None:
    """A window must cover only what happened since the last one was read.

    Overlapping windows would double-count a single stall across two recorder
    rows, so a reader diffing the series would see contention persist after it
    had ended.
    """
    time.sleep(0.3)
    first = dt.window_stats()["gil_wait"]
    second = dt.window_stats()["gil_wait"]
    assert first["samples"] > 0
    assert second["samples"] < first["samples"]


def test_a_reading_at_the_window_boundary_is_counted_once() -> None:
    """Consecutive windows are disjoint, and the lower bound is exclusive.

    Driven on the real clock deliberately. Scripting ``time.time`` would replace
    it for the whole interpreter, and this probe is not the only caller: the
    background threads alive during a full suite run would draw from the script
    too, which both perturbs them and exhausts it.
    """
    probe = dt._Probe()
    now = time.time()
    probe._window_from = now - 10.0
    with probe._lock:
        probe._readings.append((now - 5.0, 7.0))

    first = probe.window()
    second = probe.window()

    assert first["samples"] == 1
    assert first["max_ms"] == 7.0
    # The advance excludes the reading the first window already counted.
    assert second["samples"] == 0

    # A reading landing exactly on a boundary belongs to the window that closed
    # on it, so the next window must not count it a second time.
    with probe._lock:
        probe._readings.append((probe._window_from, 9.0))
    assert probe.window()["samples"] == 0


def test_a_window_longer_than_the_ring_reports_only_the_span_it_covers() -> None:
    """``window_secs`` describes the readings, not the request.

    The ring holds a fixed number of readings, so a long configured interval
    outruns it. Reporting the requested span over a fraction of it would attach a
    percentile to coverage the probe never had, which is the one number a reader
    uses to decide how much the percentile is worth.
    """
    probe = dt._Probe()
    now = time.time()
    probe._window_from = now - 3600.0  # an hour was asked for
    with probe._lock:
        # Filled to capacity, spanning about five seconds: the arrangement an
        # hourly interval produces, where eviction has already discarded most of
        # the hour before anyone asks for it.
        span = 5.0
        count = probe._readings.maxlen or 1
        for i in range(count):
            probe._readings.append((now - span + (span * i / count), 4.0))

    stats = probe.window()

    assert stats["samples"] == count
    assert stats["window_truncated"] is True
    assert 4.0 <= stats["window_secs"] <= 6.0, stats["window_secs"]


def test_a_window_inside_the_ring_reports_the_full_span() -> None:
    """The flag stays off when the readings do reach the boundary."""
    probe = dt._Probe()
    now = time.time()
    probe._window_from = now - 30.0
    with probe._lock:
        probe._readings.append((now - 29.0, 4.0))

    stats = probe.window()

    assert stats["window_truncated"] is False
    assert 29.0 <= stats["window_secs"] <= 31.0, stats["window_secs"]


def test_an_empty_window_reports_none_not_zero() -> None:
    """Nothing measured must not read as nothing happening.

    ``p95_ms: 0.0`` says "no contention"; ``p95_ms: null`` says "the probe was
    not running". A reader acts differently on each, so they cannot share a
    value.
    """
    stats = dt._distribution([])
    assert stats["samples"] == 0
    assert stats["p50_ms"] is None
    assert stats["max_ms"] is None
    assert stats["frac_over_5ms"] is None


def test_the_distribution_percentiles_and_threshold_fraction_are_exact() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    stats = dt._distribution(values)
    assert stats["samples"] == 5
    assert stats["max_ms"] == 100.0
    assert stats["p50_ms"] == 3.0
    # One of five readings is above the 5 ms switch interval.
    assert stats["frac_over_5ms"] == pytest.approx(0.2)


def test_gil_runtime_reports_the_interpreter_facts() -> None:
    runtime = dt.gil_runtime()
    assert runtime["gil_enabled"] in (True, False, None)
    assert runtime["switch_interval_ms"] == pytest.approx(sys.getswitchinterval() * 1000.0)
    assert runtime["threshold_ms"] == dt.SWITCH_THRESHOLD_MS


# ── layer 2: the procfs parsers ──────────────────────────────────────────────


def test_stat_is_parsed_when_the_thread_name_contains_spaces_and_parentheses() -> None:
    """The comm field is not whitespace-delimited.

    Python lets a thread be named anything, so a whitespace split misreads the
    state and both CPU columns for a thread called ``worker (pool 2)`` — and the
    wrong columns still parse as integers, so the failure is silent and the
    reported CPU belongs to another field entirely.
    """
    # Fields: pid comm state ppid pgrp session tty tpgid flags minflt cminflt
    # majflt cmajflt utime stime ...
    text = "4242 (worker (pool 2)) R 1 1 1 0 -1 0 0 0 0 0 " "700 300 0 0 20 0 5 0 900 0 0\n"
    parsed = dt._parse_stat(text)
    assert parsed["state"] == "R"
    assert parsed["cpu_ticks"] == 1000  # utime 700 + stime 300


def test_a_malformed_stat_line_yields_no_fields_rather_than_wrong_ones() -> None:
    assert dt._parse_stat("nonsense with no parens") == {}
    assert "cpu_ticks" not in dt._parse_stat("1 (x) R")


def test_schedstat_gives_run_time_and_run_queue_wait() -> None:
    parsed = dt._parse_schedstat("123456789 987654321 42\n")
    assert parsed == {
        "run_ns": 123456789,
        "runqueue_wait_ns": 987654321,
        "timeslices": 42,
    }


def test_a_short_schedstat_is_empty_not_partial() -> None:
    assert dt._parse_schedstat("123\n") == {}


def test_status_gives_both_context_switch_counters() -> None:
    text = (
        "Name:\tworker\n"
        "State:\tS (sleeping)\n"
        "voluntary_ctxt_switches:\t1234\n"
        "nonvoluntary_ctxt_switches:\t56\n"
    )
    parsed = dt._parse_status(text)
    assert parsed["voluntary_ctxt_switches"] == 1234
    # Renamed on the way out: "nonvoluntary" is the kernel's spelling, and
    # "involuntary" is what a reader of this output expects.
    assert parsed["involuntary_ctxt_switches"] == 56


def test_status_without_the_counters_is_empty() -> None:
    assert dt._parse_status("Name:\tworker\n") == {}


# ── layer 2: the ledger ──────────────────────────────────────────────────────


def test_the_ledger_lists_every_live_thread_with_its_top_frame(probe) -> None:
    started = threading.Event()
    release = threading.Event()

    def park() -> None:
        started.set()
        release.wait(5.0)

    worker = threading.Thread(target=park, name="diag-test-worker", daemon=True)
    worker.start()
    assert started.wait(5.0)
    try:
        ledger = dt.ledger_now()
        names = {t["name"] for t in ledger["threads"]}
        assert "diag-test-worker" in names
        assert "MainThread" in names
        entry = next(t for t in ledger["threads"] if t["name"] == "diag-test-worker")
        assert entry["top_frame"] is not None
        assert entry["ident"] == worker.ident
        assert ledger["thread_count"] == len(ledger["threads"])
    finally:
        release.set()
        worker.join(5.0)


def test_the_ledger_carries_its_caveat_and_an_interpretation(probe) -> None:
    ledger = dt.ledger_now()
    assert "no GIL owner" in ledger["caveat"]
    assert isinstance(ledger["interpretation"], str)
    assert "runtime" in ledger and "gil_wait" in ledger and "gc" in ledger


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is Linux-only")
def test_the_second_ledger_call_reports_kernel_deltas(probe) -> None:
    """Deltas need a baseline, so the first call has none and says so.

    A cumulative counter answers "since boot", which is never the question; the
    delta since the previous sample is. Reporting the cumulative value as a
    delta would make an idle thread look permanently busy.
    """
    first = dt.ledger_now()
    main_first = next(t for t in first["threads"] if t["name"] == "MainThread")
    assert main_first["cpu_delta_ms"] is None

    time.sleep(0.1)
    second = dt.ledger_now()
    main_second = next(t for t in second["threads"] if t["name"] == "MainThread")
    assert main_second["cpu_delta_ms"] is not None
    assert main_second["runqueue_wait_delta_ms"] is not None
    assert main_second["voluntary_ctxt_switches"] is not None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is Linux-only")
def test_the_ledger_reports_procfs_as_available_on_linux(probe) -> None:
    assert dt.ledger_now()["procfs_available"] is True


def test_a_platform_without_procfs_gets_nulls_not_guesses(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS and Windows keep the Python columns and null the kernel ones."""
    monkeypatch.setattr(dt.platform_compat, "IS_LINUX", False)
    ledger = dt.ledger_now()
    assert ledger["procfs_available"] is False
    assert ledger["runqueue_wait_pct"] is None
    assert ledger["interpretation"] == "not measured"
    for entry in ledger["threads"]:
        assert entry["state"] is None
        assert entry["cpu_delta_ms"] is None
        assert entry["voluntary_ctxt_switches"] is None
        assert entry["name"]  # the Python-level columns survive


# ── layer 2: the interpretation rule ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("gil_p95", "runqueue_pct", "expected"),
    [
        (None, 5.0, "not measured"),
        (10.0, None, "not measured"),
        (0.5, 1.0, "no contention signal"),
        (20.0, 1.0, "GIL contention"),
        (0.5, 50.0, "CPU contention"),
        (20.0, 50.0, "CPU contention"),
    ],
)
def test_the_interpretation_separates_gil_wait_from_cpu_contention(
    gil_p95: float | None, runqueue_pct: float | None, expected: str
) -> None:
    """The run-queue wait is what decides between the two explanations.

    Both look like "work is slower than it should be" and they have opposite
    remedies — run fewer processes, or run less Python on the hot path. A high
    GIL wait *with* a high run-queue wait is the host being oversubscribed, so
    that case must not be reported as a GIL problem.
    """
    assert expected in dt._interpret(gil_p95, runqueue_pct)


# ── redaction ────────────────────────────────────────────────────────────────


def test_a_frame_label_drops_the_absolute_path_prefix() -> None:
    """The home-directory prefix carries the operator's username.

    This output leaves the process — it goes into a snapshot row and over an
    HTTP route — so the frame keeps enough to navigate by and nothing that
    identifies the machine's owner.
    """
    label = dt._redact_frame("/home/someuser/oss/kirocrew/src/kiro_crew/thing.py", 42, "run")
    assert "someuser" not in label
    assert "/home/" not in label
    assert "thing.py" in label and "42" in label and "run" in label


def test_a_frame_label_with_spaces_drops_the_absolute_path_prefix() -> None:
    label = dt._redact_frame("/home/Jane Doe/oss/kirocrew/src/kiro_crew/thing.py", 42, "run")
    assert "Jane Doe" not in label
    assert "/home/" not in label
    assert "kiro_crew/thing.py" in label
    assert "42" in label and "run" in label


def test_the_ledger_top_frames_carry_no_home_prefix(probe) -> None:
    for entry in dt.ledger_now()["threads"]:
        frame = entry["top_frame"]
        if frame:
            assert "/home/" not in frame
            assert not frame.startswith("/")


def test_the_main_thread_stack_is_redacted_and_depth_bounded() -> None:
    captured = dt.main_thread_stack(max_depth=2)
    assert len(captured["main_thread_stack"]) <= 2
    assert captured["stack_truncated"] is True
    for frame in captured["main_thread_stack"]:
        assert "/home/" not in frame


# ── layer 3: the stall capture ───────────────────────────────────────────────


def _beat_steadily(seconds: float, gap: float = 0.02) -> None:
    """Beat like a healthy loop for *seconds*, never leaving a gap worth a stall."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        dt.beat()
        time.sleep(gap)


def test_a_heartbeat_drift_past_the_threshold_emits_one_loop_stall_event() -> None:
    """One capture per episode, not one per probe tick.

    The probe wakes ~50 times a second, so a 0.3 s stall crosses the threshold on
    roughly fifteen consecutive ticks. Without the episode latch each of those
    would write an event row and bury the incident they were meant to record.
    """
    events: list[tuple[str, dict]] = []
    dt.start_probe(
        emit_event=lambda kind, payload: events.append((kind, payload)), stall_after=0.15
    )
    try:
        # Backdate the heartbeat so the probe sees a stall on its next tick.
        dt._probe._beat_at = time.monotonic() - 5.0
        time.sleep(0.3)
    finally:
        dt.stop_probe()

    stalls = [payload for kind, payload in events if kind == "loop_stall"]
    assert len(stalls) == 1
    assert stalls[0]["drift_ms"] > 150.0
    assert "main_thread_stack" in stalls[0]


def test_a_recovered_heartbeat_allows_a_later_episode_to_be_captured() -> None:
    """Recovery closes the episode, so a SECOND stall is news again.

    The latch must not be permanent: a gateway that stalled once an hour would
    otherwise report only the first one for the rest of its life.
    """
    events: list[str] = []
    # The threshold sits far above ``_beat_steadily``'s 20 ms gap on purpose. The
    # recovery phase drives a real probe thread, so a scheduling hiccup there
    # lengthens one gap; a threshold close to the gap turns that hiccup into a
    # third episode and the count assertion fails on a busy machine. The
    # deliberate stall is unaffected by the wider threshold, because backdating
    # the beat puts drift seconds past it rather than milliseconds.
    dt.start_probe(emit_event=lambda kind, _p: events.append(kind), stall_after=0.5)
    try:
        for _ in range(2):
            dt._probe._beat_at = time.monotonic() - 5.0
            time.sleep(0.3)
            _beat_steadily(0.3)  # a healthy loop again: closes the episode
    finally:
        dt.stop_probe()
    assert events.count("loop_stall") == 2


def test_a_probe_restarted_mid_stall_still_reports_its_first_stall() -> None:
    """A restart must not inherit the previous run's open episode.

    The probe is a module singleton, so its episode latch has to be cleared when
    it starts. A latch left closed by a stop that happens DURING a stall makes
    the next run read its own first real stall as a repeat of one that ended in an
    earlier process lifetime, and drop it — and that is the stall a restart most
    likely exists to investigate.
    """
    first: list[str] = []
    dt.start_probe(emit_event=lambda kind, _p: first.append(kind), stall_after=0.15)
    try:
        dt._probe._beat_at = time.monotonic() - 5.0
        time.sleep(0.3)  # stall fires; the probe is stopped without recovering
    finally:
        dt.stop_probe()
    assert first.count("loop_stall") == 1

    second: list[str] = []
    dt.start_probe(emit_event=lambda kind, _p: second.append(kind), stall_after=0.15)
    try:
        dt._probe._beat_at = time.monotonic() - 5.0
        time.sleep(0.3)
    finally:
        dt.stop_probe()
    assert second.count("loop_stall") == 1


def test_a_restarted_probe_resets_its_gc_counters(probe) -> None:
    """The gc totals describe this run, not every run since import."""
    import gc

    gc.collect()
    gc.collect()
    assert dt.window_stats()["gc"]["collections"] >= 0
    dt.stop_probe()
    dt.start_probe()
    assert dt.window_stats()["gc"]["collections"] == 0


@pytest.mark.parametrize("seconds", [float("nan"), float("inf")])
def test_a_non_finite_sample_duration_starts_no_sampler(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> None:
    """The guards above it compare, and nan answers false to every comparison.

    Left alone it reaches ``time.sleep``, which raises only AFTER the sampler
    thread is running, so each such request would leave a daemon behind.
    """
    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
    before = threading.active_count()

    with pytest.raises(ValueError) as caught:
        dt.sample(seconds, 50)

    assert "seconds" in str(caught.value)
    assert threading.active_count() == before


def test_the_diag_directory_is_masked_and_fenced() -> None:
    """The read routes redact; the files do not, so the files are unreachable.

    Both lists are crew-home-relative leaves, so nothing outside the data home is
    affected. Pinned here because the reason belongs to this feature: a sandboxed
    session that could read the rows directly would collect the frame labels and
    folded stacks the owner-gated routes exist to scrub.
    """
    from kiro_crew import sandbox
    from kiro_crew.security import paths as secure_paths

    assert "diag" in sandbox._CREW_HIDDEN_LEAVES
    assert "diag" in secure_paths._CREW_SECRET_LEAVES


def test_two_deep_samples_stage_to_different_private_paths(monkeypatch, tmp_path) -> None:
    """The staging path is unguessable and created 0700, so it cannot be pre-planted.

    A predictable name in a shared temp directory let anyone who guessed the pid
    leave a symlink for the operator's privileged capture to follow.
    """
    import stat as stat_mod

    from kiro_crew.config import paths as cfg_paths

    monkeypatch.setattr(cfg_paths, "config_dir", lambda: tmp_path)
    first = dt._pyspy_output_path()
    second = dt._pyspy_output_path()

    assert first != second
    assert first.parent != second.parent
    assert tmp_path in first.parents
    for path in (first, second):
        assert not path.exists(), "the artifact itself is the operator's to create"
        if sys.platform != "win32":
            # Windows carries no POSIX permission bits and reports 0o777 for every
            # directory; there the private-directory guarantee rests on the
            # user-profile ACLs, and the randomised name is what this test pins.
            mode = stat_mod.S_IMODE(path.parent.stat().st_mode)
            assert mode == 0o700, oct(mode)


def test_a_symlink_left_at_a_staging_name_cannot_be_followed(monkeypatch, tmp_path) -> None:
    """Nothing can sit at the path already: ``mkdtemp`` creates what it names."""
    from kiro_crew.config import paths as cfg_paths

    monkeypatch.setattr(cfg_paths, "config_dir", lambda: tmp_path)
    got = dt._pyspy_output_path()
    assert got.parent.is_dir()
    assert not got.parent.is_symlink()


def test_stale_gil_staging_directories_are_reaped(monkeypatch, tmp_path) -> None:
    """A command that is never run still leaves a directory, so the next call reaps."""
    import os as _os

    from kiro_crew.config import paths as cfg_paths

    monkeypatch.setattr(cfg_paths, "config_dir", lambda: tmp_path)
    old = dt._pyspy_output_path().parent
    ancient = 1_600_000_000.0
    _os.utime(old, (ancient, ancient))

    fresh = dt._pyspy_output_path().parent

    assert not old.exists(), "a stale staging directory survived"
    assert fresh.is_dir()


def test_a_healthy_heartbeat_emits_nothing() -> None:
    events: list[str] = []
    dt.start_probe(emit_event=lambda kind, _p: events.append(kind), stall_after=0.5)
    try:
        _beat_steadily(0.3)
    finally:
        dt.stop_probe()
    assert events == []


# ── layer 3 read-back: the dump guards ───────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../../../etc/passwd",
        "loopstall-../escape.txt",
        "/etc/passwd",
        "notadump.txt",
        "loopstall-x.log",
        "config.json",
    ],
)
def test_read_dump_refuses_a_name_that_is_not_a_dump_filename(name: str) -> None:
    """The name arrives from an HTTP route.

    A reader that joined it to the dump directory unchecked would read any file
    the gateway can, which on this data home includes the vault and ``.env``.
    """
    with pytest.raises(ValueError):
        dt.read_dump(name)


def test_read_dump_scrubs_paths_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``faulthandler`` writes these files from C and cannot redact as it writes.

    Read time is therefore the only point at which redaction can happen, which is
    why the dumps stay in the fenced directory and only come out through here.
    """
    from kiro_crew.dashboard import crash_dump_store

    dumps = tmp_path / "crash-dumps"
    dumps.mkdir(parents=True)
    dump = dumps / f"{crash_dump_store.DUMP_PREFIX}20231114T101500Z{crash_dump_store.DUMP_SUFFIX}"
    dump.write_text(
        "# loop-stall crash dump — opened 20231114T101500Z\n"
        "# PID: 4242 @ testhost start=999\n"
        "# If thread stacks appear below, the event loop wedged.\n"
        "\n"
        "Current thread 0x00007f0000000000 (most recent call first):\n"
        '  File "/home/someuser/oss/kirocrew/src/kiro_crew/dashboard/server.py", '
        "line 5087 in _start\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(crash_dump_store, "get_dumps_dir", lambda: dumps)

    text = dt.read_dump(dump.name)
    assert "someuser" not in text
    assert "/home/" not in text
    assert "server.py" in text  # still navigable


def test_list_dumps_labels_each_dump_with_its_attributed_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import crash_dump_store

    dumps = tmp_path / "crash-dumps"
    dumps.mkdir(parents=True)
    dump = dumps / f"{crash_dump_store.DUMP_PREFIX}20231114T101500Z{crash_dump_store.DUMP_SUFFIX}"
    dump.write_text(
        "# loop-stall crash dump — opened 20231114T101500Z\n"
        "# PID: 4242 @ testhost start=999\n"
        "# header line three\n"
        "\n"
        "Current thread 0x00007f0000000000 (most recent call first):\n"
        '  File "/opt/kirocrew/src/kiro_crew/cron.py", line 10 in _cron_callback\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(crash_dump_store, "get_dumps_dir", lambda: dumps)

    listed = dt.list_dumps()
    assert len(listed) == 1
    assert listed[0]["name"] == dump.name
    assert listed[0]["has_stacks"] is True
    assert listed[0]["surface"] == "cron"
    assert listed[0]["owner_pid"] == 4242


def test_list_dumps_is_empty_when_there_are_no_dumps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.dashboard import crash_dump_store

    empty = tmp_path / "crash-dumps"
    empty.mkdir(parents=True)
    monkeypatch.setattr(crash_dump_store, "get_dumps_dir", lambda: empty)
    assert dt.list_dumps() == []


# ── layer 4: the sampling gates ──────────────────────────────────────────────


def test_sampling_is_refused_when_the_debug_gate_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(perf_sampler.DEBUG_ENV_VAR, raising=False)
    out = dt.sample(0.05, 50)
    assert perf_sampler.DEBUG_ENV_VAR in out["refused"]


def test_sampling_is_refused_on_an_event_loop_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 60-second in-line profile would wedge the loop it exists to explain.

    The gateway runs the dashboard, every agent turn and all background work on
    one loop, and the watchdog would dump-then-exit long before the profile
    returned — so the refusal is the safe answer and the route must use a worker
    thread.
    """
    import asyncio

    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")

    async def attempt() -> dict:
        return dt.sample(0.05, 50)

    out = asyncio.run(attempt())
    assert "event loop" in out["refused"]


def test_sampling_is_refused_while_another_run_holds_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
    assert dt._sample_lock.acquire(blocking=False)
    try:
        assert "already in progress" in dt.sample(0.05, 50)["refused"]
    finally:
        dt._sample_lock.release()


@pytest.mark.parametrize(("seconds", "hz"), [(0, 50), (-1, 50), (1, 0), (1, -5)])
def test_sampling_rejects_non_positive_arguments(
    monkeypatch: pytest.MonkeyPatch, seconds: float, hz: int
) -> None:
    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
    assert "positive" in dt.sample(seconds, hz)["refused"]


def test_a_sampling_run_returns_folded_stacks_and_per_thread_shares(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
    out = dt.sample(0.2, 50)
    assert "refused" not in out
    assert out["effective"]["samples"] > 0
    assert out["folded"]
    assert out["per_thread"]
    for entry in out["per_thread"]:
        assert 0.0 <= entry["runnable_in_python_frac"] <= 1.0
    assert "not which held the GIL" in out["caveat"]
    assert "gil_wait_during_window" in out


def test_a_sampling_request_longer_than_the_cap_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clamp is asserted on the recorded request, not by waiting 60 s."""
    monkeypatch.setenv(perf_sampler.DEBUG_ENV_VAR, "1")
    captured: dict[str, float] = {}
    caller = threading.get_ident()

    class _Clock:
        """Stands in for ``time`` inside the module under test, for one thread.

        Two things this is careful about. It replaces the module's OWN reference,
        so no other module sees it, and it skips the wait only on the thread
        running the sample, so a probe thread elsewhere keeps real timing instead
        of spinning through its loop for as long as this test holds the patch.
        """

        def sleep(self, duration: float) -> None:
            if threading.get_ident() != caller:
                time.sleep(duration)
                return
            captured["duration"] = duration

        def __getattr__(self, name: str) -> object:
            return getattr(time, name)

    monkeypatch.setattr(dt, "time", _Clock())
    dt.sample(9999.0, 50)
    assert captured["duration"] == dt.MAX_SAMPLE_SECONDS


# ── the perf_sampler additions this module depends on ─────────────────────────


def test_the_stack_sampler_reports_per_thread_sample_counts() -> None:
    """One pass, two answers: folded stacks and per-thread identity.

    ``counts`` folds every thread into one histogram, so a caller wanting
    per-thread shares would otherwise have to run a second sampler at double the
    cost and on a different cadence — and the two would disagree.
    """
    sampler = perf_sampler.StackSampler(interval=0.002)
    sampler.start()
    time.sleep(0.15)
    report = sampler.stop()

    assert report.samples > 0
    assert report.per_thread_samples
    assert threading.main_thread().ident in report.per_thread_samples
    assert sum(report.per_thread_samples.values()) >= report.samples


def test_pyspy_argv_adds_the_gil_flag_only_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(perf_sampler, "pyspy_path", lambda: "/usr/bin/py-spy")
    out = Path("/x/out.folded")
    plain = perf_sampler.pyspy_argv(123, 5, out, 50)
    deep = perf_sampler.pyspy_argv(123, 5, out, 50, gil=True)

    assert "--gil" not in plain
    assert "--gil" in deep
    # ``--output <path>`` must stay last, because the deep path reads argv[-1] to
    # name the artifact. Compared against the platform's own rendering of the
    # path: Windows renders it with backslashes, so a hardcoded POSIX string
    # would fail there for no reason connected to this behaviour.
    assert plain[-2:] == ["--output", str(out)]
    assert deep[-2:] == ["--output", str(out)]


def test_pyspy_argv_still_raises_when_py_spy_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(perf_sampler, "pyspy_path", lambda: None)
    with pytest.raises(FileNotFoundError):
        perf_sampler.pyspy_argv(123, 5, Path("/x/out.folded"), 50, gil=True)


def test_deep_sampling_reports_the_command_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway must not spawn a tracer of itself.

    ``pyspy_argv`` documents the split -- "returned rather than executed so the
    CLI owns spawning" -- and the spawn audit requires every subprocess in this
    package to be sandbox-routed or justified. Reporting the command satisfies
    both and still hands over the one measurement no in-process sampler can make.
    """
    monkeypatch.setattr(perf_sampler, "pyspy_path", lambda: "/usr/bin/py-spy")

    def refuse_to_run(*_a: object, **_k: object) -> None:
        raise AssertionError("deep sampling must not execute a subprocess")

    import subprocess

    monkeypatch.setattr(subprocess, "run", refuse_to_run)
    monkeypatch.setattr(subprocess, "Popen", refuse_to_run)

    out = dt._deep_sample(5.0, 50)
    assert out["available"] is True
    assert out["executed"] is False
    assert "--gil" in out["argv"]
    assert out["argv"][0] == "/usr/bin/py-spy"


def test_deep_sampling_reports_a_missing_py_spy_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No privilege is widened to make an attach succeed; the reason is reported."""
    monkeypatch.setattr(perf_sampler, "pyspy_path", lambda: None)
    out = dt._deep_sample(1.0, 50)
    assert out["available"] is False
    assert "py-spy" in out["reason"]


# ── the recorder-facing summary ───────────────────────────────────────────────


def test_window_stats_uses_one_distribution_for_wait_and_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = {
        "samples": 4,
        "p50_ms": 8.0,
        "p95_ms": 17.0,
        "max_ms": 20.0,
        "frac_over_5ms": 0.75,
        "window_secs": 60.0,
    }
    built_from: list[dict] = []

    def build_ledger(gil_wait: dict) -> dict:
        built_from.append(gil_wait)
        runqueue_wait_pct = 2.0
        return {
            "gil_wait": gil_wait,
            "runtime": {},
            "gc": {},
            "probe_running": True,
            "procfs_available": True,
            "thread_count": 0,
            "runqueue_wait_pct": runqueue_wait_pct,
            "interpretation": dt._interpret(gil_wait["p95_ms"], runqueue_wait_pct),
            "threads": [],
        }

    def recent_must_not_be_read(_seconds: float) -> dict:
        raise AssertionError("window_stats must not mix in the recent distribution")

    monkeypatch.setattr(dt._probe, "window", lambda: window)
    monkeypatch.setattr(dt._probe, "recent", recent_must_not_be_read)
    monkeypatch.setattr(dt, "_build_ledger", build_ledger)

    stats = dt.window_stats()

    assert built_from == [window]
    assert stats["gil_wait"] is window
    assert stats["interpretation"].startswith("GIL contention")


def test_window_stats_summarises_rather_than_listing_every_thread(probe) -> None:
    """The periodic row keeps totals and extremes, not the full ledger.

    A gateway carries 20-40 threads at ~200 bytes an entry, so a full ledger per
    sample would cost roughly 20 MB a day against the recorder's ~1.7 MB/day
    budget. The whole list stays one ``ledger_now()`` call away.
    """
    time.sleep(0.3)
    stats = dt.window_stats()
    assert "threads" not in stats  # no per-thread list on the periodic path
    assert isinstance(stats["thread_count"], int)
    assert isinstance(stats["thread_states"], dict)
    assert len(stats["top_cpu"]) <= dt.LEDGER_SUMMARY_TOP_N
    assert len(stats["top_runqueue_wait"]) <= dt.LEDGER_SUMMARY_TOP_N
    assert "gil_wait" in stats and "interpretation" in stats


def test_a_quiet_window_carries_no_per_thread_rows_but_keeps_the_keys(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured at ~2.5 KB a row (~7 MB/day) when the extremes were unconditional.

    A window with no contention signal has nothing worth ranking, so paying
    per-thread bytes for it buys nothing. The keys stay present and empty so a
    consumer reads the same shape on every row.
    """
    monkeypatch.setattr(dt, "_interpret", lambda *_a: "no contention signal")
    time.sleep(0.2)
    stats = dt.window_stats()
    assert stats["interpretation"] == "no contention signal"
    assert stats["top_cpu"] == []
    assert stats["top_runqueue_wait"] == []
    # The totals a quiet row still needs, to show that nothing was wrong.
    assert stats["thread_count"] > 0
    assert stats["gil_wait"]["samples"] > 0


def test_quiet_interpretation_does_not_trigger_rankings_by_substring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = {
        "samples": 1,
        "p50_ms": 0.1,
        "p95_ms": 0.2,
        "max_ms": 0.2,
        "frac_over_5ms": 0.0,
        "window_secs": 30.0,
    }
    thread = {
        "name": "quiet-worker",
        "native_id": 123,
        "state": "R",
        "top_frame": "kiro_crew/worker.py:1:run",
        "cpu_delta_ms": 10.0,
        "runqueue_wait_delta_ms": 2.0,
    }
    monkeypatch.setattr(dt._probe, "window", lambda: window)
    monkeypatch.setattr(
        dt,
        "_build_ledger",
        lambda _wait: {
            "gil_wait": window,
            "runtime": {},
            "gc": {},
            "probe_running": True,
            "procfs_available": True,
            "thread_count": 1,
            "runqueue_wait_pct": 1.0,
            "interpretation": "no contention signal",
            "threads": [thread],
        },
    )

    stats = dt.window_stats()

    assert stats["top_cpu"] == []
    assert stats["top_runqueue_wait"] == []


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="ranking needs the procfs CPU delta, which only Linux provides",
)
def test_a_contended_window_names_the_threads_involved(
    probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        dt,
        "_interpret",
        lambda *_a: "GIL contention: threads wait for the interpreter, not for a CPU",
    )
    dt.ledger_now()  # establish a baseline so the deltas are non-null
    # Real CPU, because the ranking drops threads with a zero delta and an idle
    # process accumulates none: the kernel counts CPU in clock ticks (10 ms here),
    # so a window that merely sleeps has nothing to rank and the assertion below
    # would pass or fail on how busy the host happened to be.
    deadline = time.monotonic() + 0.25
    total = 0
    while time.monotonic() < deadline:
        for _ in range(10000):
            total += 1
    assert total > 0

    stats = dt.window_stats()
    assert "contention" in stats["interpretation"]
    assert stats["top_cpu"], "a contended window must name the threads involved"
    for entry in stats["top_cpu"]:
        assert entry["name"]
        assert entry["cpu_delta_ms"] > 0.0
        assert entry["top_frame"] is None or "/home/" not in entry["top_frame"]


def test_window_stats_is_json_serialisable(probe) -> None:
    """It becomes one field of a JSON row, so a stray object breaks the series."""
    import json

    time.sleep(0.2)
    json.dumps(dt.window_stats())
