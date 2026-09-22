"""GIL and thread-state introspection — three corroborating signals, one exact.

CPython has no API that answers "which thread holds the GIL right now". There is
no counter for contention, no wait time, no owner. So a question like "are the
path gate's resolver workers GIL-bound, or is the disk slow?" cannot be answered
directly — it has to be triangulated. This module offers four layers and is
explicit in its output about which of them is evidence and which is inference:

1. :func:`window_stats` — a **probe thread** that measures how much longer than
   requested a 20 ms sleep takes. ``time.sleep`` releases the GIL, so the excess
   is the cost of getting it back plus scheduler slack. Always on, ~50 wakeups a
   second. An inference, but a direct one: the wait is real even though its
   cause is shared with CPU contention.
2. :func:`ledger_now` — a **per-thread ledger** joining ``threading.enumerate()``
   to ``/proc/self/task/<tid>/{stat,schedstat,status}`` and the thread's top
   Python frame. The kernel's run-queue-wait figure is what separates the two
   explanations: high run-queue wait means the host is oversubscribed, while low
   run-queue wait *with* a high GIL wait points at the GIL. The interpretation
   ships beside the numbers so a reader does not have to re-derive it.
3. The **stall capture**: when the loop's heartbeat drifts past
   :data:`DEFAULT_STALL_AFTER` seconds, the probe thread grabs the main thread's
   Python stack and hands a ``loop_stall`` event to the recorder. This is
   deliberately *not* a second loop watchdog —
   :mod:`kiro_crew.dashboard.loop_watchdog` already owns the 25-30 s
   dump-then-exit path with a C-level ``faulthandler`` timer. This fires at one
   second, survives recovery, and costs nothing but a frame read; the two do not
   overlap. :func:`list_dumps` and :func:`read_dump` read that watchdog's
   artifacts back, labelled by :mod:`kiro_crew.stall_attribution`.
4. :func:`sample` — the only **exact** layer. It reuses
   :class:`kiro_crew.perf_sampler.StackSampler` for folded stacks, and with
   ``deep=True`` it reuses :func:`kiro_crew.perf_sampler.pyspy_argv` to run
   py-spy with ``--gil``, which reports GIL-holding traces from outside the
   process. Gated behind ``KIROCREW_DEBUG`` and capped at
   :data:`MAX_SAMPLE_SECONDS`, because it is the only layer with a cost worth
   bounding.

Constraints this module holds to, because a diagnostic that hurts its host is
worse than no diagnostic:

* No ``psutil`` (not in the venv) and no subprocess on the periodic path — the
  same rule :mod:`kiro_crew.dashboard.stall_enrichment` follows, for the same
  reason: spawning from a sick process is riskier than reading procfs.
* No ``prctl(PR_SET_PTRACER)`` and no other privilege widening. If py-spy cannot
  attach, that is reported through
  :func:`kiro_crew.perf_sampler.pyspy_attach_failure_hint` rather than worked
  around.
* Nothing here raises into its caller. A field that cannot be read is ``None``
  — on macOS and Windows, where ``/proc/self/task`` does not exist, the kernel
  columns are ``None`` rather than a guess.
"""

from __future__ import annotations

import gc
import logging
import math
import os
import sys
import threading
import time
from collections import deque
from typing import Any, Callable

from kiro_crew import perf_sampler, platform_compat

logger = logging.getLogger(__name__)

#: How long the probe asks to sleep. Short enough that a 5 ms excess is a large
#: relative signal, long enough that the wakeup rate (~50/s) is negligible.
PROBE_SLEEP_SECONDS = 0.020

#: Excess above which one probe reading counts as "the GIL was not available
#: promptly". One default switch interval (``sys.getswitchinterval()`` is 5 ms),
#: so a reading over this means the thread waited longer than the interpreter's
#: own scheduling quantum.
SWITCH_THRESHOLD_MS = 5.0

#: Probe readings retained. At ~50/s this is ~80 s of history, comfortably more
#: than two recorder windows, so a window read is never short of data.
_PROBE_RING = 4096

#: Heartbeat drift that counts as a loop stall for layer 3. Far below the
#: watchdog's 25-30 s dump-then-exit, which is the point: a one-second stall is
#: invisible today and is exactly what a lag complaint is made of.
DEFAULT_STALL_AFTER = 1.0

#: Hard ceiling on :func:`sample`. Spec-mandated: an on-demand profile is the
#: one layer that costs real CPU, and an unbounded request from a route would
#: hold the single-run slot indefinitely.
MAX_SAMPLE_SECONDS = 60.0

#: Threads named in the compact per-sample summary, ranked by CPU delta and by
#: run-queue wait. The full ledger is what :func:`ledger_now` is for; a recorder
#: row carrying every thread would cost ~8 KB a sample (~20 MB/day against the
#: recorder's ~1.7 MB/day budget), so the periodic path keeps the extremes and
#: the totals.
LEDGER_SUMMARY_TOP_N = 8

#: ``/proc/self/task/<tid>/stat`` field numbers (1-based, as ``proc(5)`` numbers
#: them) for the two CPU columns. Resolved against the remainder after the comm
#: field, which is parenthesised and may itself contain spaces and parentheses.
_STAT_FIELD_UTIME = 14
_STAT_FIELD_STIME = 15
#: ``state`` is field 3, the first column after comm.
_STAT_FIELD_STATE = 3

_CLOCK_TICKS = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0


# ── Layer 1: the probe thread ────────────────────────────────────────────────


class _Probe:
    """Measures GIL re-acquire latency, and watches the loop heartbeat.

    One daemon thread does both duties because they share the same wakeup: the
    sleep that measures the wait is also the tick that notices the loop has gone
    quiet. A second thread would double the cost for no extra information.
    """

    def __init__(self) -> None:
        self._readings: deque[tuple[float, float]] = deque(maxlen=_PROBE_RING)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Wall clock of the last window boundary, so consecutive recorder
        #: windows are disjoint rather than overlapping.
        self._window_from = 0.0
        # Heartbeat state. ``_beat_at`` is a plain float store: the GIL makes the
        # write atomic between the loop thread and this one, which is the same
        # guarantee LoopStallWatchdog.beat relies on.
        self._beat_at = 0.0
        self._stall_after = DEFAULT_STALL_AFTER
        self._stall_open = False
        self._emit_event: Callable[[str, dict[str, Any]], None] | None = None
        # gc pause accounting, installed into gc.callbacks.
        self._gc_started_at = 0.0
        self._gc_collections = 0
        self._gc_total_ms = 0.0
        self._gc_max_ms = 0.0
        self._gc_installed = False

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        *,
        emit_event: Callable[[str, dict[str, Any]], None] | None = None,
        stall_after: float = DEFAULT_STALL_AFTER,
    ) -> None:
        if self._thread is not None:
            return
        self._emit_event = emit_event
        self._stall_after = max(0.1, float(stall_after))
        self._stop.clear()
        now = time.time()
        self._window_from = now
        self._beat_at = time.monotonic()
        # Reset the per-run observation state. The probe is a module singleton, so
        # a stop that happened DURING a stall would otherwise leave the episode
        # latch closed and the next run would treat its first real stall as a
        # repeat of one that ended in a previous process lifetime — dropping
        # exactly the event the restart was likely investigating. The gc counters
        # are reset for the same reason: they measure this run, and carrying a
        # previous run's totals forward would make the first window look busy.
        self._stall_open = False
        self._gc_started_at = 0.0
        self._gc_collections = 0
        self._gc_total_ms = 0.0
        self._gc_max_ms = 0.0
        self._install_gc_hook()
        self._thread = threading.Thread(
            target=self._run, name="kirocrew-diag-gil-probe", daemon=True
        )
        self._thread.start()
        logger.debug(
            "diag GIL probe started (sleep=%.0fms, stall_after=%.1fs)",
            PROBE_SLEEP_SECONDS * 1000,
            self._stall_after,
        )

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._remove_gc_hook()
        self._emit_event = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- gc pauses ---------------------------------------------------------

    def _install_gc_hook(self) -> None:
        if self._gc_installed:
            return
        gc.callbacks.append(self._on_gc)
        self._gc_installed = True

    def _remove_gc_hook(self) -> None:
        if not self._gc_installed:
            return
        try:
            gc.callbacks.remove(self._on_gc)
        except ValueError:  # pragma: no cover - already removed
            pass
        self._gc_installed = False

    def _on_gc(self, phase: str, _info: dict[str, Any]) -> None:
        """Accumulate collection count and pause duration.

        A collection is not reentrant and runs under the GIL, so the
        start/stop pair cannot interleave with another collection's pair — which
        is why plain attribute updates are sufficient here and a lock (which
        could itself be contended from a gc callback) is not used.
        """
        try:
            if phase == "start":
                self._gc_started_at = time.perf_counter()
                return
            if self._gc_started_at <= 0:
                return
            pause_ms = (time.perf_counter() - self._gc_started_at) * 1000.0
            self._gc_started_at = 0.0
            self._gc_collections += 1
            self._gc_total_ms += pause_ms
            self._gc_max_ms = max(self._gc_max_ms, pause_ms)
        except Exception:  # noqa: BLE001 - a gc callback must never raise
            pass

    def gc_stats(self) -> dict[str, Any]:
        return {
            "collections": self._gc_collections,
            "total_pause_ms": round(self._gc_total_ms, 3),
            "max_pause_ms": round(self._gc_max_ms, 3),
        }

    # -- heartbeat ---------------------------------------------------------

    def beat(self) -> None:
        """Record that the event loop is alive now (called from the loop)."""
        self._beat_at = time.monotonic()

    def _check_stall(self, now: float) -> None:
        beat_at = self._beat_at
        if beat_at <= 0:
            return
        drift = now - beat_at
        if drift < self._stall_after:
            self._stall_open = False
            return
        if self._stall_open:
            return  # one capture per episode, not one per probe tick
        self._stall_open = True
        emit = self._emit_event
        if emit is None:
            return
        try:
            emit("loop_stall", {"drift_ms": round(drift * 1000.0, 1), **main_thread_stack()})
        except Exception:  # noqa: BLE001 - diagnostics must not kill the probe
            logger.debug("diag loop_stall event emission failed", exc_info=True)

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        sleep = PROBE_SLEEP_SECONDS
        while not self._stop.is_set():
            t0 = time.perf_counter()
            time.sleep(sleep)
            excess_ms = (time.perf_counter() - t0 - sleep) * 1000.0
            # A negative excess is a clock artefact, not a negative wait; clamp
            # so it cannot drag a percentile below zero.
            reading = max(0.0, excess_ms)
            with self._lock:
                self._readings.append((time.time(), reading))
            try:
                self._check_stall(time.monotonic())
            except Exception:  # noqa: BLE001 - never let the watch kill the probe
                logger.debug("diag stall check raised", exc_info=True)

    # -- readings ----------------------------------------------------------

    def _since(self, start: float) -> list[float]:
        with self._lock:
            return [v for ts, v in self._readings if ts >= start]

    def window(self) -> dict[str, Any]:
        """Stats for readings since the previous call, then advance the window.

        ``window_secs`` is the span the readings actually cover, not the span that
        was asked for. The ring holds a fixed number of readings, so a long
        configured interval outruns it: at roughly fifty readings a second, 4096
        of them span about eighty seconds. Reporting the requested hour over
        eighty seconds of data would describe coverage this probe does not have,
        and a percentile is only as honest as the window attached to it.
        ``window_truncated`` says the ring did not reach back to the boundary.
        """
        with self._lock:
            now = time.time()
            window_from = self._window_from
            values = [v for ts, v in self._readings if window_from < ts <= now]
            oldest = self._readings[0][0] if self._readings else None
            ring_full = len(self._readings) == self._readings.maxlen
            self._window_from = now
        requested = max(0.0, now - window_from)
        # Truncated only when the ring is FULL and its oldest surviving reading is
        # newer than the boundary: that pair is what says in-window readings were
        # evicted. A ring below capacity has dropped nothing, and its oldest
        # reading being newer than the boundary just means measuring started then.
        truncated = ring_full and oldest is not None and oldest > window_from
        covered = requested
        if truncated and oldest is not None:
            covered = min(requested, max(0.0, now - oldest))
        stats = _distribution(values)
        stats["window_secs"] = round(covered, 3)
        stats["window_truncated"] = truncated
        return stats

    def recent(self, seconds: float) -> dict[str, Any]:
        """Stats for the last *seconds* of readings, without moving the window."""
        stats = _distribution(self._since(time.time() - max(0.0, seconds)))
        stats["window_secs"] = round(max(0.0, seconds), 3)
        return stats


def _distribution(values: list[float]) -> dict[str, Any]:
    """Percentiles of GIL wait readings, with the over-threshold fraction.

    ``None`` percentiles when there are no readings — an empty window is "not
    measured", and reporting 0.0 would read as "no contention", which is the
    opposite conclusion.
    """
    count = len(values)
    if count == 0:
        return {
            "samples": 0,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "frac_over_5ms": None,
        }
    ordered = sorted(values)

    def pct(fraction: float) -> float:
        # Nearest-rank on a small sample: interpolation would invent a value
        # between two real measurements, and these are latencies, not a curve.
        idx = min(count - 1, max(0, int(round(fraction * (count - 1)))))
        return round(ordered[idx], 3)

    over = sum(1 for v in ordered if v > SWITCH_THRESHOLD_MS)
    return {
        "samples": count,
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "max_ms": round(ordered[-1], 3),
        "frac_over_5ms": round(over / count, 4),
    }


_probe = _Probe()


def start_probe(
    *,
    emit_event: Callable[[str, dict[str, Any]], None] | None = None,
    stall_after: float = DEFAULT_STALL_AFTER,
) -> None:
    """Start the probe thread (idempotent).

    *emit_event* is the recorder's :meth:`~kiro_crew.diag.recorder.Recorder.emit_event`.
    Passed in rather than imported so this module never depends on the recorder
    — the dependency runs the other way, and a cycle would break the boot path.
    """
    _probe.start(emit_event=emit_event, stall_after=stall_after)


def stop_probe() -> None:
    """Stop the probe thread and remove the gc hook (idempotent)."""
    _probe.stop()


def beat() -> None:
    """Mark the event loop alive. Called from the recorder's heartbeat tick."""
    _probe.beat()


def gil_runtime() -> dict[str, Any]:
    """What the interpreter itself says about the GIL.

    ``sys._is_gil_enabled`` exists from CPython 3.13 (free-threaded builds); below
    that version the GIL is unconditional, so ``True`` is a fact rather than
    a guess. ``switch_interval`` is the quantum :data:`SWITCH_THRESHOLD_MS` is
    compared against, reported so a tuned interpreter's readings stay readable.
    """
    probe = getattr(sys, "_is_gil_enabled", None)
    enabled: bool | None = True
    if callable(probe):
        try:
            enabled = bool(probe())
        except Exception:  # noqa: BLE001 - introspection must not raise out
            enabled = None
    return {
        "gil_enabled": enabled,
        "switch_interval_ms": round(sys.getswitchinterval() * 1000.0, 3),
        "threshold_ms": SWITCH_THRESHOLD_MS,
    }


# ── Layer 2: the per-thread ledger ───────────────────────────────────────────


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _parse_stat(text: str) -> dict[str, Any]:
    """``state`` and the CPU columns from ``/proc/<pid>/task/<tid>/stat``.

    The comm field is parenthesised and may contain spaces *and* parentheses, so
    the split is anchored on the LAST ``)`` rather than on whitespace — a
    whitespace split misreads every thread whose name contains a space, and
    Python names its threads freely.
    """
    close = text.rfind(")")
    if close < 0:
        return {}
    fields = text[close + 1 :].split()
    if not fields:
        return {}

    def at(field_no: int) -> str | None:
        idx = field_no - _STAT_FIELD_STATE
        return fields[idx] if 0 <= idx < len(fields) else None

    out: dict[str, Any] = {"state": at(_STAT_FIELD_STATE)}
    try:
        utime = int(at(_STAT_FIELD_UTIME) or "")
        stime = int(at(_STAT_FIELD_STIME) or "")
    except ValueError:
        return out
    out["cpu_ticks"] = utime + stime
    return out


def _parse_schedstat(text: str) -> dict[str, Any]:
    """``(run_ns, runqueue_wait_ns, timeslices)`` from ``schedstat``.

    The run-queue wait is the load-bearing number in this module: it is time the
    thread was runnable and the CPU was busy with something else, which is how
    host oversubscription is told apart from GIL contention.
    """
    parts = text.split()
    if len(parts) < 3:
        return {}
    try:
        return {
            "run_ns": int(parts[0]),
            "runqueue_wait_ns": int(parts[1]),
            "timeslices": int(parts[2]),
        }
    except ValueError:
        return {}


def _parse_status(text: str) -> dict[str, Any]:
    """Voluntary / involuntary context switches from ``status``."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "voluntary_ctxt_switches":
            field = "voluntary_ctxt_switches"
        elif key == "nonvoluntary_ctxt_switches":
            field = "involuntary_ctxt_switches"
        else:
            continue
        try:
            out[field] = int(value.strip())
        except ValueError:
            continue
    return out


def _task_dir(tid: int) -> str:
    return f"/proc/self/task/{tid}"


def _redact_frame(filename: str, lineno: int, funcname: str) -> str:
    """One frame as ``file:line:func``, path-shortened and scrubbed.

    Both passes matter. :func:`~kiro_crew.perf_sampler.shorten_frame_paths`
    removes the absolute prefix, which carries the operator's home directory and
    therefore their username; :func:`~kiro_crew.perf_sampler.sanitize_profile`
    catches a credential or URL that reached a path or a function name. A frame
    is code, so a hit is unlikely — but this output leaves the process, and the
    cost of scrubbing text this short is nil.
    """
    raw = f"({filename}:{lineno}):{funcname}"
    return perf_sampler.sanitize_profile(perf_sampler.shorten_frame_paths(raw))


def _top_frames() -> dict[int, str]:
    """Thread ident -> its innermost frame, redacted.

    Keyed by Python ident because that is what ``sys._current_frames`` returns;
    the ledger joins it to ``native_id`` (the OS tid procfs needs) through the
    :class:`threading.Thread` objects, which carry both.
    """
    out: dict[int, str] = {}
    for ident, frame in sys._current_frames().items():
        code = getattr(frame, "f_code", None)
        if code is None:
            continue
        out[ident] = _redact_frame(
            getattr(code, "co_filename", "<unknown>"),
            getattr(frame, "f_lineno", 0) or 0,
            getattr(code, "co_name", "<unknown>"),
        )
    return out


def main_thread_stack(max_depth: int = 40) -> dict[str, Any]:
    """The main thread's Python stack, outermost first, redacted.

    Used by the layer-3 stall capture. Bounded depth because a stall inside deep
    recursion would otherwise write a single enormous event row.
    """
    main = threading.main_thread()
    frame = sys._current_frames().get(main.ident or -1)
    frames: list[str] = []
    truncated = False
    current = frame
    while current is not None:
        if len(frames) >= max_depth:
            truncated = True
            break
        code = getattr(current, "f_code", None)
        if code is None:
            break
        frames.append(
            _redact_frame(
                getattr(code, "co_filename", "<unknown>"),
                getattr(current, "f_lineno", 0) or 0,
                getattr(code, "co_name", "<unknown>"),
            )
        )
        current = getattr(current, "f_back", None)
    frames.reverse()
    return {
        "main_thread_stack": frames,
        "stack_truncated": truncated,
        "thread_count": threading.active_count(),
    }


#: Previous CPU/schedstat reading per tid, so the ledger can report a DELTA.
#: A cumulative counter answers "how much CPU since boot", which is never the
#: question; the delta since the last sample is.
_prev_reading: dict[int, tuple[float, int | None, int | None, int | None]] = {}
_prev_lock = threading.Lock()


def _interpret(gil_wait_p95: float | None, runqueue_wait_pct: float | None) -> str:
    """Name the explanation the two numbers support, or decline to.

    This is the whole point of collecting both. The same symptom — work taking
    longer than it should — has two causes with opposite remedies: an
    oversubscribed host (fewer concurrent processes) and GIL contention (less
    Python on the hot path). The kernel's run-queue wait separates them.
    """
    if gil_wait_p95 is None or runqueue_wait_pct is None:
        return "not measured"
    high_gil = gil_wait_p95 > SWITCH_THRESHOLD_MS
    high_rq = runqueue_wait_pct > 10.0
    if high_gil and high_rq:
        return "CPU contention (host oversubscribed); GIL wait is consistent with it"
    if high_gil:
        return "GIL contention: threads wait for the interpreter, not for a CPU"
    if high_rq:
        return "CPU contention (host oversubscribed); GIL wait is low"
    return "no contention signal"


def _build_ledger(gil_wait: dict[str, Any]) -> dict[str, Any]:
    """Build the thread ledger around the caller's one probe distribution."""
    now = time.time()
    frames = _top_frames()
    threads: list[dict[str, Any]] = []
    have_procfs = platform_compat.IS_LINUX
    total_runqueue_ns = 0
    total_run_ns = 0
    measured_any = False

    with _prev_lock:
        previous = dict(_prev_reading)

    fresh: dict[int, tuple[float, int | None, int | None, int | None]] = {}
    for thread in threading.enumerate():
        tid = getattr(thread, "native_id", None)
        entry: dict[str, Any] = {
            "name": thread.name,
            "ident": thread.ident,
            "native_id": tid,
            "daemon": thread.daemon,
            "top_frame": frames.get(thread.ident or -1),
            "state": None,
            "cpu_delta_ms": None,
            "run_delta_ms": None,
            "runqueue_wait_delta_ms": None,
            "voluntary_ctxt_switches": None,
            "involuntary_ctxt_switches": None,
        }
        if have_procfs and tid is not None:
            base = _task_dir(tid)
            stat_text = _read_text(f"{base}/stat")
            sched_text = _read_text(f"{base}/schedstat")
            status_text = _read_text(f"{base}/status")
            stat = _parse_stat(stat_text) if stat_text else {}
            sched = _parse_schedstat(sched_text) if sched_text else {}
            status = _parse_status(status_text) if status_text else {}
            entry["state"] = stat.get("state")
            entry.update(
                {
                    "voluntary_ctxt_switches": status.get("voluntary_ctxt_switches"),
                    "involuntary_ctxt_switches": status.get("involuntary_ctxt_switches"),
                }
            )
            cpu_ticks = stat.get("cpu_ticks")
            run_ns = sched.get("run_ns")
            wait_ns = sched.get("runqueue_wait_ns")
            fresh[tid] = (now, cpu_ticks, run_ns, wait_ns)
            prior = previous.get(tid)
            if prior is not None:
                elapsed = max(1e-6, now - prior[0])
                if cpu_ticks is not None and prior[1] is not None:
                    entry["cpu_delta_ms"] = round(
                        max(0, cpu_ticks - prior[1]) / _CLOCK_TICKS * 1000.0, 3
                    )
                    entry["cpu_frac"] = round((entry["cpu_delta_ms"] or 0.0) / 1000.0 / elapsed, 4)
                if run_ns is not None and prior[2] is not None:
                    entry["run_delta_ms"] = round(max(0, run_ns - prior[2]) / 1e6, 3)
                if wait_ns is not None and prior[3] is not None:
                    entry["runqueue_wait_delta_ms"] = round(max(0, wait_ns - prior[3]) / 1e6, 3)
                if (
                    entry["run_delta_ms"] is not None
                    and entry["runqueue_wait_delta_ms"] is not None
                ):
                    measured_any = True
                    total_run_ns += int((entry["run_delta_ms"] or 0.0) * 1e6)
                    total_runqueue_ns += int((entry["runqueue_wait_delta_ms"] or 0.0) * 1e6)
        threads.append(entry)

    if fresh:
        with _prev_lock:
            _prev_reading.update(fresh)
            # Drop exited threads so a long-lived gateway's dict does not grow
            # by one entry per short-lived worker thread.
            for stale in set(_prev_reading) - set(fresh):
                _prev_reading.pop(stale, None)

    runqueue_pct: float | None = None
    if measured_any and (total_run_ns + total_runqueue_ns) > 0:
        runqueue_pct = round(total_runqueue_ns / (total_run_ns + total_runqueue_ns) * 100.0, 2)

    return {
        "ts": now,
        "runtime": gil_runtime(),
        "gil_wait": gil_wait,
        "gc": _probe.gc_stats(),
        "probe_running": _probe.is_running(),
        "procfs_available": have_procfs,
        "thread_count": len(threads),
        "runqueue_wait_pct": runqueue_pct,
        "interpretation": _interpret(gil_wait.get("p95_ms"), runqueue_pct),
        "caveat": (
            "CPython exposes no GIL owner. gil_wait is inferred from sleep "
            "overshoot and shares its cause with CPU contention; only "
            "sample(deep=True) measures GIL holders directly."
        ),
        "threads": threads,
    }


def ledger_now() -> dict[str, Any]:
    """Full per-thread ledger plus the probe's recent wait distribution.

    The on-demand view (``debug_threads mode=now``). Every kernel column is
    ``None`` where ``/proc/self/task`` does not exist, which is the whole of
    macOS and Windows — those platforms get the Python-level columns (name,
    ident, daemon, top frame) and the probe distribution, and are not handed a
    fabricated substitute.
    """
    return _build_ledger(_probe.recent(30.0))


def window_stats() -> dict[str, Any]:
    """The recorder's ``threads`` block for one sample window.

    Deliberately a SUMMARY rather than the full ledger, and a summary whose size
    depends on whether there is anything to say. A gateway carries 20-40 threads
    and a full entry is ~200 bytes, so every-sample detail would cost ~8 KB a row
    — roughly 20 MB a day against the recorder's ~1.7 MB/day budget. Even the
    ranked extremes measured ~2.5 KB a row (~7 MB/day), so they are now emitted
    only when :func:`_interpret` actually names contention: a quiet window keeps
    the totals, the states and the interpretation, which is what a reader needs to
    see that nothing was wrong, and the per-thread rows appear exactly when
    something was.

    The ledger is still computed every sample either way — that is what advances
    the CPU and run-queue deltas, so the first contended window already has real
    numbers rather than a baseline. The full list stays one :func:`ledger_now`
    call away.
    """
    gil_wait = _probe.window()
    ledger = _build_ledger(gil_wait)
    threads: list[dict[str, Any]] = ledger.get("threads") or []
    interpretation = ledger["interpretation"]
    # "no contention signal" and "not measured" are the two quiet answers.
    detailed = interpretation.startswith(("CPU contention", "GIL contention"))

    def rank(key: str) -> list[dict[str, Any]]:
        if not detailed:
            return []
        scored = [t for t in threads if t.get(key) is not None]
        scored.sort(key=lambda t: t.get(key) or 0.0, reverse=True)
        return [
            {
                "name": t["name"],
                "native_id": t.get("native_id"),
                key: t.get(key),
                "state": t.get("state"),
                "top_frame": t.get("top_frame"),
            }
            for t in scored[:LEDGER_SUMMARY_TOP_N]
            if (t.get(key) or 0.0) > 0.0
        ]

    states: dict[str, int] = {}
    for thread in threads:
        state = thread.get("state") or "?"
        states[state] = states.get(state, 0) + 1

    return {
        "gil_wait": ledger["gil_wait"],
        "runtime": ledger["runtime"],
        "gc": ledger["gc"],
        "probe_running": ledger["probe_running"],
        "procfs_available": ledger["procfs_available"],
        "thread_count": ledger["thread_count"],
        "thread_states": states,
        "runqueue_wait_pct": ledger["runqueue_wait_pct"],
        "interpretation": interpretation,
        # Empty on a quiet window rather than absent: a consumer reads the same
        # keys on every row, and an empty list is 2 bytes.
        "top_cpu": rank("cpu_delta_ms"),
        "top_runqueue_wait": rank("runqueue_wait_delta_ms"),
    }


# ── Layer 3 read-back: the watchdog's crash dumps ────────────────────────────


def list_dumps() -> list[dict[str, Any]]:
    """The loop-stall dumps on disk, newest first, labelled by attribution.

    The dumps are written by :mod:`kiro_crew.dashboard.loop_watchdog` through
    ``faulthandler``, which is C code and cannot redact as it writes — so they
    live in the fenced dump directory and are scrubbed on read
    (:func:`read_dump`). This listing carries only metadata plus the surface the
    stall is attributable to, which is the one label that makes a directory of
    timestamps navigable.
    """
    # Imported here rather than at module scope: the recorder imports this module
    # on the gateway boot path, and stall_attribution pulls in the cron in-flight
    # store, which nothing on that path needs.
    from kiro_crew import stall_attribution
    from kiro_crew.dashboard import crash_dump_store

    out: list[dict[str, Any]] = []
    try:
        dumps_dir = crash_dump_store.get_dumps_dir()
    except OSError:
        return out
    try:
        entries = sorted(
            (
                p
                for p in dumps_dir.iterdir()
                if p.name.startswith(crash_dump_store.DUMP_PREFIX)
                and p.suffix == crash_dump_store.DUMP_SUFFIX
            ),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError:
        return out
    for path in entries:
        record: dict[str, Any] = {"name": path.name}
        try:
            stat = path.stat()
            record["size_bytes"] = stat.st_size
            record["mtime"] = stat.st_mtime
            record["age_seconds"] = round(crash_dump_store.dump_age_seconds(path), 1)
        except OSError:
            record["size_bytes"] = None
            record["mtime"] = None
            record["age_seconds"] = None
        frames = stall_attribution.parse_frames(crash_dump_store.dump_wedged_frames(path))
        record["has_stacks"] = bool(frames)
        record["surface"] = stall_attribution.classify_surface(frames) if frames else None
        record["frame_count"] = len(frames)
        if frames:
            record["stuck_in"] = perf_sampler.sanitize_profile(frames[0].short)
        owner = crash_dump_store.dump_owner_identity(path)
        record["owner_pid"] = owner[0] if owner is not None else None
        out.append(record)
    return out


def read_dump(name: str) -> str:
    """One dump's text, scrubbed.

    *name* is a bare filename, validated against the store's own prefix and
    suffix and rejected if it carries a separator or a parent reference: it
    arrives from an HTTP route, and a reader that joined it to the dump
    directory unchecked would read any file the gateway can. The content then
    goes through path shortening (the dumps carry absolute paths from
    ``faulthandler``, hence the operator's home directory) and the credential /
    URL scrubbers, because that is the only point at which a C-written file
    *can* be redacted.
    """
    from kiro_crew.dashboard import crash_dump_store

    if (
        not name
        or "/" in name
        or "\\" in name
        or name != os.path.basename(name)
        or ".." in name
        or not name.startswith(crash_dump_store.DUMP_PREFIX)
        or not name.endswith(crash_dump_store.DUMP_SUFFIX)
    ):
        raise ValueError(f"not a loop-stall dump name: {name!r}")
    path = crash_dump_store.get_dumps_dir() / name
    lines, truncated = crash_dump_store.dump_replay_lines(
        path, max_lines=4000, max_bytes=256 * 1024
    )
    if not lines:
        raise FileNotFoundError(f"no readable dump content: {name}")
    text = "\n".join(lines)
    if truncated:
        text += "\n[truncated]"
    return perf_sampler.sanitize_profile(perf_sampler.shorten_frame_paths(text))


# ── Layer 4: on-demand sampling ──────────────────────────────────────────────

_sample_lock = threading.Lock()


def _on_event_loop() -> bool:
    """True when called from a thread running an asyncio loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def sample(seconds: float, hz: int, deep: bool = False) -> dict[str, Any]:
    """Profile this process for *seconds* at *hz*, and report GIL waits with it.

    BLOCKING for the whole duration, and it refuses to run on an event-loop
    thread. A route must hand it to a worker thread: the gateway runs the
    dashboard, every agent turn and all background work on one loop, so a
    60-second in-line profile would wedge exactly the loop this module exists to
    explain — and the watchdog would dump-then-exit long before the profile
    returned.

    ``deep=True`` reports the py-spy ``--gil`` command that names GIL *holders*
    rather than inferring waiters, which is the one thing no in-process sampler
    can do. It reports the command instead of running it: that capture is a
    ptrace attach onto this process, and the gateway does not spawn its own
    tracer. Absence is reported too, and no privilege is widened.
    """
    if _on_event_loop():
        return {
            "refused": "sample() blocks and must not run on the event loop; "
            "call it from a worker thread (asyncio.to_thread)."
        }
    if not perf_sampler.profiling_enabled():
        return {"refused": perf_sampler.gate_refusal_message()}
    try:
        duration = float(seconds)
        rate = int(hz)
    except (TypeError, ValueError):
        return {"refused": "seconds must be a number and hz an integer"}
    if duration <= 0 or rate <= 0:
        return {"refused": "seconds and hz must both be positive"}
    # Raised rather than refused, unlike the checks above, because the route maps
    # ValueError to 400 and this IS a malformed request. nan passes every
    # comparison guard here -- ``nan <= 0`` is false -- and then reaches
    # ``time.sleep``, which raises AFTER the sampler thread has started.
    if not math.isfinite(duration):
        raise ValueError(f"seconds: expected a finite number, got {seconds!r}")
    duration = min(duration, MAX_SAMPLE_SECONDS)
    interval = min(
        perf_sampler.MAX_INTERVAL_SECONDS,
        max(perf_sampler.MIN_INTERVAL_SECONDS, 1.0 / rate),
    )

    if not _sample_lock.acquire(blocking=False):
        return {"refused": "a sampling run is already in progress (one at a time)"}
    sampler = None
    try:
        started = time.time()
        sampler = perf_sampler.StackSampler(interval=interval)
        sampler.start()
        time.sleep(duration)
        report = sampler.stop()
        sampler = None
        elapsed = time.time() - started

        ident_names = {t.ident: t.name for t in threading.enumerate()}
        per_thread = [
            {
                "ident": ident,
                "name": ident_names.get(ident),
                # Share of ticks in which this thread had a Python frame at all.
                # "Runnable in Python", not "on CPU": a thread blocked in a
                # syscall still has a frame, which is why this is reported
                # beside the kernel state rather than instead of it.
                "runnable_in_python_frac": (
                    round(count / report.samples, 4) if report.samples else None
                ),
                "samples": count,
            }
            for ident, count in sorted(report.per_thread_samples.items(), key=lambda kv: -kv[1])
        ]

        out: dict[str, Any] = {
            "requested": {"seconds": seconds, "hz": hz, "deep": bool(deep)},
            "effective": {
                "seconds": round(elapsed, 3),
                "interval_secs": interval,
                "rate_hz": round(report.effective_rate, 2),
                "samples": report.samples,
            },
            "runtime": gil_runtime(),
            "gil_wait_during_window": _probe.recent(elapsed),
            "per_thread": per_thread,
            "folded": perf_sampler.sanitize_profile(perf_sampler.render_folded(report)),
            "truncated_stacks": report.truncated_stacks,
            "caveat": (
                "Folded stacks and runnable_in_python_frac come from "
                "sys._current_frames: they show which thread had Python frames, "
                "not which held the GIL. deep=True returns a command capable of "
                "measuring GIL holders."
            ),
        }
        if deep:
            out["deep"] = _deep_sample(duration, rate)
        return out
    finally:
        # A sampler still set here started and did not stop, so the run left
        # through an exception. Releasing the lock alone would leave its daemon
        # thread sampling for the life of the process, once per request.
        if sampler is not None:
            try:
                sampler.stop()
            except Exception:  # noqa: BLE001 - cleanup must not mask the original
                logger.debug("diag: sampler stop during cleanup failed", exc_info=True)
        _sample_lock.release()


def _deep_sample(seconds: float, hz: int) -> dict[str, Any]:
    """Build the py-spy ``--gil`` command for this process, without running it.

    The command is returned, not executed, and that is deliberate on two counts.
    :func:`kiro_crew.perf_sampler.pyspy_argv` documents the division itself -
    "returned rather than executed so the CLI owns spawning" - and the thing
    being asked for here is a ptrace attach onto the gateway. A gateway that
    spawned its own tracer in response to an HTTP request would be holding far
    more authority than a diagnostic needs, which is also why the spawn audit
    (``test_spawn_audit.py``) requires every subprocess in this package to be
    either sandbox-routed or justified: the honest answer is to spawn nothing.

    So this reports what to run and why it is worth running. An operator pastes
    one command and gets the one measurement no in-process sampler can produce,
    because ``--gil`` keeps only the traces that held the GIL. Absence and
    refusal are both reported rather than guessed at.
    """
    if perf_sampler.pyspy_path() is None:
        return {"available": False, "reason": perf_sampler.pyspy_unavailable_message()}
    try:
        argv = perf_sampler.pyspy_argv(
            os.getpid(), max(1, int(seconds)), _pyspy_output_path(), hz, gil=True
        )
    except FileNotFoundError:
        return {"available": False, "reason": perf_sampler.pyspy_unavailable_message()}
    return {
        "available": True,
        "executed": False,
        "gil_only": True,
        "argv": [perf_sampler.sanitize_profile(arg) for arg in argv],
        "note": (
            "Run this to capture GIL holders; only traces holding the GIL are "
            "included. It is not run for you: it is a ptrace attach onto this "
            "gateway, and the gateway does not spawn its own tracer. The output "
            "path is a private directory created for this one answer, so the "
            "capture cannot be redirected by anything that guessed it in advance; "
            "move the file where you want it once py-spy exits."
        ),
        "hint": perf_sampler.pyspy_attach_failure_hint(),
    }


#: How long an unused GIL staging directory survives before the next call reaps it.
_GIL_STAGING_MAX_AGE_SECS = 86400.0


def _gil_staging_root() -> "Any":
    """The parent of the per-call staging directories."""
    from kiro_crew.config.paths import config_dir

    return config_dir() / "diag" / "gil-staging"


def _reap_gil_staging(root: "Any", now: float) -> int:
    """Delete staging directories older than the age above. Returns the count.

    A directory is created per call and the command it is for may never be run, so
    something has to collect them. The next call does it, which keeps the cost on
    the feature that creates them rather than on a scheduler that would have to
    know about it.
    """
    import shutil

    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if now - entry.stat().st_mtime <= _GIL_STAGING_MAX_AGE_SECS:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    return removed


def _pyspy_output_path() -> "Any":
    """Where the operator's py-spy run should write its artifact.

    A freshly created private directory under the data home, NOT a predictable
    name in the shared temp directory. The old shape named the file after this
    process id in a world-writable place, so anyone able to guess the pid -- which
    the gateway reports on request -- could leave a symlink at that exact path and
    have the operator's capture follow it. The capture may run with ptrace
    privilege, so the file it lands on is not a small question.

    ``mkdtemp`` both randomises the name and creates it 0700 in one step, which is
    what removes the plant window: there is no interval in which the path is known
    and does not yet exist.
    """
    import tempfile
    import time as _time

    root = _gil_staging_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reap_gil_staging(root, _time.time())
    # ``mkdtemp`` creates the directory 0700 itself; no chmod follows, so there is
    # no moment at which it carries wider permissions.
    staging = tempfile.mkdtemp(prefix="gil-", dir=str(root))
    from pathlib import Path

    return Path(staging) / "profile.folded"
