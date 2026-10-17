"""What ``kirocrew gateway`` prepares in ``kiro_crew.cli.main`` before ``asyncio.run``.

``_resolve_gateway_args`` turns the parsed ``gateway`` arguments into the ``_gateway()``
keyword arguments (the ``--test-mode`` bundle, the ``--port`` check and the
``--approval yolo`` rail); ``_install_child_watcher`` replaces the thread-per-child asyncio
watcher; ``_diagnostic_port`` and ``_diagnostic_bind_address`` name the port and address a
refused gateway lock reports.

Split out of ``kiro_crew.cli``, which stays the import path and the patch target: every name
here is reached as ``kiro_crew.cli.<name>``, and a patch through that facade reaches this
module (``kiro_crew._facade_mirror``). Nothing here imports the facade. New gateway-launch
preparation goes here; the ``gateway`` dispatch branch itself stays in ``main``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.paths import _default_home, _legacy_home

# The facade's logger, by name: records from here carry ``kiro_crew.cli``.
logger = logging.getLogger("kiro_crew.cli")


def _install_child_watcher() -> None:
    """Install a non-thread-per-child asyncio child watcher (Linux: pidfd; macOS: SIGCHLD).

    CPython 3.10's default ThreadedChildWatcher spawns a daemon thread per
    subprocess that blocks on os.waitpid(pid, 0).  When many children die
    simultaneously the resulting GIL contention starves the event-loop thread
    for tens of seconds.  PidfdChildWatcher uses a single epoll fd (no extra
    threads) and is immune to this.  On macOS (no pidfd syscall) we install
    SafeChildWatcher instead -- a single SIGCHLD handler, also free of the
    thread-per-child storm (the node_all_bin_dirs lru_cache only
    shrank the surface; the reaper storm itself remained on the default
    watcher).

    Python 3.14: ``set_child_watcher`` / ``PidfdChildWatcher`` /
    ``SafeChildWatcher`` are deprecated in CPython 3.12 and REMOVED in 3.14 --
    the event loop reaps children directly.  The whole function is therefore a
    no-op there, short-circuited by the ``hasattr`` guard below; without it the
    unguarded Linux pidfd branch raised ``AttributeError: module 'asyncio' has
    no attribute 'set_child_watcher'`` and killed ``kirocrew gateway`` on
    startup, before the port was ever bound.  The mitigation is not lost: 3.14
    reaps with a single non-thread reaper, so the thread-per-child storm this
    function exists to prevent cannot occur.
    ``set_child_watcher`` on a pre-run policy is attached to the loop by
    ``asyncio.run`` -> ``set_event_loop`` (main thread), so installing before
    ``asyncio.run`` here is correct on 3.10 for both watchers.

    Main-thread dependency (macOS): unlike the default ThreadedChildWatcher
    (whose ``is_active()`` is hard-coded True and whose ``attach_loop`` is a
    no-op), SafeChildWatcher attaches a SIGCHLD handler via
    ``loop.add_signal_handler`` -- which only runs when ``set_event_loop`` is
    called on the MAIN thread, and is what makes ``is_active()`` True.  The
    ``kirocrew gateway`` path satisfies this (``asyncio.run`` on the main thread
    before any subprocess spawns).  If a future caller ever drives this loop
    from a non-main thread, ``is_active()`` stays False and the FIRST
    ``create_subprocess_exec`` raises ``RuntimeError`` -- re-evaluate the install
    point then.

    Kernel probe (Linux): on 3.10 ``PidfdChildWatcher()`` does NOT validate
    kernel support -- ``__init__`` only sets ``_loop``/``_callbacks``; the
    ``pidfd_open`` syscall is first issued lazily inside ``add_child_handler``.
    So a bare ``PidfdChildWatcher()`` succeeds on a < 5.3 kernel, gets
    installed, and then the FIRST ``create_subprocess_exec`` raises
    ``OSError(ENOSYS)`` -- breaking all subprocess management instead of falling
    back.  Probe ``os.pidfd_open`` explicitly here and only install
    PidfdChildWatcher when the syscall works.

    Pidfd-unavailable (Linux): the probe raises ``OSError`` on a < 5.3 kernel,
    and ``AttributeError`` when the interpreter's build omits the
    ``os.pidfd_open`` wrapper entirely -- observed on a uv-managed / Clang-built
    CPython 3.12 aarch64 venv even though the running kernel supports pidfd.
    In BOTH cases we must NOT install PidfdChildWatcher (it would ENOSYS on the
    first spawn), but we must ALSO NOT fall back to the default
    ThreadedChildWatcher -- its thread-per-child ``os.waitpid`` reaper storm is
    the exact wedge this function exists to prevent (8 ``_do_waitpid`` threads
    starving the loop past the watchdog's ``exit_after``, killing the gateway
    seconds after startup under a throttling model backend). Instead
    fall through to the SIGCHLD-based SafeChildWatcher, the same watcher the
    macOS path uses.
    """
    # CPython 3.14 removed the child-watcher API (set_child_watcher,
    # PidfdChildWatcher, SafeChildWatcher, and the ThreadedChildWatcher default);
    # the Unix event loop reaps children itself. Bail out BEFORE the Linux pidfd
    # branch below, which references the removed names unconditionally and would
    # raise AttributeError during `kirocrew gateway` startup. This is a true
    # no-op and not a lost mitigation: 3.14's reaper spawns no thread per child,
    # so the loop-starvation wedge cannot recur. Probed via hasattr rather than
    # sys.version_info so a backport/vendored runtime that still ships the API
    # keeps the mitigation.
    if not hasattr(asyncio, "set_child_watcher"):
        return
    if sys.platform == "linux":
        try:
            # Probe real kernel support (pidfd_open: Linux 5.3+) BEFORE installing,
            # because PidfdChildWatcher.__init__ does not -- it would only fail later,
            # per-subprocess, inside add_child_handler.
            fd = os.pidfd_open(os.getpid())
            os.close(fd)
        except (OSError, AttributeError):
            # Kernel too old for pidfd_open (< 5.3), or os.pidfd_open unavailable
            # (e.g. a uv-managed / Clang-built CPython whose build omits the
            # os.pidfd_open wrapper even on a pidfd-capable kernel -- observed on
            # the aarch64 3.12.13 venv interpreter, which raises AttributeError
            # here). Falling through to `return` would leave the default
            # ThreadedChildWatcher in place -- the exact thread-per-child watcher
            # this function exists to avoid. Under a throttling model backend the
            # gateway spawns and reaps kiro-cli/MCP children in bursts, and 8+
            # simultaneous os.waitpid() reaper threads then starve the single
            # event-loop thread past the loop-stall watchdog's exit_after budget,
            # so the gateway is killed seconds after it starts serving. Fall back
            # to the SIGCHLD-based SafeChildWatcher (same mitigation as the macOS
            # path below) instead of returning, so the thread storm cannot recur.
            pass
        else:
            asyncio.set_child_watcher(asyncio.PidfdChildWatcher())
            return
    # Reached on macOS / other non-Linux Unix (no pidfd syscall), AND on Linux
    # when os.pidfd_open is unavailable (see the AttributeError fall-through
    # above).  Replace the default thread-per-child ThreadedChildWatcher with the
    # SIGCHLD-based SafeChildWatcher so a burst of simultaneously-dying
    # kiro-cli/MCP children cannot spawn a thread storm that starves the event
    # loop. SafeChildWatcher reaps only its own tracked children (unlike
    # FastChildWatcher, which reaps every child and would clobber the manual
    # killpg/_kill_escaped_children path) and attaches its SIGCHLD handler when
    # the loop is set on the main thread -- same install point as the pidfd
    # path above.  Guarded so an environment without it (Windows, or a future
    # Python that removed it) silently keeps the default watcher.
    watcher_cls = getattr(asyncio, "SafeChildWatcher", None)
    if watcher_cls is None:
        return
    try:
        asyncio.set_child_watcher(watcher_cls())
    except Exception:
        # Falling back here keeps the thread-per-child ThreadedChildWatcher —
        # the exact thread-storm watcher this function exists to replace — so a
        # silent revert must be visible in gateway.log to explain a recurring
        # loop-stall wedge.
        logger.warning(
            "Could not install SafeChildWatcher; falling back to the default "
            "ThreadedChildWatcher (thread-storm wedge mitigation inactive)",
            exc_info=True,
        )
        return


def _resolve_gateway_args(args: argparse.Namespace) -> dict:
    """Resolve the kwargs for `_gateway()` from parsed CLI args.

    Expands the `--test-mode` bundle (with explicit-flag-wins override
    semantics) and enforces the `--approval yolo` safety rail. On rail
    violation, prints a message to stderr and calls `sys.exit(2)`.
    Returned dict is safe to splat directly into `_gateway()`.
    """
    port = getattr(args, "port", None)
    json_ready = getattr(args, "json_ready", False)
    approval = getattr(args, "approval", None)
    no_open = getattr(args, "no_open", False)
    test_mode = bool(getattr(args, "test_mode", False))
    if test_mode:
        # Bundle defaults; explicit flags above take precedence (they are
        # already populated in the locals when the user passed them).
        if port is None:
            port = "auto"
        if approval is None:
            approval = "reads"
        json_ready = True
        no_open = True

    # Validate --port at parse time so a typo (e.g. `--port AUTO`, `--port abc`,
    # `--port 99999`) fails fast with a clear message instead of crashing
    # mid-startup at `int(self._port_override)` after services are partially
    # initialized.
    if port is not None:
        if str(port).lower() == "auto":
            port = "auto"  # canonicalize for downstream comparisons
        else:
            try:
                port_int = int(port)
            except ValueError:
                print(
                    f"👻 --port must be an integer or 'auto', got {port!r}.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not 1 <= port_int <= 65535:
                print(
                    f"👻 --port {port_int} out of range (1..65535).",
                    file=sys.stderr,
                )
                sys.exit(2)
            port = str(port_int)

    if approval == "yolo":
        home_env = os.environ.get("KIROCREW_HOME", "")
        if not home_env:
            print(
                "👻 --approval yolo refused: KIROCREW_HOME must be explicitly set "
                "to an isolated path (not the default ~/.kiro/crew).",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            home_resolved = Path(home_env).expanduser().resolve()
            # Compare against BOTH default (non-override) gateway homes. Do NOT use
            # config_dir() here: KIROCREW_HOME is already set, so config_dir() would
            # return the override itself and the rail would always fire. We must
            # reject the legacy ~/.kirocrew too, not just ~/.kiro/crew: on an
            # unmigrated or downgraded install the legacy home still holds the LIVE
            # data, so KIROCREW_HOME=~/.kirocrew would otherwise enable unrestricted
            # tool approval against the real gateway home. Mirrors seed.py's
            # _protected_homes().
            protected_homes: set[Path] = set()
            for home in (_default_home(), _legacy_home()):
                try:
                    protected_homes.add(home.resolve())
                except OSError:
                    protected_homes.add(home)
        except OSError as exc:
            print(
                f"👻 --approval yolo refused: failed to resolve KIROCREW_HOME: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        if home_resolved in protected_homes:
            print(
                "👻 --approval yolo refused: KIROCREW_HOME resolves to a main "
                f"gateway home ({home_resolved}). Set KIROCREW_HOME to an isolated "
                "path before re-running.",
                file=sys.stderr,
            )
            sys.exit(2)

    return {
        "no_dashboard": getattr(args, "slack_only", False),
        "no_crons": getattr(args, "no_crons", False),
        "no_tunnel": getattr(args, "no_tunnel", False),
        "no_open": no_open,
        "port_override": port,
        "json_ready": json_ready,
        "approval_mode": approval,
        "test_mode": test_mode,
    }


def _diagnostic_port(gw_kwargs: dict) -> int | None:
    """The port a refused gateway would have bound, for lock-refusal diagnosis.

    Mirrors the gateway's own resolution (``parse_dashboard_url`` on
    ``dashboard.url``, with ``KIROCREW_PORT`` and then ``--port`` overriding it)
    so the message can only ever name the port this process was about to bind.

    ``None`` when there is no single answer -- ``--port auto`` picks an ephemeral
    port and ``--slack-only`` binds nothing -- in which case the refusal message
    omits every port claim rather than asserting one it cannot support.
    """
    if gw_kwargs.get("no_dashboard"):
        return None
    override = gw_kwargs.get("port_override")
    if override is not None:
        return None if str(override).lower() == "auto" else int(override)
    try:
        # Deferred import: ``dashboard.urls`` is a stdlib-only leaf, but
        # importing it executes ``dashboard/__init__`` — keep it out of
        # the module scope ``import kiro_crew.cli`` loads, so non-gateway commands and the MCP stdio
        # servers never touch the dashboard package.
        from kiro_crew.dashboard.urls import parse_dashboard_url

        return parse_dashboard_url(KiroCrewConfig.load().dashboard.url)[1]
    except Exception:
        # Diagnosis only — never let it break the refusal path it decorates.
        return None


def _diagnostic_bind_address() -> str | None:
    """The address this gateway is configured to bind, for lock-refusal diagnosis.

    The lock's serving-holder predicate (``gateway_lock.GatewayLock._serving_verdict``)
    probes a holder of the port for HTTP at THIS address, never at one it
    guesses. Resolved by the gateway's own resolver,
    ``dashboard.urls.bind_address_for``: a valid ``KIROCREW_BIND`` is the bind
    whatever ``local_only`` says, and the two fallbacks that flag chooses between
    (loopback, the v4 wildcard) are probed at the same loopback address, so
    ``local_only=True`` loses nothing. ``None`` only when the resolver itself
    cannot be reached -- diagnosis only, never a reason to break the refusal
    path it decorates; the lock then probes loopback.
    """
    try:
        # Deferred import, for the same reason as ``_diagnostic_port``: importing
        # ``dashboard.urls`` executes ``dashboard/__init__``.
        from kiro_crew.dashboard.urls import bind_address_for

        return bind_address_for(local_only=True)
    except Exception:
        return None
