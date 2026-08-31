"""Kiro Crew CLI — personal AI agent.

Commands:
    kirocrew chat -m "message"    Send a single message
    kirocrew chat                 Interactive chat mode
    kirocrew gateway              Start the Kiro Crew server (dashboard + messaging channels)
    kirocrew gateway --seed NAME  Populate $KIROCREW_HOME from fixture NAME, then start the gateway
    kirocrew status               Show runtime stats
    kirocrew run TASK.md          Run an autonomous task from a spec file
    kirocrew update               Update Kiro Crew via git fetch + rebuild
    kirocrew cron list|add|remove Manage scheduled jobs
    kirocrew spawn run "task"     Spawn a background subagent
    kirocrew spawn list           List subagents
    kirocrew learn add|list|remove Save and manage learned corrections
    kirocrew setup                Interactive setup wizard
    kirocrew doctor               Verify setup
"""

from __future__ import annotations

# Ensure SSL certs are found before any library caches its SSL context.
# The ``kirocrew`` entry-point (console_scripts) bypasses ``__main__.py``,
# so we must run this here as well.
from kiro_crew._ssl_compat import _ensure_ssl_certs

_ensure_ssl_certs()

import argparse
import asyncio
import atexit
import faulthandler
import importlib
import importlib.machinery
import logging
import os
import queue
import shutil
import subprocess
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import NoReturn

from kiro_crew import __version__, cli_help, platform_compat
from kiro_crew.apps import builtins as _builtins_pkg
from kiro_crew.apps.builtins import BUILTIN_NAMES as _BUILTIN_NAMES
from kiro_crew.config import KiroCrewConfig, config_dir, ensure_data_home
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    build_provider_factory,
)
from kiro_crew.config.paths import _default_home, _legacy_home, default_workspace_dir
from kiro_crew.constants import BANNER, MIN_NODE_MAJOR, env_flag_enabled
from kiro_crew.crash_guard import install as _install_crash_guard
from kiro_crew.env import git_build_info
from kiro_crew.gateway_lock import GatewayLock, GatewayLockError
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.store import KnowledgeStore
from kiro_crew.log_redaction import install_log_redaction
from kiro_crew.memory import MemoryStore
from kiro_crew.platform import (
    PlatformCompositionError,
    boot_platform,
    current_context,
)
from kiro_crew.preflight import run_preflight_checks
from kiro_crew.seed import seed_cmd
from kiro_crew.sel import sel
from kiro_crew.service.live_target import maybe_reexec
from kiro_crew.session import SessionManager
from kiro_crew.skills import SkillsLoader

logger = logging.getLogger(__name__)

# Markers that uniquely identify the KiroCrew repo root for project-dir
# auto-detection. ``skills/`` + ``src/kiro_crew/`` is the stable signature:
# ``skills/`` is editable-at-root and ``src/kiro_crew/`` pins this to the
# KiroCrew package repo (not just any directory that happens to contain a
# ``skills/`` folder).
_PROJECT_MARKERS = ("skills", "src/kiro_crew")

# Commands that run agent work in-process and so are candidates for the
# process-isolation jail (CPP JailProvider seam).  The RULE: every command that
# builds an LLM provider factory (``build_provider_factory`` /
# ``SessionManager``) and runs in-process agent/LLM work belongs here — so a
# companion's ``jail=on`` isolation guarantee covers all of them, not just the
# interactive ones.  Today that is ``chat``/``run`` plus ``consolidate``
# (history consolidation LLM inference) and ``eval`` (eval turns + judge).
# ``gateway`` is deliberately EXCLUDED despite being agent-bearing: it is the
# long-lived service whose own execv-based self-update / restart path must not be
# nested inside a jail re-exec (that would exhaust user namespaces); it composes
# isolation by other means.  The public edition's JailProvider has no backend, so
# this set only matters once a companion supplies a real one.
_JAILED_COMMANDS = frozenset({"chat", "run", "consolidate", "eval"})

# Env marker the gate sets BEFORE a successful re-exec into the jail.  The jailed
# CHILD re-runs ``main`` (and so the gate) for the same command; without this
# marker the child would probe the backend again, get ``None`` ("already jailed",
# per the JailProvider contract), and the on-mode floor would refuse to run it —
# a re-entry deadlock.  The gate short-circuits when the marker is PRESENT (it is
# already inside the jail).  A companion's ``maybe_reexec_into_jail`` may rely on
# the core setting this; it is also free to set its own marker — re-entry is
# detected by PRESENCE of a non-empty value (NOT truthiness), so a descriptive
# value like ``"jailed"`` or a namespace id works just as well as the core's "1".
_JAILED_ENV_MARKER = "KIROCREW_JAILED"


def _already_jailed() -> bool:
    """True if we are running inside an already-established jail (marker present).

    Presence-based (any non-empty value), NOT truthiness — this matches both the
    core's own ``"1"`` write and a companion that sets a descriptive marker value,
    so the re-entry guard cannot be defeated by a non-truthy marker.
    """
    return bool(os.environ.get(_JAILED_ENV_MARKER, "").strip())


def _child_argv() -> "list[str]":
    """The argv the jail should re-exec — this same kirocrew invocation.

    Reuses ``agent._resolve_kirocrew_bin`` (the same self-invocation resolver
    kirocrew-core/kirocrew-cron use) so the jailed child inherits its venv
    ``bin/`` walk and ``os.access(X_OK)`` validation — rather than
    re-implementing a bare ``shutil.which`` that misses those cases.
    The resolver returns the bare ``"kirocrew"`` sentinel when it finds no usable
    binary; in that case fall back to ``python -m kiro_crew`` so a non-PATH /
    editable run still re-execs correctly.
    """
    from kiro_crew.agent import _resolve_kirocrew_bin

    exe = _resolve_kirocrew_bin()
    if exe != "kirocrew":  # a resolved, validated absolute path
        return [exe, *sys.argv[1:]]
    return [sys.executable, "-m", "kiro_crew", *sys.argv[1:]]


def _refuse_unjailed(command: str, reason: str) -> NoReturn:
    """Fail closed: ``agent.jail=on`` could not be satisfied → log + ``exit 2``.

    The single on-mode refusal site, so the error code / message / any future
    audit hook stay identical regardless of WHICH on-mode failure tripped it
    (availability probe error vs no re-exec).
    """
    logging.getLogger("kiro_crew").error(
        "agent.jail=on but %s for %r; refusing to run un-jailed", reason, command
    )
    sys.exit(2)


def _jail_reexec_gate(command: str, no_jail_flag: bool) -> None:
    """Give the active edition a chance to re-exec into a process-isolation jail.

    Called once per agent-bearing command (``_JAILED_COMMANDS``) after platform
    boot, before dispatch.  Either returns (run in-process) or terminates the
    process via :func:`sys.exit` — with the jailed child's exit code on a
    successful re-exec, or ``2`` when ``agent.jail=on`` could not be satisfied.

    Fail-closed discipline (``mode == "on"`` means the operator DEMANDED
    isolation, so anything short of a real re-exec must refuse to run un-jailed):

    * Already inside the jail (``KIROCREW_JAILED`` marker set by a prior re-exec)
      → return.  Without this the jailed CHILD would re-probe, get ``None``
      ("already jailed"), and the on-mode floor would deadlock the child.
    * ``off`` (``--no-jail`` or ``KIROCREW_NO_JAIL`` truthy) → early return, no
      probe, run in-process.
    * ``available()`` is the backend-presence probe.  When it returns ``False``
      cleanly (the public Default) the gate is a NO-OP even under ``mode == "on"``
      — exactly as the ``agent.jail`` help text promises, and we never build the
      re-exec argv.  A :class:`PlatformCompositionError` always propagates.  A
      *transient* probe error degrades to "no backend" under ``auto`` but FAILS
      CLOSED under ``on`` (``exit 2``): an on-mode host must not run un-jailed
      just because the presence probe was flaky (availability unknown ≠ absent).
    * With a backend present + ``mode != "off"``, a single fail-closed floor
      governs the re-exec: under ``on`` anything other than a real re-exec (a
      ``None`` return OR a swallowed backend error, both leaving ``rc is None``)
      refuses to run un-jailed.  One check, so the error path and the
      ``None``-return path cannot diverge.

    The mode is re-normalized here (``_normalize_jail``) so a context whose
    ``agent.jail`` was set programmatically (a companion) to an off-spec value is
    handled identically to the load-time path.
    """
    from kiro_crew.config.loader import _normalize_jail

    log = logging.getLogger("kiro_crew")

    # Re-entry guard: if we are already the jailed child (a prior re-exec set the
    # marker), do not re-probe / re-jail — that would deadlock under on-mode.
    # Presence-based (see _already_jailed) so a companion's non-truthy marker
    # value still short-circuits.
    if _already_jailed():
        return

    ctx = current_context()
    jail = ctx.jail

    # ``off`` per-invocation: --no-jail OR the KIROCREW_NO_JAIL env hatch (the
    # documented env-only bypass for wrapper / cron / systemd / CI hosts that
    # cannot inject a flag into argv).  Uses the shared truthy convention
    # (``env_flag_enabled``) — a bare ``=0``/``=false`` does NOT disable the jail,
    # so a typo can't silently bypass isolation.
    if no_jail_flag or env_flag_enabled("KIROCREW_NO_JAIL"):
        return  # disabled this invocation → run in-process (no probe needed)

    mode = _normalize_jail(ctx.cfg.agent.jail)
    if mode == "off":
        return

    # Backend-presence probe.  Tell "returned False cleanly" (no backend → no-op)
    # apart from "probe raised" (availability unknown): the latter must fail
    # CLOSED under on-mode rather than degrade to a no-op.
    try:
        available = jail.available()
    except PlatformCompositionError:
        raise
    except Exception:
        log.debug("jail.available() probe failed", exc_info=True)
        if mode == "on":
            _refuse_unjailed(command, "the jail availability probe errored")
        return  # auto: availability unknown → degrade to in-process
    if not available:
        return  # no backend → clean no-op even under on-mode (public Default)

    # Mark the env BEFORE invoking the backend so the re-exec'd CHILD (which
    # inherits the current environment at exec time) sees the marker and the
    # re-entrant gate short-circuits — without it the child would re-probe, get
    # an "already jailed" None, and deadlock under on-mode.  The ``finally``
    # restores the prior value: if the backend re-execs, this process is replaced
    # and the restore never runs (the child keeps the marker); if it RETURNS
    # (degrade / no-op), we must not leave the marker set, or a later in-process
    # check would wrongly believe it is already jailed.
    _prior_marker = os.environ.get(_JAILED_ENV_MARKER)
    os.environ[_JAILED_ENV_MARKER] = "1"
    try:
        rc = jail.maybe_reexec_into_jail(_child_argv(), mode)
    except PlatformCompositionError:
        raise
    except Exception:
        # A jail backend failure must not brick the CLI in the degrading mode;
        # fall through and run in-process (the always-on sandbox / security
        # controls still apply).  An exception counts as "did not jail" (rc=None)
        # for the single fail-closed check below.
        log.debug("jail re-exec check failed", exc_info=True)
        rc = None
    finally:
        # Only reached when the backend RETURNED (no re-exec replaced us).
        if _prior_marker is None:
            os.environ.pop(_JAILED_ENV_MARKER, None)
        else:
            os.environ[_JAILED_ENV_MARKER] = _prior_marker
    if rc is None:
        if mode == "on":
            _refuse_unjailed(command, "no jail re-exec occurred")
        return
    sys.exit(rc)


def _project_dir_file() -> Path:
    """Return the path to the saved project_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "project_dir"


def _ensure_node(proj_dir: str = "") -> bool:
    """Run ensure-node.sh to guarantee a supported Node. Returns True if node is OK."""
    script = None
    env_dir = os.environ.get("KIROCREW_PROJECT_DIR")
    for candidate in [
        Path(proj_dir) / "ensure-node.sh" if proj_dir else None,
        Path(env_dir) / "ensure-node.sh" if env_dir else None,
        Path(__file__).resolve().parent.parent.parent / "ensure-node.sh",
    ]:
        if candidate and candidate.is_file():
            script = candidate
            break
    if not script:
        return _node_ok()
    try:
        result = subprocess.run(
            ["bash", str(script)],
            timeout=120,
            capture_output=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return _node_ok()


def _node_ok() -> bool:
    """Check if node >= MIN_NODE_MAJOR is available."""
    node = shutil.which("node")
    if not node:
        return False
    try:
        node_ver = subprocess.run(
            ["node", "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        major = int(node_ver.stdout.strip().lstrip("v").split(".")[0])
        return major >= MIN_NODE_MAJOR
    except Exception:
        return False


def _detect_project_dir() -> str | None:
    """Find the project root containing agents/ and skills/.

    Search order:
    1. Walk up from CWD
    2. Read saved path from config_dir()/project_dir (respects KIROCREW_HOME)
    """
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if all((d / m).is_dir() for m in _PROJECT_MARKERS):
            return str(d)
    pdf = _project_dir_file()
    if pdf.is_file():
        saved = pdf.read_text(encoding="utf-8").strip()
        p = Path(saved)
        if p.is_dir() and all((p / m).is_dir() for m in _PROJECT_MARKERS):
            return saved
    return None


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
        # cli.py's module scope so non-gateway commands and the MCP stdio
        # servers never touch the dashboard package (see issue #3504).
        from kiro_crew.dashboard.urls import parse_dashboard_url

        return parse_dashboard_url(KiroCrewConfig.load().dashboard.url)[1]
    except Exception:
        # Diagnosis only — never let it break the refusal path it decorates.
        return None


def _knowledge(args) -> None:
    """``kirocrew knowledge dedup [--apply]`` -- collapse cross-source duplicate docs."""
    if getattr(args, "knowledge_action", None) != "dedup":
        print("Usage: kirocrew knowledge dedup [--apply]")
        return
    apply = bool(getattr(args, "apply", False))
    db_path = default_workspace_dir() / "knowledge" / "knowledge.db"
    if not db_path.exists():
        sel().log_tool_invocation(
            session_key="cli", source="cli", tool_name="knowledge_dedup", outcome="not_configured"
        )
        print("Knowledge Library is not configured (no knowledge.db). Ingest documents first.")
        return
    store = KnowledgeStore(str(db_path))
    try:
        results = dedup_sweep(store, apply=apply)
    finally:
        store.db.close()
    sel().log_tool_invocation(
        session_key="cli",
        source="cli",
        tool_name="knowledge_dedup",
        outcome="applied" if apply else "preview",
        metadata={"duplicate_count": len(results), "apply": apply},
    )
    mode = "APPLIED" if apply else "DRY RUN -- no changes; pass --apply to delete"
    if not results:
        print(f"[{mode}] No cross-source duplicate documents found.")
        return
    verb = "Deleted" if apply else "Would delete"
    print(f"[{mode}] {len(results)} duplicate document(s):\n")
    for r in results:
        print(f"  {verb}: {r['loser']}  ({r['items_deleted']} chunks)")
        print(f"      keep: {r['winner']}   [{r['reason']}]")


def _consolidate_cmd(args) -> None:
    """Force history consolidation (and auto-skill extraction) for sessions."""

    cfg = KiroCrewConfig.load()
    conv_log = ConversationLog()
    conv_log.init()

    session_key = args.session_key
    consolidate_all = getattr(args, "consolidate_all", False)

    if not session_key and not consolidate_all:
        # List path — lightweight, no heavy machinery needed
        found = []
        for f in conv_log._dir.glob("*.jsonl"):
            key = f.stem
            count = conv_log.unconsolidated_count(key)
            if count > 0:
                found.append((key, count))
        if not found:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Sessions with unconsolidated messages ({len(found)}):\n")
        for key, count in sorted(found, key=lambda x: -x[1]):
            print(f"  {key}  ({count} messages)")
        print("\nRun with a session key or --all to consolidate.")
        return

    # Heavy machinery only for actual consolidation
    mem = MemoryStore()
    mem.init()
    skills = SkillsLoader()
    sessions = SessionManager(cfg, provider_factory=build_provider_factory(cfg))

    # vector_store omitted: skill dedup uses SkillsLoader.find_similar() (Jaccard),
    # not vector_store. vector_store is for episodic memory embeddings only.
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=mem,
        sessions=sessions,
        skills_loader=skills,
        auto_skills_enabled=cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=cfg.skills.auto_similarity_threshold,
        approval_required=cfg.skills.approval_required,
        max_auto_skills=cfg.skills.max_auto_skills,
        stale_after_days=cfg.skills.stale_after_days,
        archive_after_days=cfg.skills.archive_after_days,
        generate_scripts=cfg.skills.generate_scripts,
        judge_model=cfg.skills.judge_model,
    )

    async def _run(keys: list[str]) -> None:
        for key in keys:
            try:
                sel().log_api_access(
                    caller="cli",
                    operation="consolidate",
                    outcome="allowed",
                    source="cli",
                    resources=key,
                )
                count = conv_log.unconsolidated_count(key)
                if count < 1:
                    print(f"  {key}: no unconsolidated messages, skipping")
                    continue
                print(f"  {key}: consolidating {count} messages...")
                if await consolidator.consolidate_now(key):
                    print(f"  {key}: done ✓")
                else:
                    print(f"  {key}: skipped (consolidation retry backoff)")
            except Exception:
                logger.debug("consolidate (or SEL) failed for %s", key, exc_info=True)

    if consolidate_all:
        keys = [
            f.stem
            for f in conv_log._dir.glob("*.jsonl")
            if conv_log.unconsolidated_count(f.stem) > 0
        ]
        if not keys:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Consolidating {len(keys)} session(s)...")
        asyncio.run(_run(keys))
    else:
        print(f"Consolidating session: {session_key}")
        asyncio.run(_run([session_key]))

    print("\nDone. Check ~/.kiro/crew/skills/auto/ for new skills.")


def _fd_targets_file(fd: int, path: Path) -> bool:
    """True when *fd* is open on the same inode *path* names.

    Used to detect a detach-spawned gateway: ``_spawn_detached_gateway``
    redirects the child's stdout/stderr INTO ``gateway.log``, and from inside
    the child that redirect is only visible by comparing device/inode numbers.
    Any failure (fd closed, file missing, platform without usable inode
    numbers) answers ``False`` — callers then keep the foreground behavior,
    which is what a false negative degrades to today.
    """
    try:
        st_fd = os.fstat(fd)
        st_path = path.stat()
    except OSError:
        return False
    return (st_fd.st_dev, st_fd.st_ino) == (st_path.st_dev, st_path.st_ino)


def _redirect_fds_to(path: Path, fds: tuple[int, ...] = (1, 2)) -> None:
    """Re-point raw *fds* at *path* (append mode) via ``dup2``.

    After the boot rotation renames ``gateway.log`` → ``gateway.log.prev``,
    a detach-spawned gateway's inherited stdout/stderr still reference the
    RENAMED inode, so raw writes that bypass the logging module (uncaught
    tracebacks, inherited child-process stderr) would land in the rotated
    file instead of the live one. ``dup2`` swaps the descriptors in place;
    ``sys.stdout``/``sys.stderr`` objects keep working because they address
    the fd *number*, not the open file description. Best-effort: on failure
    the fds keep pointing where they already were.
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass  # a broken std stream must not abort gateway boot
    try:
        raw_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError:
        return
    try:
        for fd in fds:
            try:
                os.dup2(raw_fd, fd)
            except OSError:
                pass  # leave this fd as-is; the others may still succeed
    finally:
        os.close(raw_fd)


class _FdTrackingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that re-points raw fds 1/2 after each rollover.

    In detached mode ``_redirect_fds_to`` aims the process's raw
    stdout/stderr at ``gateway.log`` so writes that bypass the logging module
    (uncaught tracebacks, child-process stderr) stay captured. But a
    size-based rollover RENAMES that file: the raw fds keep following the
    renamed inode through ``.1`` → ``.2`` → ``.3`` → unlink, after which raw
    stderr disappears from every retained log. Re-pointing the fds at the
    freshly created ``gateway.log`` inside ``doRollover`` keeps raw-write
    capture continuous across the file's whole retention lifecycle.
    """

    def doRollover(self) -> None:
        super().doRollover()
        _redirect_fds_to(Path(self.baseFilename))


class _CliLogQueueHandler(QueueHandler):
    """Marker subclass so re-entrant setup (and tests) can find the CLI's own
    queue handler among whatever other handlers the process accumulated."""


# Commands that run an asyncio event loop for hours -- the ones the loop-stall
# watchdog guards. Only these need file-handler I/O moved off the calling
# thread, and none of them ever exec-over-self, so the atexit / bounded
# force-exit drains cover every exit path they have. Short-lived CLI verbs
# keep synchronous handlers: several replace the process via ``os.exec*``
# (``logs -f`` -> tail/journalctl, ``config edit`` -> $EDITOR, ``pod exec``),
# which skips atexit and would strand queued records -- and none of them runs
# an event loop, so the queue buys them nothing.
_LONG_LIVED_COMMANDS = {"serve", "gateway", "chat", None}

_LOG_QUEUE_LISTENER: QueueListener | None = None


def _stop_log_queue_listener(timeout: float | None = None) -> None:
    """Stop the log queue listener, draining every queued record to disk.

    Idempotent — registered via ``atexit`` so process shutdown flushes the
    queued tail. Also the deterministic drain point for tests: after this
    returns, every record logged before the call has been written by the
    file handler.

    ``timeout`` bounds the drain for force-exit paths (a second SIGINT/
    SIGTERM hard-exits via ``os._exit``, which skips atexit): the sentinel
    is enqueued and the listener thread joined for at most that long, so a
    wedged disk cannot hang the force exit. When the thread does not finish
    in time, flush/close are skipped — they would block on the same wedged
    handler.
    """
    global _LOG_QUEUE_LISTENER
    listener, _LOG_QUEUE_LISTENER = _LOG_QUEUE_LISTENER, None
    if listener is None:
        return
    try:
        if timeout is None:
            listener.stop()
        else:
            listener.enqueue_sentinel()
            # QueueListener has no bounded stop; join its (private but stable
            # across 3.10-3.14) thread handle against the deadline.
            thread = getattr(listener, "_thread", None)
            if thread is not None:
                thread.join(timeout)
                if thread.is_alive():
                    return  # wedged handler — do not block the force exit
    except Exception:
        return  # interpreter-teardown races must not raise
    for handler in listener.handlers:
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass  # a broken handler must not block shutdown


async def drain_log_queue_before_hard_exit(timeout: float = 2.0) -> None:
    """Flush the queued ``gateway.log`` tail from an ``async`` hard-exit path.

    ``os._exit`` runs no ``atexit`` handler, so the drain registered by
    :func:`_setup_cli_logging` never fires on a path that hard-exits -- every
    record still sitting in the queue is lost, including the ones logged
    immediately before the exit, which are the most diagnostic ones a
    post-mortem has.

    :func:`_stop_log_queue_listener` is a *synchronous* bounded drain (it
    joins the listener thread), so calling it inline from a coroutine would
    park the event loop for up to ``timeout``. Offload it to
    :func:`~kiro_crew.executors.subprocess_executor` -- the pool reserved for
    teardown work that can block on a wedged resource -- under an outer
    deadline, exactly as the restart path already does for the SEL flush. A
    wedged disk therefore delays neither the loop nor the exit.

    Never raises: a hard exit must not be blocked, or replaced, by logging.
    """
    from kiro_crew.executors import subprocess_executor

    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _stop_log_queue_listener, timeout
            ),
            timeout=timeout + 1.0,
        )
    except Exception:
        pass  # includes TimeoutError: reaching the exit is what matters


def _setup_cli_logging(command: str | None, verbose: int) -> None:
    """Configure console echo + persistent ``gateway.log`` logging.

    Foreground invocations get the classic shape: ``basicConfig`` console
    handler on the root logger (stderr) plus a rotating file handler on the
    ``kiro_crew`` logger.

    A detach-spawned gateway (``_spawn_detached_gateway``) arrives with
    stdout/stderr already redirected INTO ``gateway.log``. In that mode the
    console handler would write a second, console-formatted copy (no [PID])
    of every ``kiro_crew`` record into the SAME file the file handler writes
    to — records propagate to root — doubling log volume and halving the
    rotation window. Detected via device/inode comparison, and then:

    - the console echo is skipped entirely;
    - the file handler attaches to the ROOT logger instead, so third-party
      WARNINGs that previously reached the file only through the stderr
      redirect still land, now formatted and PID-stamped;
    - after the boot rotation, fds 1/2 are re-pointed at the live log so raw
      writes (uncaught tracebacks, child stderr) do not land in ``.prev``.

    In BOTH modes, for LONG-LIVED commands (``_LONG_LIVED_COMMANDS``) the file
    handler is not attached to a logger directly: the logger gets a
    ``QueueHandler`` and the file handler lives on a ``QueueListener`` thread,
    so no file I/O (rollover included) ever runs on the event-loop thread.
    Short-lived commands attach the file handler synchronously — they run no
    event loop, and several exec-over-self (skipping atexit), where a queued
    tail would be lost. See the inline comment at the attach site.
    """
    if verbose >= 2:
        level = logging.DEBUG
    elif verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING

    log_file = config_dir() / "gateway.log"
    # Detect BEFORE the boot rotation below: rotation renames the file, and
    # the inode comparison must see the file stderr actually inherited.
    detached = _fd_targets_file(2, log_file)

    if detached:
        # Console echo would double-write into gateway.log — skip it. Set the
        # root level to what basicConfig would have set so third-party
        # loggers gate identically in both modes.
        logging.getLogger().setLevel(logging.WARNING)
    else:
        logging.basicConfig(
            level=logging.WARNING,  # third-party libs stay quiet
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    # Kiro Crew loggers: --verbose CLI flag takes precedence, otherwise
    # fall back to the persistent log_level from config.
    if verbose == 0:
        try:
            _cfg = KiroCrewConfig.load()
            _persisted = _cfg.agent.log_level.upper()
            level = getattr(logging, _persisted, logging.WARNING)
        except Exception:
            pass  # config missing or corrupt — keep default WARNING
    logging.getLogger("kiro_crew").setLevel(level)

    # Persistent file log — respects the configured log_level.
    # On startup, rotate gateway.log → gateway.log.prev so a crash's final
    # lines are never lost.  Only for `gateway` subcommand
    # to avoid renaming the file while the gateway is actively writing.
    # encoding="utf-8" is REQUIRED on Windows: Kiro Crew logs non-ASCII glyphs and
    # the default file encoding there is cp1252, so a RotatingFileHandler without
    # it raises UnicodeEncodeError on the first non-ASCII log record (logging
    # swallows it, but it spams "--- Logging error ---" tracebacks and drops the
    # line). ensure_utf8_console() only fixes the console streams, not this file
    # handler.
    if command == "gateway":
        prev_log = log_file.with_suffix(".log.prev")
        if log_file.exists() and log_file.stat().st_size > 0:
            try:
                log_file.replace(prev_log)
            except OSError:
                pass  # race or permission — keep going
        if detached:
            _redirect_fds_to(log_file)
    # Detached mode uses the fd-tracking subclass: a size-based rollover
    # renames gateway.log, and without re-pointing, the redirected raw fds
    # would follow the renamed inode through .1 → .2 → .3 → unlink, losing
    # later raw stderr from all retained logs.
    handler_cls = _FdTrackingRotatingFileHandler if detached else RotatingFileHandler
    fh = handler_cls(log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    # In detached mode the handler also serves the root logger: cap its level
    # at WARNING so third-party WARNINGs keep flowing even when kiro_crew's
    # own configured level is stricter (kiro_crew records below `level` are
    # already filtered at the kiro_crew logger, so this cannot over-log).
    fh.setLevel(min(level, logging.WARNING) if detached else level)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [PID %(process)d]: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    if detached:
        # Root attach: kiro_crew records arrive once via propagation, and
        # third-party WARNINGs land formatted — replacing what the accidental
        # stderr echo used to provide unformatted.
        target_logger = logging.getLogger()
    else:
        target_logger = logging.getLogger("kiro_crew")

    # Never run file-handler I/O on the caller's thread of a LONG-LIVED
    # command: the gateway invokes this from the thread that will run the
    # asyncio event loop, and an inline emit contends for Handler.lock with
    # every worker thread — while a size-based rollover runs the WHOLE rename
    # chain plus the dup2 fd re-point on the loop. Blocking past the
    # loop-stall watchdog's ``exit_after`` hard-exits the gateway mid-turn
    # and orphans every in-flight subagent. A QueueHandler makes emit a
    # non-blocking put; the QueueListener's thread owns the file handler, so
    # ALL file I/O — ``doRollover()`` and ``_redirect_fds_to()`` included —
    # happens off the loop. Secret redaction is unaffected: it hooks the
    # log-record FACTORY (creation time, producer thread), upstream of any
    # handler.
    #
    # Short-lived commands keep the synchronous handler on purpose: none of
    # them runs an event loop (nothing to stall), and several replace the
    # process via ``os.exec*`` (``logs -f``, ``config edit``, ``pod exec``),
    # which skips atexit — a queue there would strand its tail records, while
    # a sync handler has already written them by the time exec runs. See
    # ``_LONG_LIVED_COMMANDS``.
    if command in _LONG_LIVED_COMMANDS:
        global _LOG_QUEUE_LISTENER
        _stop_log_queue_listener()  # re-entrant call (tests): retire the old thread
        for lgr in (logging.getLogger(), logging.getLogger("kiro_crew")):
            for stale in [h for h in lgr.handlers if isinstance(h, _CliLogQueueHandler)]:
                lgr.removeHandler(stale)  # re-entrant call: orphaned producer
        log_queue: "queue.SimpleQueue[logging.LogRecord]" = queue.SimpleQueue()
        queue_handler = _CliLogQueueHandler(log_queue)
        # Gate at the producer: records the file handler would drop must not
        # transit the queue at all.
        queue_handler.setLevel(fh.level)
        _LOG_QUEUE_LISTENER = QueueListener(log_queue, fh, respect_handler_level=True)
        _LOG_QUEUE_LISTENER.start()
        atexit.register(_stop_log_queue_listener)
        target_logger.addHandler(queue_handler)
    else:
        target_logger.addHandler(fh)

    # Install secret redaction filter — scrubs Bearer tokens from all kiro_crew
    # log output before it reaches any handler. The filter also accepts literal
    # secret values to redact, but none are passed here: wiring resolved vault
    # secret values into the filter is a follow-up PR. Bearer token redaction is
    # active immediately with zero vault I/O.
    if command in _LONG_LIVED_COMMANDS:
        install_log_redaction([])


def _builtin_mcp_server_available(name: str) -> bool:
    """True when ``kiro_crew.apps.builtins.<name>.mcp_server`` resolves.

    ``_BUILTIN_NAMES`` answers two different questions: which builtins get HTTP
    routes (all of them) and which get an ``mcp-<name>`` CLI verb (only those
    that actually ship an ``mcp_server`` module). Gating the verb on this
    predicate keeps the two decoupled — a builtin without the module is simply
    not registered, instead of advertising a command that dies with a raw
    ``ModuleNotFoundError`` traceback (#5901).

    Resolution deliberately uses ``PathFinder`` rather than
    ``importlib.util.find_spec``: ``find_spec`` IMPORTS the parent package as a
    side effect, and every builtin's ``__init__`` pulls in its whole
    ``backend.routes`` chain — which would execute all builtin apps at
    parser-build time on every CLI invocation, including the gateway boot path.
    ``PathFinder`` walks the same import machinery (source, bytecode, extension
    loaders) without executing anything. Worst-case boot cost is bounded by
    ``len(BUILTIN_NAMES)``: one directory listing per builtin package
    (mtime-cached by the import system), independent of user data or profile
    size.
    """
    finder = importlib.machinery.PathFinder
    pkg_spec = finder.find_spec(name, list(_builtins_pkg.__path__))
    if pkg_spec is None or not pkg_spec.submodule_search_locations:
        return False
    return finder.find_spec("mcp_server", list(pkg_spec.submodule_search_locations)) is not None


def main() -> None:
    """Entry point — parse args and dispatch to the appropriate subcommand."""
    # On Windows, force stdout/stderr to UTF-8 BEFORE anything prints — KiroCrew's
    # non-ASCII output otherwise raises UnicodeEncodeError under the cp1252
    # default when stdout is a pipe (detached gateway, KiroCrewHub client). No-op
    # on POSIX. Must be the first statement: the KIROCREW_PORT error below prints
    # non-ASCII glyphs.
    platform_compat.ensure_utf8_console()

    # Clear any INHERITED sandbox-active marker before any argv-wrapping path
    # can run. KIROCREW_SANDBOX_ACTIVE tells the sandbox layer that OS isolation
    # is already active, so it skips re-wrapping (nested-sandbox passthrough).
    # Its ONLY legitimate setter is the namespace launcher's in-sandbox main()
    # — a separate process. A real sandboxed child never runs this CLI
    # entrypoint, so a value present here can only be forged/inherited from the
    # gateway's own environment; trusting it would let an operator env-inject a
    # full sandbox bypass for every agent/tool spawn. Drop it so only the
    # launcher's in-namespace set is ever honored. Its companion tier record
    # KIROCREW_SANDBOX_LEVEL gets the same treatment: a stale inherited value
    # would be read as the ACTIVE tier by a descendant's passthrough and
    # corrupt its downgrade audit.
    os.environ.pop("KIROCREW_SANDBOX_ACTIVE", None)
    os.environ.pop("KIROCREW_SANDBOX_LEVEL", None)

    # Validate KIROCREW_PORT early — fail fast before anything else loads.
    # Range as well as type: an in-range check that lives only in the binder
    # would let `KIROCREW_PORT=70000 kirocrew service install` bake an
    # unbindable port into a service definition and report success, leaving a
    # gateway that dies on every start. Rejecting here keeps ONE policy for
    # every entry point rather than a second one per consumer.
    _raw_port = os.environ.get("KIROCREW_PORT")
    if _raw_port is not None:
        try:
            _port_val = int(_raw_port)
        except ValueError:
            print(
                f"❌ KIROCREW_PORT={_raw_port!r} is not a valid integer.\n"
                f"   Unset it or provide a numeric port (e.g. KIROCREW_PORT=6777).",
                file=sys.stderr,
            )
            sys.exit(1)
        if not 1 <= _port_val <= 65535:
            print(
                f"❌ KIROCREW_PORT={_raw_port!r} is outside the valid port range 1-65535.\n"
                f"   Unset it or provide a bindable port (e.g. KIROCREW_PORT=6777).",
                file=sys.stderr,
            )
            sys.exit(1)

    if not os.environ.get("KIROCREW_PROJECT_DIR"):
        detected = _detect_project_dir()
        if detected:
            os.environ["KIROCREW_PROJECT_DIR"] = detected

    parser = argparse.ArgumentParser(
        prog="kirocrew",
        description="Kiro Crew — personal AI agent",
        usage=cli_help.TOP_USAGE,
        epilog=cli_help.render_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"kirocrew {__version__}")
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase log verbosity (-v INFO, -vv DEBUG)",
    )
    # ``--no-jail`` is shared between the top-level parser and the jailed
    # subparsers (every command in ``_JAILED_COMMANDS`` —
    # chat/run/consolidate/eval — gets ``parents=[_jail_opts]``) via a parent
    # parser, so BOTH ``kirocrew --no-jail <cmd>`` and ``kirocrew <cmd> --no-jail``
    # are accepted (argparse only matches a flag on the parser that declares it).
    # The PARENT copy uses ``default=argparse.SUPPRESS`` so that when the flag is
    # given only at the top level, the subparser does not reset ``no_jail`` back to
    # False (the classic argparse parent/subparser default-override trap).
    _jail_opts = argparse.ArgumentParser(add_help=False)
    _jail_opts.add_argument(
        "--no-jail",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable the process-isolation jail for this invocation (no-op on "
        "the public edition, which has no jail backend).",
    )
    parser.add_argument(
        "--no-jail",
        action="store_true",
        help="Disable the process-isolation jail for this invocation (no-op on "
        "the public edition, which has no jail backend).",
    )

    # ``help=SUPPRESS`` drops argparse's own flat subcommand block — ~40 commands
    # in registration order, with the three anybody needs first buried in the
    # middle — in favour of the grouped listing rendered into ``epilog`` above.
    # It also drops the placeholder from the usage line, hence the explicit
    # ``usage=``. ``metavar`` is what an "invalid choice" error names, and
    # ``prog`` must be pinned because argparse otherwise derives each
    # subcommand's prog from the parent's ``usage=`` string, which would prefix
    # every ``kirocrew <cmd> --help`` with the whole top-level usage line.
    sub = parser.add_subparsers(
        dest="command",
        metavar="<command>",
        help=argparse.SUPPRESS,
        prog="kirocrew",
    )

    # Helper for commands with examples
    _fmt = argparse.RawDescriptionHelpFormatter

    # chat
    chat_parser = cli_help.add_command(
        sub,
        "chat",
        epilog="""
Examples:
  kirocrew chat                      # Interactive mode
  kirocrew chat -m 'check my CRs'    # Single message
  kirocrew chat --model claude-opus  # Use specific model
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    chat_parser.add_argument("-m", "--message", help="Single message (non-interactive)")
    chat_parser.add_argument("--model", help="Model to use (default: from config)")
    chat_parser.add_argument("--agent", help="Agent to use (default: from config)")

    # doctor
    _doctor_parser = cli_help.add_command(sub, "doctor")
    _doctor_parser.add_argument(
        "--bundle",
        action="store_true",
        help="Collect logs + crash reports into a redacted diagnostics zip",
    )

    # gateway
    gw_parser = cli_help.add_command(sub, "gateway")
    gw_parser.add_argument(
        "--slack-only",
        action="store_true",
        help="Slack-only mode — skip dashboard web server and SSH tunnel instructions",
    )
    gw_parser.add_argument(
        "--no-crons",
        action="store_true",
        help="Skip cron scheduler — use when another instance handles cron execution",
    )
    gw_parser.add_argument(
        "--no-tunnel",
        action="store_true",
        help=(
            "Never publish a tunnel — this process refuses to start or provision "
            "one for its whole life, whatever tunnel.enabled says. Scoped to "
            "TUNNELS: it does not change where the dashboard binds, so a config "
            "that widens dashboard.url off loopback still does. Use for an "
            "instance that must not publish a tunnel (a Dev Fleet pod passes "
            "this); reach it with `ssh -L` instead."
        ),
    )
    gw_parser.add_argument(
        "--seed",
        metavar="FIXTURE",
        help=(
            "Seed $KIROCREW_HOME from the named fixture BEFORE starting the "
            "gateway (dev tool). Fixture must exist under "
            "src/kiro_crew/tests_fixtures/. The gateway then runs normally "
            "against the populated $KIROCREW_HOME. Refuses when "
            "$KIROCREW_HOME is the main gateway home (~/.kiro/crew) or "
            "when the target is non-empty (use --seed-replace to wipe + re-seed)."
        ),
    )
    gw_parser.add_argument(
        "--seed-replace",
        action="store_true",
        help=(
            "When used with --seed, wipe $KIROCREW_HOME (rmtree) before "
            "copying the fixture. Ignored without --seed. Does NOT "
            "override the main-gateway-home rail — ~/.kiro/crew is refused "
            "regardless."
        ),
    )
    gw_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open the dashboard URL in the default browser on startup",
    )
    gw_parser.add_argument(
        "--port",
        metavar="PORT",
        help=(
            "Override the dashboard port. Pass an integer (e.g. --port 9999) "
            "for a fixed port, or --port auto to bind to an ephemeral port "
            "(OS-assigned). When omitted, falls back to the value in config "
            "(dashboard.url)."
        ),
    )
    gw_parser.add_argument(
        "--json-ready",
        action="store_true",
        help=(
            "Print a single line `KIROCREW_READY:{...}` to stdout once the "
            "dashboard is bound. Payload includes port, token, pid, and "
            "KIROCREW_HOME. Used by test harnesses to discover the bound "
            "ephemeral port and authenticate without polling. NOTE: the "
            "token grants gateway access for up to 20 hours — treat the "
            "READY line as sensitive and do not commit captured stdout to "
            "shared logs."
        ),
    )
    gw_parser.add_argument(
        "--approval",
        choices=["reads", "yolo", "interactive"],
        help=(
            "Default approval mode for tool invocations. 'reads' auto-approves "
            "read-only tools (read/list/get/search/* prefixes); 'yolo' "
            "auto-approves all tools (refused unless KIROCREW_HOME is "
            "explicitly set to a non-default location); 'interactive' uses "
            "the standard Slack/dashboard prompt flow. When omitted, current "
            "interactive behavior is preserved."
        ),
    )
    gw_parser.add_argument(
        "--test-mode",
        action="store_true",
        help=(
            "Convenience alias for --port auto --no-open --json-ready "
            "--approval reads. An explicit --port or --approval value "
            "overrides the bundle's default (e.g. --test-mode --approval "
            "yolo uses yolo). The boolean flags --no-open and --json-ready "
            "are forced on by --test-mode and cannot be opted out of."
        ),
    )

    # setup
    setup_parser = cli_help.add_command(sub, "setup")
    setup_parser.add_argument(
        "--agent-only",
        action="store_true",
        help="Only install the agent config, skip the interactive wizard steps",
    )
    setup_parser.add_argument(
        "--slack",
        action="store_true",
        help=(
            "Also run the guided Slack credential setup (messaging channels are "
            "otherwise connected later from the dashboard); ignored with --agent-only"
        ),
    )
    setup_parser.add_argument(
        "--whatsapp",
        action="store_true",
        help=(
            "Also run the guided WhatsApp setup: report the optional 'whatsapp' "
            "extra and the pairing state, then enable the channel; ignored with "
            "--agent-only"
        ),
    )
    setup_parser.add_argument(
        "--electron-only",
        action="store_true",
        help="Only install the Kiro Crew desktop app (macOS), skip other setup",
    )
    setup_parser.add_argument(
        "--clean",
        action="store_true",
        help="Fresh install — don't merge MCP servers/tools from existing config",
    )

    # manifest
    manifest_parser = cli_help.add_command(sub, "manifest")
    manifest_parser.add_argument("--alias", help="Override alias (default: auto-detect)")
    manifest_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    manifest_parser.add_argument(
        "--url",
        action="store_true",
        help="Print a one-click Slack app creation URL",
    )

    # cron
    cron_parser = cli_help.add_command(
        sub,
        "cron",
        epilog="""
Examples:
  kirocrew cron list
  kirocrew cron add 'daily-status' 'show status' --every 86400
  kirocrew cron add 'weekday-9am' 'check tickets' --cron '0 9 * * MON-FRI' --approval-mode auto
  kirocrew cron add 'c360-check' 'check pipeline' --every 600 --agent customer360-code-agent
  kirocrew cron update <job-id> --approval-mode auto
  kirocrew cron update <job-id> --agent oncall-agent
  kirocrew cron remove <job-id>
""",
        formatter_class=_fmt,
    )
    cron_sub = cron_parser.add_subparsers(dest="cron_action")
    cron_sub.add_parser("list", help="List cron jobs")
    cron_add = cron_sub.add_parser("add", help="Add a cron job")
    cron_add.add_argument("name", help="Job name")
    cron_add.add_argument("message", help="Message to send to agent")
    cron_add.add_argument("--every", type=int, help="Interval in seconds")
    cron_add.add_argument(
        "--cron", dest="cron_expr", help='Cron expression (e.g. "0 9 * * MON-FRI")'
    )
    cron_add.add_argument("--channel", help="Slack channel ID to post results to")
    cron_add.add_argument(
        "--agent",
        dest="agent",
        default="",
        help="Agent name for this job (e.g. 'customer360-code-agent'). "
        "Empty or omitted uses the default kirocrew agent.",
    )
    cron_add.add_argument(
        "--silent",
        action="store_true",
        help="Suppress auto-delivery; agent controls notifications",
    )
    cron_add.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto"],
        default="",
        help='Tool approval mode ("auto" to auto-approve all tools)',
    )
    cron_update = cron_sub.add_parser("update", help="Update a cron job")
    cron_update.add_argument("job_id", help="Job ID to update")
    cron_update.add_argument("--name", help="New job name")
    cron_update.add_argument("--message", help="New message")
    cron_update.add_argument("--every", type=int, dest="every_secs", help="New interval in seconds")
    cron_update.add_argument(
        "--timeout-secs",
        type=int,
        dest="timeout_secs",
        default=None,
        help="Per-wake execution budget in seconds (1..86400, default 1800)",
    )
    cron_update.add_argument("--cron", dest="cron_expr", help="New cron expression")
    cron_update.add_argument("--channel", help="New channel ID")
    cron_update.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="New agent name (empty string resets to default kirocrew agent)",
    )
    cron_update.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto", "default"],
        default=None,
        help='Tool approval mode ("auto" to auto-approve, "default" to reset)',
    )
    cron_rm = cron_sub.add_parser("remove", help="Remove a cron job")
    cron_rm.add_argument("job_id", help="Job ID to remove")
    cron_pause = cron_sub.add_parser("pause", help="Pause a cron job")
    cron_pause.add_argument("job_id", help="Job ID to pause")
    cron_resume = cron_sub.add_parser("resume", help="Resume a cron job")
    cron_resume.add_argument("job_id", help="Job ID to resume")
    cron_trigger = cron_sub.add_parser("trigger", help="Trigger a cron job immediately")
    cron_trigger.add_argument("job_id", help="Job ID to trigger")

    cron_adopt = cron_sub.add_parser(
        "adopt",
        help="Give a cron job an owning chat session, so that session can manage it and receives its results",
    )
    cron_adopt.add_argument("job_id", help="Job ID to adopt")
    adopt_target = cron_adopt.add_mutually_exclusive_group(required=True)
    adopt_target.add_argument(
        "--session-of",
        metavar="SESSION",
        help='Dashboard slot name ("chat-3-1712793600") or a fully-qualified '
        'session key ("dashboard:chat-3-1712793600"); see `kirocrew cron list`',
    )
    adopt_target.add_argument(
        "--release",
        action="store_true",
        help="Clear the owning session, returning the job to CLI/dashboard-only management",
    )

    cron_preview = cron_sub.add_parser(
        "preview",
        help="Run a script cron locally with real MCP tools; notifications are captured and printed instead of delivered",
    )
    cron_preview.add_argument(
        "script", help="Script path in module:function format (e.g. ~/.kiro/crew/crons/my.py:run)"
    )
    cron_preview.add_argument("--message", "-m", default="", help="ctx.message value")
    cron_preview.add_argument(
        "--env", "-e", action="append", metavar="K=V", help="Extra env vars (repeatable)"
    )

    # spawn
    spawn_parser = cli_help.add_command(
        sub,
        "spawn",
        epilog="""
Examples:
  kirocrew spawn run 'check my open CRs'        # Wait for result
  kirocrew spawn run --async 'analyze logs'     # Fire-and-forget
  kirocrew spawn list                           # Show active subagents
""",
        formatter_class=_fmt,
    )
    spawn_sub = spawn_parser.add_subparsers(dest="spawn_action")
    spawn_run = spawn_sub.add_parser("run", help="Spawn a subagent")
    spawn_run.add_argument("task", help="Task for the subagent")
    spawn_run.add_argument(
        "--async",
        dest="fire_and_forget",
        action="store_true",
        help="Fire-and-forget (don't wait for result)",
    )
    spawn_sub.add_parser("list", help="List subagents")
    spawn_parser.add_argument("--port", type=int, default=DASHBOARD_PORT, help="Dashboard port")

    # run (autonomous task runner)
    run_parser = cli_help.add_command(
        sub,
        "run",
        epilog="""
Examples:
  kirocrew run TASK.md                  # Run task with auto-resume
  kirocrew run TASK.md --fresh          # Start from scratch
  kirocrew run TASK.md --no-test        # Skip test verification
  kirocrew run TASK.md --timeout 3600   # 1 hour timeout
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    run_parser.add_argument("spec", help="Path to the spec/task file (e.g. TASK.md)")
    run_parser.add_argument(
        "--name",
        default="",
        help="Human-readable task name (auto-derived from spec if omitted)",
    )
    run_parser.add_argument(
        "--no-test",
        action="store_true",
        help="Skip running build/test verification after each step",
    )
    run_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoint, start task from scratch",
    )
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Global timeout in seconds (0 = no limit)",
    )
    run_parser.add_argument(
        "--port", type=int, default=DASHBOARD_PORT, help="Dashboard port for status"
    )

    # snapshot / restore
    snap_parser = cli_help.add_command(sub, "snapshot")
    snap_parser.add_argument(
        "output_dir",
        nargs="?",
        default=None,
        help="Directory to write the snapshot into (default: the data home's snapshots dir)",
    )
    snap_parser.add_argument("--keep", type=int, default=7, help="Keep N most recent snapshots")
    snap_parser.add_argument(
        "--list", action="store_true", dest="list_snapshots", help="List existing snapshots"
    )
    snap_parser.add_argument(
        "--allow-unpinned-staging",
        action="store_true",
        dest="allow_unpinned",
        help=(
            "Stage by path name on a platform that cannot open a directory relative to "
            "a descriptor (Windows). Without this the snapshot is refused there rather "
            "than taken with a traversal an ancestor swap could redirect. The archive's "
            "MANIFEST.json records that it was staged unpinned."
        ),
    )
    snap_parser.add_argument(
        "--components",
        default=None,
        help="Comma-separated components to include (default: all); see restore --list-components",
    )
    snap_parser.add_argument(
        "--purpose",
        default="backup",
        choices=("backup", "share"),
        help=(
            "backup = restoring onto a host you control, keeps credential-bearing "
            "components (the default, and the only one that produces a bundle today); "
            "share = leaves your control, so it carries only components certified free "
            "of credential material — no component is certified yet, so this currently "
            "refuses rather than emitting a bundle that has not been checked"
        ),
    )

    # Retired, and defined only so it fails with a pointer instead of a bare argparse
    # error. Dropping it entirely would still fail loudly ("unrecognized arguments"), but
    # an operator whose muscle memory or old cron line still says `--to s3://bucket/prefix`
    # is better served by being told where that capability went than by exit 2.
    snap_parser.add_argument("--to", default=None, help=argparse.SUPPRESS)

    rest_parser = cli_help.add_command(sub, "restore")
    rest_parser.add_argument("snapshot", nargs="?", help="Path to snapshot .tar.gz")
    rest_parser.add_argument(
        "--mode",
        choices=("replace", "merge"),
        help="replace = overwrite existing state, merge = keep it and add (default: auto-detect)",
    )
    rest_parser.add_argument(
        "--dry-run", action="store_true", help="Preview what would be restored, write nothing"
    )
    rest_parser.add_argument("--components", help="Comma-separated components to restore")
    rest_parser.add_argument(
        "--list-components", action="store_true", help="List restorable component names and exit"
    )
    rest_parser.add_argument(
        "--force", action="store_true", help="Restore even if gateway is running"
    )
    rest_parser.add_argument(
        "--allow-unpinned-staging",
        action="store_true",
        dest="allow_unpinned",
        help=(
            "Restore by path name on a platform that cannot open a directory relative "
            "to a descriptor (Windows). Without this the restore is refused there "
            "rather than run with a destination an ancestor swap could redirect."
        ),
    )

    # security
    sec_parser = cli_help.add_command(sub, "security")

    # eval (benchmark harness)
    eval_parser = cli_help.add_command(
        sub,
        "eval",
        epilog="""
Examples:
  kirocrew eval                         # smoke test (~30s)
  kirocrew eval memory_recall_basic     # specific scenario
  kirocrew eval --all                   # all scenarios (slow)
""",
        formatter_class=_fmt,
        parents=[_jail_opts],
    )
    eval_parser.add_argument(
        "scenarios",
        nargs="*",
        default=[],
        help="Scenario names to run (without extension). Default: smoke_test",
    )
    eval_parser.add_argument(
        "--all", action="store_true", dest="all_scenarios", help="Run all scenarios"
    )
    eval_parser.add_argument("--judge", action="store_true", help="Enable LLM judge scoring")

    sec_sub = sec_parser.add_subparsers(dest="sec_action")
    sec_sub.add_parser("audit", help="Scan conversation history for suspicious tool usage")
    sec_sub.add_parser("deny-list", help="Show active deny patterns")
    sel_parser = sec_sub.add_parser("events", help="Show recent security event log entries")
    sel_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of entries")
    # A count alone cannot express "what happened around 14:05" — on a busy log
    # 6000 entries reached only 15 minutes back, so answering a question about a
    # two-hour-old event meant pulling ~90k entries and filtering by hand
    # (issue #4843). Reading the file directly is correctly refused by the
    # credential-path gate, so the time selector has to live here.
    _time_help = (
        "a relative age (30m, 2h, 7d) or an ISO 8601 instant " "(2026-08-21, 2026-08-21T04:00:00Z)"
    )
    sel_parser.add_argument("--since", default="", help=f"Only entries at or after {_time_help}")
    sel_parser.add_argument("--until", default="", help=f"Only entries before {_time_help}")
    sec_sub.add_parser("verify", help="Verify security event log HMAC integrity")

    # policy — governance model inspection (read-only; MCP-safe)
    tn_parser = cli_help.add_command(sub, "tailnet")
    tn_sub = tn_parser.add_subparsers(dest="tailnet_action")
    for _tn_name, _tn_help in (
        ("status", "Show whether the dashboard is published and trusted on your tailnet"),
        ("up", "Publish the dashboard on your tailnet and trust its origin"),
        ("down", "Stop publishing the dashboard on your tailnet"),
    ):
        _tn = tn_sub.add_parser(_tn_name, help=_tn_help)
        # The escape hatch for when discovery cannot decide. Publishing has to front
        # the port the gateway is ACTUALLY on: if the run marker is unreadable (or
        # several gateways are up, where it deliberately refuses) the fallback is the
        # configured port -- which, if the gateway moved because that port was taken,
        # now belongs to some OTHER local service that would get exposed. Naming the
        # port outranks every heuristic.
        _tn.add_argument(
            "--port",
            type=int,
            default=None,
            help="Dashboard port to publish (default: discover the running gateway)",
        )

    tel_parser = cli_help.add_command(sub, "telemetry")
    tel_sub = tel_parser.add_subparsers(dest="telemetry_action")
    tel_sub.add_parser(
        "status", help="Show exactly what the anonymous beacon sends (and whether it will)"
    )
    tel_sub.add_parser("disable", help="Turn the anonymous beacon off permanently")
    tel_sub.add_parser("enable", help="Turn the anonymous beacon back on")

    policy_parser = cli_help.add_command(sub, "policy")
    policy_sub = policy_parser.add_subparsers(dest="policy_action")
    policy_show = policy_sub.add_parser(
        "show", help="Show the effective enterprise security policy"
    )
    policy_show.add_argument(
        "--ids",
        action="store_true",
        # NOT named --verbose: the top-level parser already defines --verbose/-v
        # as an int `count` (log level). A same-named store_true on this
        # subparser would collide in the merged Namespace via argparse's
        # parent/subparser default-override gotcha (see the --no-jail comment
        # above) -- whichever parser's default applies last silently
        # overwrites the other's value.
        help="List each denied-command category's rule ids (default: counts only)",
    )
    policy_sub.add_parser("validate", help="Validate the policy + all profiles (load-check)")
    explain_parser = policy_sub.add_parser(
        "explain", help="Explain a tool/scope decision for a surface"
    )
    explain_parser.add_argument("scope", help="Governed scope, e.g. 'commands' or 'mcp'")
    explain_parser.add_argument("item", help="The item to evaluate, e.g. 'git push origin'")
    explain_parser.add_argument(
        "--session-key", default="cli_chat", help="Surface session key (default: cli_chat)"
    )
    explain_parser.add_argument("--agent", default="", help="Agent name (optional)")
    explain_parser.add_argument("--app", default="", help="App slug (optional)")
    profile_show = policy_sub.add_parser("profile", help="Show a profile by name")
    profile_show.add_argument("name", help="Profile file stem (without .json)")
    policy_sub.add_parser(
        "source", help="Show whether this host fetches its policy from a central source"
    )
    policy_fetch = policy_sub.add_parser(
        "fetch", help="Fetch the central policy now and apply it if it is usable"
    )
    policy_fetch.add_argument(
        "--force",
        action="store_true",
        # Skips the conditional-request validators. An operator running this by
        # hand wants to prove the round trip end to end; a 304 tells them nothing
        # about whether the document they just published reads correctly.
        help="Ignore the cached validators and re-download the whole document",
    )

    register_perf_parser(sub)
    register_bench_parser(sub)
    register_desktop_parser(sub)

    kn_parser = cli_help.add_command(sub, "knowledge")
    kn_sub = kn_parser.add_subparsers(dest="knowledge_action")
    kn_dedup = kn_sub.add_parser(
        "dedup", help="Collapse cross-source duplicate documents (dry-run unless --apply)"
    )
    kn_dedup.add_argument(
        "--apply",
        action="store_true",
        help="Apply the deletions (default: dry-run preview that changes nothing)",
    )

    # secrets — encrypted vault maintenance. Only the migration importer lives
    # here; the set/list/rm surface is owned by a separate change.
    secrets_parser = sub.add_parser("secrets", help="Encrypted secret vault")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_action")
    secrets_import = secrets_sub.add_parser(
        "import",
        help="Migrate plaintext .env credentials into the vault (dry-run unless --apply)",
    )
    secrets_import.add_argument(
        "--apply",
        action="store_true",
        help="Store the secrets and rewrite .env to secret:// refs (default: dry-run)",
    )

    # pod — isolated, throwaway, full-stack test instances per worktree (kubectl-style)
    pod_parser = cli_help.add_command(sub, "pod")
    pod_sub = pod_parser.add_subparsers(
        dest="pod_action",
        metavar="{up,down,ls,prune,status,token,url,scenarios,api,logs,exec,provision,install}",
    )
    pod_up = pod_sub.add_parser("up", help="Schedule an isolated pod for a worktree")
    pod_up.add_argument("name", help="Worktree name")
    pod_up.add_argument("--json", action="store_true", help="Emit {base_url, token, port} as JSON")
    pod_up.add_argument(
        "--provision",
        action="store_true",
        help="Provision (venv + SPA dist) if needed before bringing the pod up",
    )
    pod_up.add_argument("--ttl", default="2h", help="Token TTL (default: 2h)")
    pod_up.add_argument(
        "--seed",
        default="",
        help=(
            "Pre-populate the isolated home. A bare NAME is a shipped seed scenario "
            "and populates the whole home; unknown names are refused with the "
            "available list. A PATH (a separator or leading ~ or .) contributes only "
            "its sanitized config.json. Populated homes are never re-seeded."
        ),
    )
    pod_up.add_argument(
        "--approval",
        # Literal mirrors kiro_crew.pod.runtime.APPROVAL_MODES, which is the
        # enforcement point; this parser imports no pod module at startup.
        choices=["reads", "yolo", "interactive"],
        help=(
            "Approval mode the pod's gateway boots with, forwarded to "
            "`kirocrew gateway --approval`. Persisted per pod so it survives a "
            "service-manager restart. Omit to leave the gateway's own default in "
            "force, which resolves from config agent.approval_mode (default: "
            "auto). Applies at boot, so re-up a stopped pod to change it."
        ),
    )
    pod_up.add_argument(
        "--crons",
        action="store_true",
        help=(
            "Run the pod's cron scheduler. Pods boot with --no-crons by default. "
            "Without --seed the HOME starts with no cron definitions; a named "
            "scenario may provide them. Persisted per pod; applies at boot."
        ),
    )
    pod_down = pod_sub.add_parser("down", help="Evict a pod (zero residue)")
    pod_down.add_argument("name", help="Worktree name")
    pod_ls = pod_sub.add_parser("ls", help="List running pods")
    pod_ls.add_argument("--json", action="store_true", help="Emit rows as JSON")
    pod_prune = pod_sub.add_parser(
        "prune", help="Reclaim orphaned pod HOMEs in bulk (the N-at-once `pod down`)"
    )
    pod_prune.add_argument(
        "--older-than",
        dest="older_than",
        default="3d",
        help=(
            "Only reclaim orphans whose last activity is older than this "
            "(e.g. 3d, 12h, 30m, 45s). Default: 3d, so a freshly-crashed HOME "
            "an operator may still be debugging is kept. Use --all to reclaim "
            "regardless of age."
        ),
    )
    pod_prune.add_argument(
        "--all",
        dest="prune_all",
        action="store_true",
        help="Reclaim ALL orphans regardless of age (overrides --older-than)",
    )
    pod_prune.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Classify and print what would be reclaimed without deleting anything",
    )
    pod_prune.add_argument("--json", action="store_true", help="Emit per-name results as JSON")
    pod_status = pod_sub.add_parser("status", help="Up/down + health for one pod")
    pod_status.add_argument("name", help="Worktree name")
    pod_status.add_argument("--json", action="store_true", help="Emit status as JSON")
    pod_token = pod_sub.add_parser("token", help="(Re)mint a dashboard token for a running pod")
    pod_token.add_argument("name", help="Worktree name")
    pod_token.add_argument("--ttl", default="2h", help="Token TTL (default: 2h)")
    pod_url = pod_sub.add_parser("url", help="Print a pod's base URL")
    pod_url.add_argument("name", help="Worktree name")
    pod_scenarios = pod_sub.add_parser(
        "scenarios",
        help="List the seed scenarios `pod up --seed <scenario>` accepts",
    )
    pod_scenarios.add_argument("--json", action="store_true", help="Emit rows as JSON")
    pod_api = pod_sub.add_parser(
        "api",
        help="Call a running pod's HTTP API with its own token",
        description=(
            "Make one authenticated request against a running pod and print a "
            "fixed-key JSON document: {name, method, path, status, ok, body}. "
            "GET and HEAD are permitted by default; other methods require "
            "--allow-write."
        ),
        epilog=(
            "Examples:\n"
            "  kirocrew pod api my-wt GET sessions\n"
            "  kirocrew pod api my-wt GET /api/health\n"
            '  kirocrew pod api my-wt POST config --data \'{"key":"agent.model"}\' '
            "--allow-write\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    pod_api.add_argument("name", help="Worktree name")
    pod_api.add_argument(
        "method",
        # No `choices=`: argparse would reject an unrecognised method with its own
        # usage prose on stderr and exit 2, escaping the fixed-key JSON envelope
        # this command promises on EVERY exit. `pod.runtime.pod_api` validates the
        # method against API_METHODS and raises PodError, which the envelope
        # reports as a status-0 document an agent can parse. `type=str.upper` stays
        # so a lowercase method still normalises.
        type=str.upper,
        metavar="METHOD",
        help="HTTP method: GET, HEAD, POST, PUT, PATCH or DELETE (case-insensitive)",
    )
    pod_api.add_argument(
        "path",
        help=(
            "API path; /api/ is prepended when absent. Existing query parameters "
            "are preserved, but caller-supplied token parameters are refused."
        ),
    )
    pod_api.add_argument(
        "--data",
        default="",
        help="Request body, sent verbatim as application/json",
    )
    pod_api.add_argument(
        "--allow-write",
        action="store_true",
        help="Permit POST, PUT, PATCH, or DELETE (off by default)",
    )
    pod_logs = pod_sub.add_parser("logs", help="Tail a pod's journal")
    pod_logs.add_argument("name", help="Worktree name")
    pod_logs.add_argument("-n", "--lines", type=int, default=50, help="Lines to tail (default: 50)")
    pod_exec = pod_sub.add_parser(
        "exec",
        help="Run a kirocrew command against a pod, using the pod's own binary and data",
    )
    pod_exec.add_argument("name", help="Worktree name")
    # REMAINDER so the pod's own flags (--json, -n, --ttl …) reach the child
    # instead of being claimed by this parser.
    pod_exec.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        metavar="-- ARGS",
        help="Command to run in the pod, e.g. `-- status` or `-- cron list`",
    )
    pod_prov = pod_sub.add_parser("provision", help="Build a worktree's venv + SPA dist")
    pod_prov.add_argument("name", help="Worktree name")
    pod_prov.add_argument(
        "--venv-only", action="store_true", help="Build only the venv (skip the slow dist)"
    )
    pod_sub.add_parser("install", help="Lay down the systemd --user template unit (once)")
    # Hidden verbs: `_run` is the unit's ExecStart body; `_cleanup` is the
    # single-name manual reclaim (teardown itself lives on the `down` path).
    # Registered without `help=` so they stay out of `pod --help` while remaining
    # dispatchable (the metavar above also omits them from the usage line).
    pod_run = pod_sub.add_parser("_run")
    pod_run.add_argument("name")
    pod_cleanup = pod_sub.add_parser("_cleanup")
    pod_cleanup.add_argument("name")

    update_parser = cli_help.add_command(sub, "update")
    update_parser.add_argument(
        "action",
        nargs="?",
        choices=["approve"],
        help="approve: approve a pending in-app update armed from the dashboard",
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Discard local commits when a git checkout has diverged from its "
            "upstream (git installs only)"
        ),
    )

    # stop
    stop_parser = cli_help.add_command(sub, "stop")
    stop_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Dashboard port (default: resolved from KIROCREW_PORT env or "
            "dashboard.url config). When passed explicitly, bypasses the "
            "systemd/launchd service short-circuit and SIGTERMs the gateway "
            "bound to that port — use this for parallel dev gateways on a "
            "non-default port."
        ),
    )

    # restart — service-aware: restarts the systemd/launchd service if active,
    # otherwise SIGTERMs the foreground gateway and respawns it detached so the
    # shell returns immediately. Mirrors `stop`.
    restart_parser = cli_help.add_command(sub, "restart")
    restart_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Dashboard port (default: resolved from KIROCREW_PORT env or "
            "dashboard.url config). When passed explicitly, bypasses the "
            "systemd/launchd service short-circuit and restarts the gateway "
            "bound to that port — use this for parallel dev gateways on a "
            "non-default port."
        ),
    )

    # service — install/uninstall/status as a system-level systemd unit (Linux,
    # /etc/systemd/system/, requires sudo) or launchd LaunchAgent (macOS,
    # ~/Library/LaunchAgents/, no sudo) so the gateway survives SSH disconnect,
    # auto-restarts on crash, and auto-starts on boot.
    svc_parser = cli_help.add_command(sub, "service")
    svc_sub = svc_parser.add_subparsers(dest="service_action")
    svc_sub.add_parser("install", help="Install and start the gateway service (sudo on Linux)")
    svc_sub.add_parser("uninstall", help="Stop and remove the gateway service (sudo on Linux)")
    svc_sub.add_parser("status", help="Show service status (systemctl/launchctl)")

    # sandbox — the AppArmor grant a DIRECT launch needs. `service install`
    # already installs a named profile that systemd applies to its unit, but a
    # double-clicked AppImage has no unit: nothing transitions it into a profile,
    # so the agent sandbox fails closed on every spawn. These subcommands attach
    # the same single `userns` grant to the app's own executable path, which the
    # kernel applies at exec time without any privileged transition.
    sbx_parser = cli_help.add_command(
        sub,
        "sandbox",
        epilog="""
Examples:
  kirocrew sandbox status                      # is THIS launch covered?
  kirocrew sandbox install-profile             # attach to $APPIMAGE (sudo)
  kirocrew sandbox install-profile --path P    # attach to an explicit executable
  kirocrew sandbox remove-profile              # unload and delete it (sudo)

Only needed on hosts with kernel.apparmor_restrict_unprivileged_userns=1
(Ubuntu 23.10+ and derivatives). Everywhere else these are no-ops.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sbx_sub = sbx_parser.add_subparsers(dest="sandbox_action")
    sbx_install = sbx_sub.add_parser(
        "install-profile",
        help="Attach the userns AppArmor profile to this app (sudo on Linux)",
    )
    sbx_install.add_argument(
        "--path",
        default=None,
        help=(
            "Executable to attach the profile to. Defaults to $APPIMAGE. Refused "
            "for world-writable locations (/tmp and friends) and for shared "
            "interpreters such as /usr/bin/python3, which would over-grant."
        ),
    )
    sbx_status = sbx_sub.add_parser(
        "status", help="Report whether this launch is covered by the profile"
    )
    sbx_status.add_argument("--path", default=None, help="Executable to check instead of $APPIMAGE")
    sbx_sub.add_parser("remove-profile", help="Unload and remove the profile (sudo on Linux)")

    # cloud — provision + run KiroCrew on the user's own AWS EC2 (bring-your-own
    # AWS; credentials resolved by the aws CLI, never stored by KiroCrew).
    cloud_parser = cli_help.add_command(
        sub,
        "cloud",
        epilog="""
Examples:
  kirocrew cloud launch                  # interactive: provision + configure + open dashboard
  kirocrew cloud launch --size power     # non-interactive size
  kirocrew cloud launch --new            # create a separate new instance
  kirocrew cloud launch --subnet subnet-0abc…  # pin the launch to an exact subnet
  kirocrew cloud list                    # list your cloud instances
  kirocrew cloud connect                 # reopen the dashboard over SSM
  kirocrew cloud stop | start            # pause / resume (save cost)
  kirocrew cloud destroy                 # remove EVERYTHING from AWS
  kirocrew cloud iam-policy              # print the least-privilege IAM policy
  kirocrew cloud doctor                  # check prerequisites + AWS reachability
""",
        formatter_class=_fmt,
    )

    # --profile/--region are universal; --tag only applies to verbs that address ONE
    # instance. `list`, `iam-policy`, `iam-boundary`, and `doctor` are account-wide,
    # so they take the pair WITHOUT --tag via _cloud_creds_opts.
    def _cloud_creds_opts(p: "argparse.ArgumentParser") -> None:
        p.add_argument(
            "--profile", default="", help="AWS profile name (default: saved / CLI default)"
        )
        p.add_argument("--region", default="", help="AWS region (default: saved / us-east-1)")

    def _cloud_common(p: "argparse.ArgumentParser") -> None:
        _cloud_creds_opts(p)
        p.add_argument("--tag", default="", help="Instance tag (default: last launched)")

    cloud_sub = cloud_parser.add_subparsers(dest="cloud_action")
    _c_launch = cloud_sub.add_parser("launch", help="Provision + configure an instance")
    _cloud_creds_opts(_c_launch)
    _c_launch.add_argument(
        "--size",
        default="",
        choices=_cloud_size_choices(),
        help="Instance size tier (default: balanced / interactive picker)",
    )
    _c_launch.add_argument(
        "--subnet",
        default="",
        metavar="SUBNET_ID",
        help="Launch into this exact subnet (subnet-xxxx) instead of network "
        "auto-discovery — required to target a dedicated/private-subnet VPC "
        "when a default VPC exists. The subnet must have internet egress "
        "(NAT or IGW route).",
    )
    _c_launch.add_argument("-y", "--yes", action="store_true", help="Accept defaults, no prompts")
    _c_launch.add_argument(
        "--new",
        action="store_true",
        help="Create a separate new instance instead of resuming the saved one",
    )
    _c_launch.add_argument(
        "--keep-on-failure",
        action="store_true",
        help="On bootstrap failure, keep the instance (disable rollback) for inspection",
    )

    _c_list = cloud_sub.add_parser("list", help="List your Kiro Crew cloud instances")
    _cloud_creds_opts(_c_list)

    _c_status = cloud_sub.add_parser("status", help="Show one instance's state")
    _cloud_common(_c_status)

    def _tunnel_opts(p: "argparse.ArgumentParser") -> None:
        _cloud_common(p)
        p.add_argument(
            "--local-port",
            type=int,
            default=0,
            help="Local port to forward the dashboard to (default: 5599)",
        )
        p.add_argument(
            "--no-browser",
            action="store_true",
            help="Open the tunnel but don't launch a browser",
        )

    _c_connect = cloud_sub.add_parser("connect", help="Open the dashboard over an SSM tunnel")
    _tunnel_opts(_c_connect)
    # `tunnel` — a clear standalone command to open the dashboard SSM tunnel,
    # independent of launch/setup (alias of connect).
    _c_tunnel = cloud_sub.add_parser(
        "tunnel", help="Open the dashboard SSM tunnel (standalone; alias of connect)"
    )
    _tunnel_opts(_c_tunnel)
    _c_login = cloud_sub.add_parser(
        "login", help="Sign kiro-cli in on the instance (fixes 'not logged in' chat errors)"
    )
    _cloud_common(_c_login)
    _c_login.add_argument(
        "--no-browser", action="store_true", help="Print the device URL but don't open a browser"
    )
    _c_logout = cloud_sub.add_parser(
        "logout", help="Sign kiro-cli out on the instance (to switch Kiro account)"
    )
    _cloud_common(_c_logout)
    _c_stop = cloud_sub.add_parser("stop", help="Stop the instance (pause billing)")
    _cloud_common(_c_stop)
    _c_start = cloud_sub.add_parser("start", help="Start a stopped instance")
    _cloud_common(_c_start)

    _c_destroy = cloud_sub.add_parser(
        "destroy", help="Remove the instance and ALL its AWS resources"
    )
    _cloud_common(_c_destroy)
    _c_destroy.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    _c_destroy.add_argument(
        "--dry-run", action="store_true", help="Show the delete command, don't run it"
    )

    _c_iam = cloud_sub.add_parser(
        "iam-policy", help="Print the least-privilege IAM policy to apply"
    )
    _cloud_creds_opts(_c_iam)
    _c_boundary = cloud_sub.add_parser(
        "iam-boundary",
        help="Pre-create the immutable instance permissions boundary (admin, one-time)",
    )
    _cloud_creds_opts(_c_boundary)
    _c_doctor = cloud_sub.add_parser("doctor", help="Check cloud prerequisites + AWS reachability")
    _cloud_creds_opts(_c_doctor)

    # logs — tail the gateway log. Reads from the systemd journal when running
    # as a service on Linux, the launchd stdout file on macOS, or the
    # foreground gateway log file otherwise.
    logs_parser = cli_help.add_command(sub, "logs")
    logs_parser.add_argument(
        "-f", "--follow", action="store_true", help="Follow log output (live tail)"
    )
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=100, help="Number of lines to show (default: 100)"
    )

    # token
    token_parser = cli_help.add_command(sub, "token")

    # logout
    logout_parser = cli_help.add_command(sub, "logout")
    logout_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )
    token_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )
    token_parser.add_argument("--ttl", default="20h", help="Token TTL, e.g. 1h, 30m (default: 20h)")
    token_parser.add_argument(
        "--embed-parent-port",
        type=int,
        default=None,
        help=(
            "Parent dashboard port to authorize as a CSP frame-ancestor for the "
            "multi-instance embed (the embedding gateway's KIROCREW_PORT). Loopback "
            "origins at this port may frame this dashboard; omit for the default "
            "'self'-only policy."
        ),
    )

    # status
    status_parser = cli_help.add_command(sub, "status")
    status_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )

    # mcp-cron (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-cron")

    # consolidate
    consolidate_parser = cli_help.add_command(sub, "consolidate", parents=[_jail_opts])
    consolidate_parser.add_argument(
        "session_key",
        nargs="?",
        default=None,
        help="Session key to consolidate (omit to list pending sessions)",
    )
    consolidate_parser.add_argument(
        "--all",
        action="store_true",
        dest="consolidate_all",
        help="Consolidate all sessions with unconsolidated messages",
    )

    # mcp-core (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-core")

    # mcp-computer (MCP server — spawned by the agent backend, not user-facing).
    # A THIN SHIM: it forwards to the gateway over loopback, where the
    # fail-closed governance gate and all accessibility work live.
    sub.add_parser("mcp-computer")

    # mcp-dashboard (MCP server — spawned by the agent backend, not user-facing).
    # Mounted only for an agent whose spec grants it, so an unassigned set costs
    # a session nothing: kiro-cli loads a server only when `tools` names it.
    sub.add_parser("mcp-dashboard")

    # Builtin app MCP servers (spawned by the agent backend, not user-facing).
    # Only builtins that actually ship an ``mcp_server`` module get a verb —
    # ``_BUILTIN_NAMES`` is load-bearing for HTTP route registration and lists
    # every builtin, so registering unconditionally advertised commands that
    # crashed with a raw ModuleNotFoundError traceback (#5901). A builtin that
    # gains an ``mcp_server.py`` gains its verb automatically.
    #
    # The probe only runs when the invocation actually names an ``mcp-*``
    # command: registration precision is unobservable otherwise (the verbs are
    # hidden from help/choices by hide_internal_commands, and dispatch cannot
    # reach them), so every other command — including the gateway boot path —
    # pays nothing for it.
    _probe_mcp_verbs = any(_a.startswith("mcp-") for _a in sys.argv[1:])
    for _bname in _BUILTIN_NAMES:
        if not _probe_mcp_verbs or _builtin_mcp_server_available(_bname):
            sub.add_parser(f"mcp-{_bname}")

    # them in the ``kirocrew-computer`` MCP server instead.
    computer_parser = cli_help.add_command(
        sub,
        "computer",
        epilog="""
Examples:
  kirocrew computer doctor                     # Support + permission report
  kirocrew computer doctor --json              # The same report as JSON
  kirocrew computer apps                       # Apps with an on-screen window

Computer use is OFF by default and is enabled only from the dashboard
(Settings -> Computer Use). An agent cannot enable it.
""",
        formatter_class=_fmt,
    )
    computer_parser.add_argument(
        "computer_args",
        nargs=argparse.REMAINDER,
        help="computer sub-command and its arguments",
    )

    # learn
    learn_parser = cli_help.add_command(
        sub,
        "learn",
        epilog="""
Examples:
  kirocrew learn list
  kirocrew learn add 'use snake_case for variables' --category tool
  kirocrew learn remove 'snake_case'
""",
        formatter_class=_fmt,
    )
    learn_sub = learn_parser.add_subparsers(dest="learn_action")
    learn_add = learn_sub.add_parser("add", help="Save a lesson")
    learn_add.add_argument("rule", help="The rule or correction to remember")
    learn_add.add_argument(
        "--category",
        choices=["tool", "preference", "knowledge"],
        default="knowledge",
        help="Lesson category (default: knowledge)",
    )
    learn_add.add_argument("--negative", help="What NOT to do (optional)")
    learn_sub.add_parser("list", help="List all lessons")
    learn_rm = learn_sub.add_parser("remove", help="Remove lessons matching a substring")
    learn_rm.add_argument("query", help="Substring to match against lesson rules")

    # artifact
    art_parser = cli_help.add_command(
        sub,
        "artifact",
        epilog="""
Examples:
  kirocrew artifact list
  kirocrew artifact list --tag op --kind widget
  kirocrew artifact save --name "CR Queue" --content-file widget.html --tags ops,cr
  cat widget.html | kirocrew artifact save --name "Pipeline Health"
  kirocrew artifact show cr-queue
  kirocrew artifact show cr-queue --version 2
  kirocrew artifact show cr-queue --meta
  kirocrew artifact update cr-queue --content-file widget.html
  kirocrew artifact versions cr-queue
  kirocrew artifact delete cr-queue
""",
        formatter_class=_fmt,
    )
    art_sub = art_parser.add_subparsers(dest="artifact_action")

    art_list = art_sub.add_parser("list", help="List saved artifacts")
    art_list.add_argument("--tag", help="Filter by tag")
    art_list.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text", "image"],
        help="Filter by kind",
    )
    art_list.add_argument("-q", "--q", help="Substring filter on artifact name")

    art_show = art_sub.add_parser("show", help="Print an artifact's content")
    art_show.add_argument("slug", help="Artifact slug")
    art_show.add_argument("--version", type=int, help="Specific version (default: current)")
    art_show.add_argument(
        "--meta",
        action="store_true",
        help="Print metadata as JSON instead of the content body",
    )

    art_save = art_sub.add_parser("save", help="Save a new artifact")
    art_save.add_argument("--name", required=True, help="Human-readable name")
    art_save.add_argument(
        "--slug",
        help=(
            "Explicit slug instead of one derived from --name. A taken or "
            "malformed slug is REFUSED, never renamed: use 'artifact update "
            "<slug>' to version one in place. Omit to let a collision resolve "
            "by suffixing (-2, -3, ...)"
        ),
    )
    art_save.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text", "image"],
        default=None,
        help="Artifact kind (default: inferred from content / file extension)",
    )
    art_save.add_argument("--content", help="Inline content")
    art_save.add_argument("--content-file", help="Path to file containing the content")
    art_save.add_argument("--description", default="", help="Short description")
    art_save.add_argument("--tags", help="Comma-separated tag list")

    art_update = art_sub.add_parser("update", help="Update an artifact in place")
    art_update.add_argument("slug", help="Artifact slug to update")
    art_update.add_argument("--content", help="Inline new content")
    art_update.add_argument("--content-file", help="Path to file containing new content")
    art_update.add_argument("--name", help="New name (rename)")
    art_update.add_argument("--description", help="New description")
    art_update.add_argument("--tags", help="Replacement tag list (comma-separated)")

    art_del = art_sub.add_parser("delete", help="Delete an artifact and all its versions")
    art_del.add_argument("slug", help="Artifact slug to delete")

    art_ver = art_sub.add_parser("versions", help="List the version numbers for an artifact")
    art_ver.add_argument("slug", help="Artifact slug")

    # Memory
    mem_parser = cli_help.add_command(sub, "memory")
    mem_sub = mem_parser.add_subparsers(dest="mem_action")
    mem_sub.add_parser("list", help="Show semantic memory entries")
    mem_search = mem_sub.add_parser("search", help="Search memory (vector + markdown layers)")
    mem_search.add_argument("query", help="Search query text")
    mem_search.add_argument(
        "--layer",
        choices=["vector", "history", "all"],
        default="all",
        help=(
            "Which memory to search: vector (episodic semantic recall), "
            "history (keyword FTS over preferences/projects/daily history), "
            "or all (default)"
        ),
    )
    mem_show = mem_sub.add_parser(
        "show", help="Show the markdown memory layer (preferences, projects, daily history)"
    )
    mem_show.add_argument(
        "target",
        nargs="?",
        choices=["preferences", "projects", "history"],
        help="Layer to show (default: all three)",
    )
    mem_show.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format: raw markdown or structured JSON (default: md)",
    )
    mem_show.add_argument(
        "--since",
        help="History only: include days on or after DATE (YYYY-MM-DD)",
    )
    mem_sub.add_parser("stats", help="Show memory statistics")
    mem_sub.add_parser("audit", help="Scan memory for suspicious content")
    mem_export = mem_sub.add_parser("export", help="Export all memory to JSON")
    mem_export.add_argument("--output", "-o", help="Output file (default: stdout)")
    mem_export.add_argument(
        "--include-markdown",
        action="store_true",
        help="Also include the markdown layer (preferences, projects, daily history)",
    )
    mem_sub.add_parser("migrate", help="Migrate legacy markdown memory to vector store")
    mem_import = mem_sub.add_parser("import", help="Import memory from JSON file")
    mem_import.add_argument("file", help="Path to JSON file (export format)")

    # agent
    agent_parser = cli_help.add_command(sub, "agent")
    agent_sub = agent_parser.add_subparsers(dest="agent_action")
    agent_sub.add_parser("list", help="List Kiro Crew agents")
    agent_create = agent_sub.add_parser("create", help="Create a Kiro Crew agent")
    agent_create.add_argument("--name", required=True, help="Agent name")
    agent_create.add_argument("--kiro-agent", default="kirocrew", help="Kiro agent name")
    agent_create.add_argument("--workspace", default="default", help="Workspace name")
    agent_create.add_argument("--memory-store", default="default", help="Memory store name")
    agent_update = agent_sub.add_parser("update", help="Update a Kiro Crew agent")
    agent_update.add_argument("name", help="Agent name to update")
    agent_update.add_argument("--kiro-agent", help="New kiro agent name")
    agent_update.add_argument("--workspace", help="New workspace name")
    agent_update.add_argument("--memory-store", help="New memory store name")
    agent_delete = agent_sub.add_parser("delete", help="Delete a Kiro Crew agent")
    agent_delete.add_argument("name", help="Agent name to delete")
    agent_reset_model = agent_sub.add_parser(
        "reset-model",
        help="Clear an agent spec's pinned model so it tracks the shipped default again",
    )
    agent_reset_model.add_argument(
        "--agent",
        # Literal rather than an import: this parser is built on every CLI
        # invocation and `agent.py` pulls in config.loader, which is the import
        # cost the lazy dispatch in main() exists to avoid. Same spelling the
        # resolver itself compares against in resolve_effective_model.
        default="kirocrew",
        help="Kiro agent spec to reset (default: kirocrew)",
    )

    # workspace
    ws_parser = cli_help.add_command(sub, "workspace")
    ws_sub = ws_parser.add_subparsers(dest="workspace_action")
    ws_sub.add_parser("list", help="List workspaces")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("--name", required=True, help="Workspace name")
    ws_create.add_argument(
        "--dir",
        default=None,
        help=(
            "Workspace directory NAME, relative to the KiroCrew data home "
            "(default: workspace-<name>). Any path resolving outside the data "
            "home is rejected."
        ),
    )
    ws_create.add_argument("--copy-from", help="Copy dir from an existing workspace")
    ws_update = ws_sub.add_parser("update", help="Update a workspace")
    ws_update.add_argument("name", help="Workspace name to update")
    ws_update.add_argument(
        "--dir",
        help=(
            "New workspace directory NAME, relative to the KiroCrew data home. "
            "Any path resolving outside the data home is rejected."
        ),
    )
    ws_delete = ws_sub.add_parser("delete", help="Delete a workspace")
    ws_delete.add_argument("name", help="Workspace name to delete")

    # app
    app_parser = cli_help.add_command(
        sub,
        "app",
        epilog="""
Examples:
  kirocrew app install /path/to/oncall-watchtower
  kirocrew app list
  kirocrew app enable oncall-watchtower
  kirocrew app disable oncall-watchtower
  kirocrew app info oncall-watchtower
  kirocrew app uninstall oncall-watchtower
""",
        formatter_class=_fmt,
    )
    app_sub = app_parser.add_subparsers(dest="app_action")
    app_install = app_sub.add_parser("install", help="Install an app from a local directory")
    app_install.add_argument("source", help="Path to app directory containing app.json")
    app_sub.add_parser("list", help="List installed apps")
    app_enable = app_sub.add_parser("enable", help="Enable an installed app")
    app_enable.add_argument("name", help="App name to enable")
    app_disable = app_sub.add_parser("disable", help="Disable an installed app")
    app_disable.add_argument("name", help="App name to disable")
    app_mcp = app_sub.add_parser(
        "mcp",
        help="Run an app's MCP server on stdio (spawned by kiro-cli, not for humans)",
    )
    app_mcp.add_argument("name", help="App name whose MCP server to run")
    app_uninstall = app_sub.add_parser(
        "uninstall", help="Uninstall an app (preserves app data by default)"
    )
    app_uninstall.add_argument("name", help="App name to uninstall")
    app_uninstall.add_argument(
        "--purge-data",
        action="store_true",
        help="Permanently delete the app data directory",
    )
    app_info = app_sub.add_parser("info", help="Show app details")
    app_info.add_argument("name", help="App name")
    app_dev = app_sub.add_parser(
        "dev",
        help="Toggle dev mode (no-store UI serving + live reload on file change)",
        # No flag abbreviations: --confirm-out-of-install-root is the
        # operator's out-of-install attestation and the builtin agent deny
        # rule matches its LITERAL text, so an accepted abbreviation
        # (`--confirm`, `--c`) would sail past the rule and let an agent
        # shell self-supply the attestation. Only the exact flag parses.
        allow_abbrev=False,
    )
    app_dev.add_argument("name", help="App name")
    app_dev.add_argument("--off", action="store_true", help="Turn dev mode off")
    app_dev.add_argument(
        "--confirm-out-of-install-root",
        action="store_true",
        help=(
            "Confirm enabling dev mode on a ui root that resolves outside the "
            "app's install directory (e.g. a ui/ symlinked to your source tree)"
        ),
    )
    app_init = app_sub.add_parser("init", help="Scaffold a new app")
    app_init.add_argument("name", help="App name (kebab-case)")
    app_init.add_argument("--dir", default=".", help="Output directory (default: current)")
    app_init.add_argument("--backend", action="store_true", help="Include backend stub")
    app_init.add_argument("--ui", action="store_true", help="Include UI frontend (ESM + Vite)")
    app_init.add_argument("--cron", action="store_true", help="Include sample cron job")

    # config
    cfg_parser = cli_help.add_command(
        sub,
        "config",
        epilog="""
Examples:
  kirocrew config get                   # Show all config
  kirocrew config get agent.provider    # Get a specific value
  kirocrew config set dashboard.url http://localhost:5476
  kirocrew config edit                  # Open in $EDITOR
  kirocrew config defaults              # Stored values holding a superseded default
  kirocrew config defaults --adopt      # Take the current defaults for all of them
  kirocrew config defaults --keep session.autocompact_pct   # Affirm one as intentional

The dashboard port is set with the KIROCREW_PORT env var, not a config key.
""",
        formatter_class=_fmt,
    )
    cfg_sub = cfg_parser.add_subparsers(dest="config_action")
    cfg_get = cfg_sub.add_parser("get", help="Get a config value (or all if no key)")
    cfg_get.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set = cfg_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set.add_argument("value", nargs="?", help="Value to set")
    cfg_set.add_argument("--file", "-f", dest="file", help="Load full config from a JSON file")
    cfg_set.add_argument(
        "--local",
        action="store_true",
        help="Save to config.local.json (persists across upgrades)",
    )
    cfg_sub.add_parser("edit", help="Open config in $EDITOR")
    cfg_defaults = cfg_sub.add_parser(
        "defaults",
        help="Review stored values that still hold a superseded default",
    )
    cfg_defaults.add_argument(
        "keys",
        nargs="*",
        help="Limit to these dotted keys (default: every drifted key)",
    )
    _cfg_def_mode = cfg_defaults.add_mutually_exclusive_group()
    _cfg_def_mode.add_argument(
        "--adopt",
        action="store_true",
        help="Remove the stored keys so the current defaults apply",
    )
    _cfg_def_mode.add_argument(
        "--keep",
        action="store_true",
        help="Record the stored values as intentional and stop reporting them",
    )

    # Last registration done: stop argparse from answering an unknown command
    # with the internal mcp-* server names. Must come after every add_parser,
    # and before parse_args.
    cli_help.hide_internal_commands(sub)

    args = parser.parse_args()

    # Direct agent-bearing CLI commands do not construct the long-lived
    # prerequisite service. Pin an explicit override before the jail gate or
    # provider factory can launch it, preserving the same process-start trust
    # boundary as the gateway.
    if args.command in _JAILED_COMMANDS:
        from kiro_crew.kiro_prerequisite import register_process_start_override_attestation

        register_process_start_override_attestation()

    # The live target (Dev Fleet "Make live") is a pointer file, not an edit to
    # this process's service definition — so it is resolved HERE, by the process
    # itself. This must stay the FIRST thing the gateway does after argv is
    # known: the gateway lock is not held yet and no socket is bound, so exec'ing
    # away leaves nothing half-done. It returns (and we boot the installed build)
    # whenever there is no usable target, so a bad pointer can never leave the
    # host with no gateway. Gateway only: a plain CLI invocation must keep running
    # the install the user typed, not a worktree someone made live.
    if args.command == "gateway":
        maybe_reexec(sys.argv[1:])

    # ``gateway --seed <fixture>`` populates $KIROCREW_HOME from a hand-authored
    # fixture BEFORE the gateway starts — lets a dev spin up a pre-populated
    # server in one command. We run the seed here (post parse_args, but BEFORE
    # ``KiroCrewConfig.load()`` and the file-log handler attach at line ~603):
    # both of those call ``config_dir()`` which ``mkdir``s $KIROCREW_HOME, which
    # would pre-populate the target and break ``shutil.copytree``'s
    # empty-target-only contract in Phase 1.A.  If seed fails, exit with the
    # seed's own exit code instead of continuing into the gateway — running
    # the gateway against a half-seeded or wrong-state $KIROCREW_HOME would be
    # worse than a clean failure.
    #
    # ``is not None`` (not truthiness): argparse assigns ``""`` when the user
    # explicitly passes ``--seed ""``, and ``""`` is falsy. A truthiness check
    # would silently start the gateway without seeding — exactly the silent
    # wrong-state startup the rest of this block is set up to avoid.
    # ``_resolve_fixture("")`` has an explicit rail for this case.
    if args.command == "gateway" and getattr(args, "seed", None) is not None:
        _rc = seed_cmd(args)
        if _rc != 0:
            sys.exit(_rc)

    # Resolve (and, on first launch of an upgraded install, MIGRATE) the data home
    # NOW — synchronously, on the main thread, before any subcommand starts an
    # asyncio loop. The legacy→~/.kiro/crew migration blocks (copytree + os.walk
    # under a file lock); running it here guarantees it never lands on the event
    # loop via a lazy first config_dir() inside an async-facing constructor, where
    # it would freeze the loop and could trip the stall watchdog
    # (no-blocking-call-on-event-loop). Idempotent + process-cached, so every later
    # config_dir() is a cheap lookup. Placed AFTER the --seed guard (seeding needs
    # an empty target) and before KiroCrewConfig.load()/the log handler, both of
    # which call config_dir(). Skipped for the seed path above, which set up its
    # own $KIROCREW_HOME (an override → migration is a no-op there anyway).
    ensure_data_home()
    # Console + gateway.log logging. Extracted to a helper because the
    # detach-spawned gateway needs double-write protection (stderr IS
    # gateway.log in that mode) — see _setup_cli_logging.
    _setup_cli_logging(args.command, args.verbose)

    # No subcommand given (`kirocrew` with no args) — show banner + help and exit.
    # Without this guard, the `args.command.startswith("mcp-")` branch later
    # in the dispatch chain raises AttributeError on None.
    if args.command is None:
        print(BANNER)
        parser.print_help()
        return

    # ── Platform context boot (CPP seam) ──
    # Resolve + install the PlatformContext ONCE, before any subcommand spins up
    # services.  Standalone (no companion, no KIROCREW_PROFILE) composes the
    # all-defaults context, so behavior is identical to today; ``boot_platform``
    # is idempotent so a later ``run_gateway`` boot is a no-op.  Failure to
    # compose a non-standalone profile is fail-closed (raises), but a standalone
    # boot never raises — keep the call defensive so a corrupt config cannot
    # break the CLI for the standalone edition.
    #
    # ``doctor`` is exempt from the fail-closed re-raise: it is the read-only
    # triage command whose whole job is to diagnose a broken setup (including a
    # failed composition), so it must RUN rather than abort with a traceback —
    # otherwise the one command that could explain the failure is also bricked
    # by it.  It does no agent/credential work, so running it without an
    # installed context is safe; _doctor() reports the composition failure.
    try:
        boot_platform(KiroCrewConfig.load())
    except Exception as exc:
        # Fail-closed: a non-standalone profile that cannot compose (companion
        # missing/rejected/version-mismatched) MUST NOT silently downgrade to
        # open-source defaults — re-raise so the CLI aborts instead of running
        # mcp-core/mcp-cron/etc. with no security overlay or credential
        # redaction. PluginAdmissionError subclasses PlatformCompositionError.
        if isinstance(exc, PlatformCompositionError):
            if args.command == "doctor":
                logging.getLogger("kiro_crew").debug(
                    "platform composition failed; doctor will report it", exc_info=True
                )
                _platform_boot_error = exc
            else:
                raise
        else:
            # Standalone boot never raises; only genuinely-unexpected errors
            # reach here, and the standalone edition must not break on a corrupt
            # config.
            logging.getLogger("kiro_crew").debug("platform boot deferred", exc_info=True)
            _platform_boot_error = None
    else:
        _platform_boot_error = None

    # ── Process-isolation jail gate (CPP JailProvider seam) ──
    # For agent-bearing commands, give the active edition a chance to re-exec this
    # process into an isolation jail BEFORE any agent/credential work starts.  The
    # public DefaultJailProvider has no backend → pure fall-through (the command
    # runs in-process exactly as today).  See ``_jail_reexec_gate``.
    if args.command in _JAILED_COMMANDS:
        _jail_reexec_gate(args.command, getattr(args, "no_jail", False))

    if args.command == "chat":
        _run_chat(args.message, args.model, agent=getattr(args, "agent", None))
    elif args.command == "gateway":
        # Seam-supplied pre-launch checks (CPP IdentityProvider seam). Runs
        # HERE in the gateway dispatch — not in boot_platform (which runs for
        # every subcommand incl. the mcp-core/mcp-cron stdio servers, where an
        # interactive prompt would corrupt the JSON-RPC stream) and not in
        # DashboardContributor.start_services (which never fires for `token`
        # and only inside gateway async startup). Public default = no checks.
        run_preflight_checks()
        # Enable faulthandler for the long-lived gateway process: it makes
        # `kill -ABRT <pid>` dump every thread's stack to stderr (the gateway
        # log) on demand, and it is the signal the dashboard's stall watchdog
        # keys off to decide whether to arm itself (see start_dashboard). Cheap
        # and gateway-only — other CLI subcommands are short-lived and skip it.
        faulthandler.enable()
        # Install crash breadcrumbs (atexit + excepthook) before asyncio.run
        # so any fatal exception writes to crash.log.
        # The asyncio loop handler is installed later inside run().
        _install_crash_guard()
        gw_kwargs = _resolve_gateway_args(args)
        # Deferred imports (issue #3504): ``dashboard.state`` pulls
        # vector_memory → numpy (~56 MB) and ``cli_server`` pulls
        # slack.gateway (~549 ms) — only the gateway command needs either,
        # so no other subcommand (and no MCP stdio server) pays for them.
        from kiro_crew.cli_server import _gateway
        from kiro_crew.dashboard.state import set_build_info

        # Resolve the running build's git branch+commit ONCE here in the sync
        # entrypoint: provably AFTER KIROCREW_PROJECT_DIR detection (top of main())
        # and BEFORE asyncio.run() starts the loop. Resolving it at state.py import
        # time froze ("","") under systemd, where this module is imported before
        # main() detects the project dir (lru_cache then pins the empty result).
        set_build_info(git_build_info())
        _install_child_watcher()
        # Single-writer guard: refuse a second gateway bound to this
        # KIROCREW_HOME so two ConversationLog writers can never clobber the same
        # session file. Held for the process lifetime; released by the kernel on
        # death (POSIX record lock — see kiro_crew.gateway_lock). The port is
        # passed for diagnosis only: on refusal it lets the error say whether the
        # holder is answering on that port or is a wedged orphan squatting on it.
        try:
            _gw_lock = GatewayLock(config_dir(), port=_diagnostic_port(gw_kwargs)).acquire()
        except GatewayLockError as exc:
            print(f"👻 {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            asyncio.run(_gateway(**gw_kwargs))
        finally:
            _gw_lock.release()
    elif args.command == "setup":
        _setup(
            agent_only=getattr(args, "agent_only", False),
            electron_only=getattr(args, "electron_only", False),
            clean=getattr(args, "clean", False),
            slack=getattr(args, "slack", False),
            whatsapp=getattr(args, "whatsapp", False),
        )
    elif args.command == "doctor":
        _doctor(platform_boot_error=_platform_boot_error, bundle=getattr(args, "bundle", False))
    elif args.command == "manifest":
        _manifest(
            alias=getattr(args, "alias", None),
            output=getattr(args, "output", None),
            url=getattr(args, "url", False),
        )
    elif args.command == "cron":
        from kiro_crew.cli_commands import _cron

        _cron(args)
    elif args.command == "spawn":
        from kiro_crew.cli_commands import _spawn

        _spawn(args)
    elif args.command == "run":
        from kiro_crew.cli_server import _run_task

        asyncio.run(_run_task(args))
    elif args.command == "learn":
        from kiro_crew.cli_commands import _learn

        _learn(args)
    elif args.command == "artifact":
        from kiro_crew.cli_commands import _artifact

        _artifact(args)
    elif args.command == "memory":
        from kiro_crew.cli_commands import _memory_cmd

        _memory_cmd(args)
    elif args.command == "mcp-cron":
        from kiro_crew.mcp_cron import run_mcp_server as run_mcp_cron_server

        run_mcp_cron_server()
    elif args.command == "mcp-core":
        from kiro_crew.mcp_core import run_mcp_core_server

        run_mcp_core_server()
    elif args.command == "mcp-computer":
        from kiro_crew.mcp_computer import run_mcp_server as run_mcp_computer_server

        run_mcp_computer_server()
    elif args.command == "mcp-dashboard":
        # Loaded through importlib in the branch, like the builtin-app servers
        # below: `kirocrew gateway` boots through this module, and a default-off
        # optional subsystem must not be imported to start it.
        importlib.import_module("kiro_crew.mcp_dashboard").run_mcp_server()
    elif args.command.startswith("mcp-") and args.command[4:] in _BUILTIN_NAMES:
        # Registration gates this verb on _builtin_mcp_server_available, and
        # _run_app_mcp_server is the ONE dispatch-time spelling of "import the
        # builtin's mcp_server and run it or refuse cleanly" — the same helper
        # the `kirocrew app mcp <name>` manifest path uses (clean stderr line +
        # exit 1 on ImportError or a missing run_mcp_server entrypoint), so an
        # unresolvable module cannot reach a raw traceback here (#5901).
        from kiro_crew.cli_commands import _run_app_mcp_server

        _run_app_mcp_server(args.command[4:])
    elif args.command == "computer":
        # Deferred import: ``computer_use.cli`` reaches the driver seam, and the
        # macOS driver loads native frameworks on first use. Keeping it out of
        # cli.py's module imports means every OTHER command — and the whole CI
        # fleet — pays nothing for it.
        from kiro_crew.computer_use.cli import run_computer

        run_computer(getattr(args, "computer_args", []))
    elif args.command == "eval":
        from kiro_crew.cli_commands import _run_eval

        asyncio.run(_run_eval(args))
    elif args.command == "security":
        from kiro_crew.cli_commands import _security

        _security(args)
    elif args.command == "tailnet":
        from kiro_crew.cli_commands import _tailnet

        _tailnet(args)
    elif args.command == "telemetry":
        from kiro_crew.cli_commands import _telemetry

        _telemetry(args)
    elif args.command == "policy":
        from kiro_crew.cli_commands import _policy

        _policy(args)
    elif args.command == "knowledge":
        _knowledge(args)
    elif args.command == "secrets":
        # Dispatch through the module-level `importlib` rather than a
        # function-local `from kiro_crew.cli_commands import …`: the latter trips
        # the `top-level-imports` lint, while a module-scope import of
        # `cli_commands` is deliberately avoided (issue #3504 — it costs ~556 ms
        # on every CLI start). `importlib.import_module` keeps the load lazy AND
        # satisfies the linter.
        importlib.import_module("kiro_crew.cli_commands")._handle_secrets(args)
    elif args.command == "pod":
        from kiro_crew.cli_commands import _pod

        _pod(args)
    elif args.command == "update":
        if getattr(args, "action", None) == "approve":
            from kiro_crew.cli_server import _update_approve

            _update_approve()
        else:
            from kiro_crew.cli_server import _update

            _update(force=args.force)
    elif args.command == "stop":
        from kiro_crew.cli_server import _stop

        _stop(args.port)
    elif args.command == "restart":
        from kiro_crew.cli_server import _restart

        _restart(args.port)
    elif args.command == "service":
        from kiro_crew.cli_server import _service_cmd

        sys.exit(_service_cmd(args))
    elif args.command == "sandbox":
        from kiro_crew.cli_server import _sandbox_cmd

        sys.exit(_sandbox_cmd(args))
    elif args.command == "cloud":
        sys.exit(handle_cloud(args))
    elif args.command == "logs":
        from kiro_crew.cli_server import _logs_cmd

        _logs_cmd(args)
    elif args.command == "token":
        from kiro_crew.cli_server import _token

        _token(args)
    elif args.command == "logout":
        from kiro_crew.cli_server import _logout, resolve_client_port

        _logout(resolve_client_port(args.port))
    elif args.command == "status":
        from kiro_crew.cli_server import _status

        _status(args)
    elif args.command == "consolidate":
        _consolidate_cmd(args)
    elif args.command == "config":
        _config_cmd(args)
    elif args.command == "perf":
        rc = perf_cmd(args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "bench":
        rc = bench_cmd(args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "desktop":
        rc = desktop_cmd(args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "snapshot":
        from kiro_crew.snapshot import snapshot_main

        rc = snapshot_main(parsed=args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "restore":
        from kiro_crew.snapshot import restore_main

        rc = restore_main(parsed=args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "agent":
        from kiro_crew.cli_commands import _handle_agent

        _handle_agent(args)
    elif args.command == "workspace":
        from kiro_crew.cli_commands import _handle_workspace

        _handle_workspace(args)
    elif args.command == "app":
        from kiro_crew.cli_commands import _handle_app

        _handle_app(args)
    else:
        print(BANNER)
        parser.print_help()


# ── Config ──


# NOTE (issue #3504): ``cli_commands`` and ``cli_server`` are deliberately NOT
# imported at module scope. ``cli_commands`` costs ~556 ms and ``cli_server``
# ~549 ms (it pulls ``slack.gateway``), and the MCP stdio servers
# (``kirocrew mcp-core`` / ``mcp-cron`` / ``mcp-computer``) — which dispatch
# through this module and hold its imports RESIDENT for their whole lifetime —
# need neither. Every name from those two modules is imported inside the one
# ``main()`` dispatch branch that uses it. ``test_cli_lazy_imports.py`` ratchets
# this: a new module-scope import that reaches them fails the suite.
from kiro_crew.cli_bench import bench_cmd, register_bench_parser  # noqa: E402
from kiro_crew.cli_chat import _run_chat  # noqa: E402
from kiro_crew.cli_cloud import add_size_choices as _cloud_size_choices  # noqa: E402
from kiro_crew.cli_cloud import handle_cloud  # noqa: E402
from kiro_crew.cli_config import _config_cmd  # noqa: E402
from kiro_crew.cli_desktop import desktop_cmd, register_desktop_parser  # noqa: E402
from kiro_crew.cli_doctor import _doctor  # noqa: E402
from kiro_crew.cli_perf import perf_cmd, register_perf_parser  # noqa: E402
from kiro_crew.cli_setup import (  # noqa: E402, F401
    _fix_shell_profiles,
    _manifest,
    _setup,
)
