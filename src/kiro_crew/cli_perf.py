"""``kirocrew perf`` -- debug-only performance sampling commands.

Thin CLI layer: argument handling, gate enforcement, process resolution and
output. The sampling itself lives in :mod:`kiro_crew.perf_sampler`.

Two shapes, because they answer different questions and have different
requirements:

* ``kirocrew perf sample --pid <PID>`` (default: the running gateway) attaches
  from outside via py-spy. This is the only way to see a gateway that is already
  serving, and it needs py-spy installed (plus privileges on macOS).
* ``kirocrew perf sample --call <module:callable>`` runs that callable in this
  process with the in-process sampler around it. No extra dependency, works
  everywhere, and is the path for profiling one code path in isolation.
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir
from kiro_crew.gateway_lock import LOCK_FILENAME
from kiro_crew.perf_sampler import (
    DEFAULT_INTERVAL_SECONDS,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    StackSampler,
    gate_refusal_message,
    profiling_enabled,
    pyspy_argv,
    pyspy_attach_failure_hint,
    pyspy_path,
    pyspy_unavailable_message,
    render_folded,
    sanitize_profile,
    shorten_frame_paths,
)

# Ceiling on a single run. A sampler left running for hours produces an artifact
# nobody can load; a maintainer asking a user to profile wants a short window.
MAX_SECONDS = 300


def _read_gateway_pid() -> int | None:
    """PID of a **live** gateway from ``$KIROCREW_HOME/gateway.lock``, or None.

    Two steps, because the recorded PID alone is not evidence. The gateway stamps
    its PID on acquire but nothing clears it on exit, so a stopped gateway leaves
    a stale number behind — and PIDs are reused, so attaching to a stale one would
    profile an unrelated process and label the artifact as the gateway's.

    The authoritative check is the lock itself: try to take it non-blockingly. If
    we get it, nothing holds it and there is no live gateway, so we release it
    immediately and report None. If we cannot, a live holder exists and the
    recorded PID names it. Any error reads as "cannot confirm" and returns None —
    fail closed, since the failure mode is profiling the wrong process.
    """
    lock_path = config_dir() / LOCK_FILENAME
    try:
        recorded = lock_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        pid = int(recorded.splitlines()[0]) if recorded else 0
    except (ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    if not _gateway_lock_is_held(lock_path):
        return None
    # A live holder exists and the file names this pid. Confirm the process is
    # actually there: on a torn write the recorded pid can disagree with the
    # holder, and attaching to a dead pid should read as "no gateway".
    if not platform_compat.pid_exists(pid):
        return None
    return pid


def _gateway_lock_is_held(lock_path: Path) -> bool:
    """True when something holds the gateway lock (i.e. a gateway is running).

    Non-destructive: acquiring is only used as a probe and released at once, so a
    real gateway is never disturbed. Errors read as held, so an unreadable lock
    never gets mistaken for "no gateway running".
    """
    if not lock_path.exists():
        return False
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        if platform_compat.try_acquire_lock(fd, exclusive=True):
            platform_compat.release_lock(fd)
            return False
        return True
    except OSError:
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _resolve_callable(spec: str) -> object:
    """Import ``module:attr`` and return the attribute.

    Deliberately narrow: an import plus ``getattr``, no expression evaluation. It
    is still arbitrary in-process code execution, which is why it sits behind the
    debug gate -- though it grants nothing a local user could not already do by
    running python directly against the same installed package.
    """
    if ":" not in spec:
        raise ValueError(f"expected 'module:callable', got {spec!r}")
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"expected 'module:callable', got {spec!r}")
    module = importlib.import_module(module_name)
    target = module
    for part in attr.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise TypeError(f"{spec} is not callable")
    return target


def _write_artifact(output: Path, text: str) -> bool:
    """Write the profile owner-only. Returns False (with a diagnostic) on failure.

    Uses :func:`atomic_write` rather than ``Path.write_text``: the default output
    path is relative, so it usually lands in whatever directory the operator ran
    the command from -- frequently a repo checkout. ``write_text`` follows a
    symlink, so a planted ``kirocrew-profile.folded`` link would have the linked
    file truncated and chmodded to 0o600. atomic_write creates a fresh temp file
    and renames over the path, which replaces the link instead of writing through
    it, and applies the mode to the file it actually created.

    Errors are reported rather than raised: by the time this runs the sampling is
    already done, so an unwritable ``--output`` should cost the operator a clear
    message and a nonzero exit, not a traceback over a profile that was collected
    successfully.

    0o600 because a profile names code paths and file layout from the user's
    machine; it is diagnostic data, not world-readable output.
    """
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(output, text, mode=0o600)
    except OSError as exc:
        print(f"Could not write the profile to {output}: {exc}", file=sys.stderr)
        return False
    return True


def _sample_in_process(args: argparse.Namespace) -> int:
    """Profile a callable in this process with the in-process sampler."""
    try:
        target = _resolve_callable(args.perf_call)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - see below
        # Deliberately broad: resolving runs the target module's top-level code,
        # which can raise anything (a RuntimeError from a failed import guard, a
        # KeyError from missing config) or call sys.exit -- hence SystemExit is
        # named explicitly, since it is a BaseException and Exception alone would
        # let it through. KeyboardInterrupt is deliberately NOT caught: an operator
        # interrupting the command should still interrupt it. Report the type so
        # the failure stays diagnosable.
        print(
            f"Cannot resolve --call {args.perf_call!r}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 2

    sampler = StackSampler(interval=args.interval)
    started = time.perf_counter()
    sampler.start()
    try:
        target()  # type: ignore[operator]
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - report, still emit
        # The profile of a call that raised is often exactly what is wanted, so
        # keep the samples and surface the error rather than discarding both.
        #
        # SystemExit is included deliberately and is not hypothetical: the most
        # natural things to profile here are CLI entry points, and those call
        # sys.exit on their normal path (`--call kiro_crew.cli_doctor:_doctor`
        # exits 1 whenever it finds an issue). Catching only Exception meant the
        # run that mattered most produced no artifact at all. KeyboardInterrupt
        # is BaseException and not SystemExit, so Ctrl-C still propagates and
        # aborts rather than silently yielding a partial profile.
        print(f"--call raised {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        report = sampler.stop()
    elapsed = time.perf_counter() - started

    if report.samples == 0:
        print(
            "No samples collected -- the command finished faster than one "
            f"sampling interval ({args.interval}s). Lower --interval or profile "
            "more work.",
            file=sys.stderr,
        )
        return 1

    if not _write_artifact(args.output, sanitize_profile(render_folded(report))):
        return 6
    print(
        f"Sampled {report.samples} ticks over {elapsed:.2f}s "
        f"({report.effective_rate:.0f}/s effective, {args.interval}s requested)."
    )
    if report.truncated_stacks:
        print(f"{report.truncated_stacks} stack(s) truncated at maximum depth.")
    print(f"Wrote folded stacks to {args.output}")
    print("Open it in speedscope (https://speedscope.app) or flamegraph.pl.")
    return 0


def _sample_out_of_process(args: argparse.Namespace, pid: int) -> int:
    """Profile a foreign PID by shelling out to py-spy.

    py-spy is pointed at a **private temporary file**, never at ``--output``. Two
    reasons, both load-bearing:

    * py-spy opens its output path directly, so a symlink at ``--output`` would
      have its target truncated by py-spy before this function ever got to
      sanitize and re-write. Routing through a temp dir we own means the caller's
      path is only ever touched by :func:`_write_artifact`, which renames over it.
    * py-spy's raw output has to be re-read and redacted anyway (it embeds
      absolute paths from the target process), so the artifact the operator asked
      for should only ever appear in its final, sanitized form -- never as an
      intermediate the redactors have not seen yet.

    Every syscall on this path is guarded: a discovered-but-unlaunchable binary
    makes ``subprocess.run`` raise ``OSError``, which would otherwise surface as a
    traceback rather than an exit code.
    """
    if pyspy_path() is None:
        print(pyspy_unavailable_message(), file=sys.stderr)
        return 3

    rate = max(1, int(round(1.0 / args.interval)))
    print(f"Attaching py-spy to pid {pid} for {args.seconds}s at {rate}Hz...")

    try:
        # 0o700 by default, and removed on exit, so the intermediate profile is
        # never readable by another user and never left behind.
        with tempfile.TemporaryDirectory(prefix="kirocrew-perf-") as tmpdir:
            staged = Path(tmpdir) / "profile.folded"
            try:
                argv = pyspy_argv(pid=pid, seconds=args.seconds, output=staged, rate=rate)
            except FileNotFoundError:
                # py-spy vanished between the check above and here.
                print(pyspy_unavailable_message(), file=sys.stderr)
                return 3
            try:
                completed = subprocess.run(
                    argv, capture_output=True, text=True, timeout=args.seconds + 60
                )
            except subprocess.TimeoutExpired:
                print("py-spy did not finish within its duration plus 60s.", file=sys.stderr)
                return 4
            except OSError as exc:
                # A discovered path can still be unlaunchable: wrong architecture,
                # not actually executable, a broken interpreter line.
                print(f"Could not run py-spy ({argv[0]}): {exc}", file=sys.stderr)
                return 3
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                print(f"py-spy failed (exit {completed.returncode}).", file=sys.stderr)
                if detail:
                    print(sanitize_profile(detail), file=sys.stderr)
                print(pyspy_attach_failure_hint(), file=sys.stderr)
                return completed.returncode
            try:
                raw = staged.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                print("py-spy reported success but wrote no readable profile.", file=sys.stderr)
                return 5
    except OSError as exc:
        # Creating the temp dir can fail (no space, unwritable TMPDIR).
        print(f"Could not create a temporary directory for the profile: {exc}", file=sys.stderr)
        return 6

    if not _write_artifact(args.output, sanitize_profile(shorten_frame_paths(raw))):
        return 6
    print(f"Wrote folded stacks to {args.output}")
    print("Open it in speedscope (https://speedscope.app) or flamegraph.pl.")
    return 0


def _perf_sample(args: argparse.Namespace) -> int:
    """Entry point for ``kirocrew perf sample``."""
    if not profiling_enabled():
        print(gate_refusal_message(), file=sys.stderr)
        return 1

    if not MIN_INTERVAL_SECONDS <= args.interval <= MAX_INTERVAL_SECONDS:
        print(
            f"--interval must be between {MIN_INTERVAL_SECONDS} and "
            f"{MAX_INTERVAL_SECONDS} seconds.",
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.seconds <= MAX_SECONDS:
        print(f"--seconds must be between 1 and {MAX_SECONDS}.", file=sys.stderr)
        return 2

    if args.perf_call:
        return _sample_in_process(args)

    pid = args.pid or _read_gateway_pid()
    if pid is None:
        print(
            "No running gateway found (no pid recorded in "
            f"{config_dir() / LOCK_FILENAME}). Pass --pid explicitly, or use "
            "--call to profile a code path in this process.",
            file=sys.stderr,
        )
        return 2
    return _sample_out_of_process(args, pid)


def perf_cmd(args: argparse.Namespace) -> int:
    """Dispatch a ``perf`` subcommand."""
    if args.perf_action == "sample":
        return _perf_sample(args)
    print("Usage: kirocrew perf sample [--pid PID | --call module:callable]", file=sys.stderr)
    return 2


def register_perf_parser(sub: argparse._SubParsersAction) -> None:
    """Wire ``kirocrew perf`` into the top-level parser.

    Named ``perf`` rather than ``profile``: ``kirocrew policy profile`` already
    exists, and "profile" is overloaded three ways in this codebase (governance
    profiles, AWS profiles, deploy profiles).
    """
    perf_parser = sub.add_parser("perf", help="Debug-only performance sampling (off by default)")
    perf_sub = perf_parser.add_subparsers(dest="perf_action")
    sample = perf_sub.add_parser(
        "sample",
        help="Sample stacks into a folded-stack profile",
        description=(
            "Debug-only stack sampler. Requires KIROCREW_DEBUG=1. With no "
            "--call, attaches to the running gateway (or --pid) using py-spy; "
            "with --call, profiles that callable in this process."
        ),
    )
    sample.add_argument(
        "--seconds", type=int, default=10, help=f"Attach duration, 1-{MAX_SECONDS} (default: 10)"
    )
    sample.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=(
            f"Seconds between samples, {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS} "
            f"(default: {DEFAULT_INTERVAL_SECONDS})"
        ),
    )
    sample.add_argument(
        "--pid", type=int, default=0, help="Target PID (default: the running gateway)"
    )
    sample.add_argument(
        "--call",
        dest="perf_call",
        default="",
        metavar="MODULE:CALLABLE",
        # dest is explicit and prefixed: the top-level subparsers use
        # dest="command", so a flag named --command here would silently overwrite
        # the subcommand name and route the invocation to argparse's help path.
        help="Profile 'module:callable' in this process instead of attaching",
    )
    sample.add_argument(
        "--output",
        type=Path,
        default=Path("kirocrew-profile.folded"),
        help="Where to write the profile (default: ./kirocrew-profile.folded)",
    )
