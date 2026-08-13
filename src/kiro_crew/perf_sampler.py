"""Debug-only statistical stack sampler.

KiroCrew has aggregate duration histograms (``kiro_crew.metrics``) and a stall
detector that dumps thread stacks when the event loop wedges
(``dashboard.loop_watchdog``), but nothing that attributes CPU/wall time to
call paths. This module is the missing piece: a sampling profiler that turns a
window of execution into folded stacks, which speedscope, flamegraph.pl and
Perfetto all import directly.

Two sampling strategies, because they answer different questions:

* **In-process** (:class:`StackSampler`) -- a daemon thread wakes on an interval
  and reads every other thread's stack via :func:`sys._current_frames`. Needs no
  extra privileges, no third-party package, and works on macOS and Windows. It
  can only see the process it runs in, so it profiles a command executed by the
  CLI, not a gateway already running elsewhere.
* **Out-of-process** (:func:`pyspy_argv`) -- py-spy attaches to a foreign PID.
  This is the only way to profile an already-running gateway, and it is
  deliberately optional: py-spy needs ``task_for_pid`` on macOS, which the OS
  denies without elevated privileges (the same constraint that made
  ``loop_watchdog`` prefer an in-process ``faulthandler`` timer over an external
  py-spy capture). When py-spy is missing or refused, the caller reports that
  instead of silently producing nothing.

Nothing here runs unless a CLI command asks for it. There is no import-time
hook, no background thread at rest, and no HTTP surface -- see
:func:`profiling_enabled` for the gate.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

# Environment switch. Off unless explicitly set, so a normal install cannot
# start a sampler by accident and CI never carries one.
DEBUG_ENV_VAR = "KIROCREW_DEBUG"

# Interval bounds. Below ~1ms the sampler thread costs more than it measures;
# above 1s the sample count is too small to attribute anything.
MIN_INTERVAL_SECONDS = 0.001
MAX_INTERVAL_SECONDS = 1.0
DEFAULT_INTERVAL_SECONDS = 0.005

# Frames deeper than this are truncated. Runaway recursion would otherwise
# produce single folded lines megabytes wide.
MAX_STACK_DEPTH = 200

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def profiling_enabled(env: dict[str, str] | None = None) -> bool:
    """True when the debug gate is set.

    Read from *env* (defaults to the real environment) so tests do not mutate
    global state. Any value outside :data:`_TRUTHY` counts as off, which means a
    stray ``KIROCREW_DEBUG=0`` reads as disabled rather than as "the name is
    present, therefore on".
    """
    source = os.environ if env is None else env
    return source.get(DEBUG_ENV_VAR, "").strip().lower() in _TRUTHY


def gate_refusal_message() -> str:
    """One-line explanation of how to enable profiling, for the CLI to print."""
    return (
        f"Profiling is off. It is a debug-only tool, so it must be enabled "
        f"explicitly: re-run with {DEBUG_ENV_VAR}=1 set in the environment."
    )


@dataclass(frozen=True)
class SampleReport:
    """Result of a sampling run.

    ``counts`` maps a folded stack ("outermost;...;innermost") to the number of
    samples in which that exact stack was on top. ``samples`` is the number of
    sampling ticks taken and ``duration`` the wall-clock span, so a caller can
    report the effective rate actually achieved rather than the one requested --
    a loaded machine will not hit the requested interval, and reporting the
    request as if it were the result would overstate the resolution.
    """

    counts: dict[str, int]
    samples: int
    duration: float
    interval: float
    truncated_stacks: int = 0

    @property
    def effective_rate(self) -> float:
        """Samples per second actually achieved (0.0 when nothing was sampled)."""
        if self.duration <= 0 or self.samples <= 0:
            return 0.0
        return self.samples / self.duration


def _frame_label(filename: str, lineno: int, funcname: str) -> str:
    """Render one frame as ``func (file:line)`` with the path shortened.

    Absolute paths are reduced to their last two components. That keeps the
    label useful for navigation while dropping the home-directory prefix, which
    is user-identifying and worthless for reading a flamegraph.
    """
    try:
        parts = Path(filename).parts
    except (TypeError, ValueError):
        parts = ()
    short = "/".join(parts[-2:]) if parts else filename or "<unknown>"
    return f"{funcname} ({short}:{lineno})"


def _fold_frame_stack(frame: object) -> tuple[str, bool]:
    """Walk a frame's ``f_back`` chain into a folded, outermost-first stack.

    Returns the folded string plus whether the walk hit
    :data:`MAX_STACK_DEPTH`. Frames are collected innermost-first (the direction
    the chain runs) and reversed once, rather than prepending, so a deep stack
    stays linear instead of quadratic.
    """
    labels: list[str] = []
    truncated = False
    current = frame
    while current is not None:
        if len(labels) >= MAX_STACK_DEPTH:
            truncated = True
            break
        code = getattr(current, "f_code", None)
        if code is None:
            break
        labels.append(
            _frame_label(
                getattr(code, "co_filename", "<unknown>"),
                getattr(current, "f_lineno", 0) or 0,
                getattr(code, "co_name", "<unknown>"),
            )
        )
        current = getattr(current, "f_back", None)
    labels.reverse()
    return ";".join(labels), truncated


class StackSampler:
    """Samples every thread's stack from a daemon thread on a fixed interval.

    The sampler thread excludes itself: including it would put its own sleep at
    the top of a large share of samples and crowd out the real work.
    """

    def __init__(self, interval: float = DEFAULT_INTERVAL_SECONDS) -> None:
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ValueError(
                f"interval must be between {MIN_INTERVAL_SECONDS} and "
                f"{MAX_INTERVAL_SECONDS} seconds, got {interval}"
            )
        self._interval = interval
        self._counts: collections.Counter[str] = collections.Counter()
        self._samples = 0
        self._truncated = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._stopped_at = 0.0

    def _tick(self) -> None:
        """Take one sample of every thread except the sampler's own."""
        own = threading.get_ident()
        for tid, frame in sys._current_frames().items():
            if tid == own:
                continue
            folded, truncated = _fold_frame_stack(frame)
            if not folded:
                continue
            self._counts[folded] += 1
            if truncated:
                self._truncated += 1
        self._samples += 1

    def _run(self) -> None:
        # Absolute deadlines rather than sleep(interval): a fixed sleep drifts by
        # however long each tick takes, so the achieved rate would sag under load
        # without that showing up anywhere.
        next_at = time.perf_counter()
        while not self._stop.is_set():
            next_at += self._interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                if self._stop.wait(delay):
                    break
            else:
                # Behind schedule: skip the backlog instead of spinning to catch
                # up, which would sample in a tight burst and skew the profile.
                next_at = time.perf_counter()
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - a debug tool must not kill its host
                # A frame can vanish mid-walk when its thread exits. Dropping the
                # sample keeps the run going; the report's sample count reflects it.
                continue

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sampler already started")
        self._started_at = time.perf_counter()
        self._thread = threading.Thread(
            target=self._run, name="kirocrew-perf-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> SampleReport:
        if self._thread is None:
            raise RuntimeError("sampler not started")
        self._stop.set()
        # Bounded join: the loop only ever waits on the stop event or a bounded
        # sleep, so it exits promptly. The timeout means a wedged sampler still
        # yields whatever it collected instead of hanging the CLI.
        self._thread.join(timeout=max(1.0, self._interval * 20))
        self._stopped_at = time.perf_counter()
        self._thread = None
        return SampleReport(
            counts=dict(self._counts),
            samples=self._samples,
            duration=max(0.0, self._stopped_at - self._started_at),
            interval=self._interval,
            truncated_stacks=self._truncated,
        )

    def __enter__(self) -> "StackSampler":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._thread is not None:
            self.stop()


def render_folded(report: SampleReport) -> str:
    """Render a report as folded stacks, hottest first.

    Folded text is the lowest-common-denominator profile format: speedscope,
    flamegraph.pl and Perfetto all read it, so emitting it avoids owning a
    version-pinned JSON schema. Sorted by descending count then by stack so the
    output is deterministic and diffable between runs.
    """
    lines = [
        f"{stack} {count}"
        for stack, count in sorted(report.counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def shorten_frame_paths(text: str) -> str:
    """Reduce absolute paths in a folded profile to their last two components.

    :func:`_frame_label` does this for the in-process sampler, but py-spy writes
    its own artifact with full paths from the target process, so the same
    guarantee has to be applied to that text after the fact. Without this the
    documented promise ("absolute paths and the home-directory prefix are
    dropped") held only for one of the two sampling strategies, and a py-spy
    profile leaked the operator's home directory (hence username).

    Matches POSIX and Windows absolute paths that end in a source-file component,
    which is the only shape py-spy emits inside a frame label. Relative paths are
    left alone -- they carry no home prefix to strip.

    Two passes, because a path component may legitimately contain a space
    (``/home/Jane Doe/...``, ``C:\\Users\\Jane Doe\\...``). A single
    space-excluding pattern matched only the tail after the last space, so it
    rewrote ``/home/Jane Doe/proj/pkg/mod.py`` to ``/home/Jane Doepkg/mod.py`` --
    still carrying the home prefix, which is precisely what this function exists
    to remove. The first pass therefore handles the parenthesized ``(path:line)``
    form py-spy emits, where the closing paren is an unambiguous terminator so
    spaces inside the path are safe to consume; the second pass keeps the
    conservative space-excluding rule for any bare path outside parentheses,
    where a space cannot be told apart from surrounding prose.
    """

    def _shorten(raw_path: str) -> str:
        parts = [p for p in re.split(r"[/\\]", raw_path) if p and not p.endswith(":")]
        return "/".join(parts[-2:]) if len(parts) >= 2 else raw_path

    def _replace(match: "re.Match[str]") -> str:
        return _shorten(match.group(0))

    def _replace_parenthesized(match: "re.Match[str]") -> str:
        # Rebuild the wrapper so only the path itself is rewritten. The suffix
        # group is optional, so it is None when there is no ":<line>".
        return f"({_shorten(match.group('path'))}{match.group('suffix') or ''})"

    # Pass 1: "(<absolute path>:<line>)" -- the shape py-spy writes. The path may
    # contain spaces; ")" terminates it unambiguously.
    parenthesized = (
        r"\((?P<path>(?:[A-Za-z]:)?[/\\][^)]*?\.[A-Za-z0-9_]+)(?P<suffix>:\d+)?\)"
    )
    text = re.sub(parenthesized, _replace_parenthesized, text)

    # Pass 2: bare absolute paths outside parentheses. Anchored on the extension
    # so bare directories and ordinary prose are not rewritten; space-excluding
    # because nothing delimits the end of the path here.
    pattern = r"(?:[A-Za-z]:)?(?:[/\\][^/\\\s;:()]+)+\.[A-Za-z0-9_]+"
    return re.sub(pattern, _replace, text)


def sanitize_profile(text: str) -> str:
    """Strip credentials and exfiltration URLs from profile output.

    Frame labels are code identifiers and shortened paths, so a hit is unlikely
    -- but a sampled frame can carry a literal in a filename, and the artifact is
    meant to be sent to a maintainer. Redacting on the way out is cheap
    insurance, and matches the redact-before-egress rule the rest of the
    codebase follows.
    """
    cleaned, _ = redact_exfiltration_urls(text)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned


def _home_dir() -> Path:
    """Return the user's home directory.

    A one-line indirection purely so tests can simulate an unresolvable home
    without monkeypatching ``pathlib.Path.home`` itself. Patching that class
    attribute mutates pathlib process-wide for the duration of the test, which
    breaks unrelated code calling ``Path.home()`` and corrupted ``WindowsPath``
    internals on the Windows CI shard.
    """
    return Path.home()


def pyspy_candidates() -> tuple[Path, ...]:
    """Explicit install locations to probe before falling back to ``PATH``.

    Mirrors ``website/electron/pyspy-dump.js``, which learned this the hard way:
    a process launched by the desktop app (or by launchd) gets a minimal ``PATH``
    with no shell profile sourced, so a py-spy installed by homebrew, cargo or
    ``pip --user`` is present on disk but invisible to a ``PATH`` lookup. Probing
    the known locations first is what stops the attach path reporting itself
    unavailable on a machine that has py-spy.

    Kept in sync with that module's ``PYSPY_CANDIDATES`` list.

    Empty on Windows, deliberately. Every entry is a POSIX location, the binary
    there is ``py-spy.exe`` rather than ``py-spy``, and ``os.access(X_OK)`` is
    permissive on Windows (there is no execute bit, so any existing file answers
    True) -- probing would therefore be both useless and wrong. Windows discovery
    goes through :func:`shutil.which`, which applies ``PATHEXT``.
    """
    if platform_compat.IS_WINDOWS:
        return ()
    absolute = (
        Path("/opt/homebrew/bin/py-spy"),
        Path("/usr/local/bin/py-spy"),
    )
    try:
        home = _home_dir()
    except (RuntimeError, OSError):
        # Path.home() raises when the home directory cannot be resolved -- e.g. a
        # systemd unit or container with no HOME and no passwd entry. The
        # home-relative candidates are simply unavailable there; the absolute ones
        # and the PATH fallback still work, so degrade rather than raise out of a
        # discovery helper.
        return absolute
    return absolute + (
        home / ".cargo" / "bin" / "py-spy",
        home / ".local" / "bin" / "py-spy",
    )


def pyspy_path() -> str | None:
    """Absolute path to a py-spy binary, or None when it is not installed.

    Candidate locations first, then ``PATH`` (via :func:`shutil.which`, which also
    applies ``PATHEXT`` on Windows).
    """
    for candidate in pyspy_candidates():
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return shutil.which("py-spy")


def pyspy_argv(pid: int, seconds: int, output: Path, rate: int) -> list[str]:
    """Build the py-spy argv for attaching to *pid*.

    ``--format raw`` emits folded stacks, matching :func:`render_folded`, so both
    sampling strategies produce one interchangeable artifact format. Returned
    rather than executed so the CLI owns spawning (and so this stays testable
    without a real py-spy).
    """
    binary = pyspy_path()
    if binary is None:
        raise FileNotFoundError("py-spy is not installed")
    return [
        binary,
        "record",
        "--pid",
        str(pid),
        "--duration",
        str(seconds),
        "--rate",
        str(rate),
        "--format",
        "raw",
        "--output",
        str(output),
    ]


def pyspy_attach_failure_hint() -> str:
    """Explain the two distinct reasons an attach is refused.

    Blaming privileges alone is misleading on Linux, where the more likely cause
    is that something else already holds the target: ptrace admits exactly one
    tracer per process, so a second ``PTRACE_ATTACH`` is refused while another is
    attached. The desktop app does exactly that — ``website/electron/pyspy-dump.js``
    runs ``py-spy dump`` on the gateway just before SIGKILL when the liveness
    monitor declares it wedged.

    That capture deliberately takes precedence: it is the only record of the
    frozen frame and it is time-bounded and unrepeatable, whereas this command is
    operator-invoked and can simply be re-run. So the guidance is to retry rather
    than to defeat it.

    macOS differs — py-spy reads via ``task_for_pid`` there, where multiple
    readers can hold a task port, so a refusal on macOS points at privileges
    instead.
    """
    return (
        "py-spy could not attach. Two common causes:\n"
        "  1. Another tracer already holds the process. On Linux only ONE ptrace "
        "tracer is allowed per process, and the desktop app attaches py-spy to the "
        "gateway to capture a frozen stack when it looks wedged. That capture wins "
        "by design (it is the only record of the freeze) — wait and re-run.\n"
        "  2. Insufficient privileges. On macOS reading another process needs "
        "task_for_pid, which the OS denies without elevation; try sudo."
    )


def pyspy_unavailable_message() -> str:
    """Explain that out-of-process sampling needs py-spy, and how to get it."""
    return (
        "Attaching to another process needs py-spy, which is not installed.\n"
        "Install it with:  pip install 'kirocrew[perf]'   (or: pip install py-spy)\n"
        "On macOS py-spy also needs elevated privileges to read another "
        "process (the OS denies task_for_pid otherwise), so it may require sudo.\n"
        "To profile work in this process instead, use: --call module:callable"
    )
