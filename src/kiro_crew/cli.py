"""Kiro Crew CLI — personal AI agent.

Commands:
    kirocrew chat -m "message"    Send a single message
    kirocrew chat                 Interactive chat mode
    kirocrew gateway              Start the Kiro Crew server (dashboard + Slack)
    kirocrew gateway --seed NAME  Populate $KIROCREW_HOME from fixture NAME, then start the gateway
    kirocrew status               Show runtime stats
    kirocrew run TASK.md          Run an autonomous task from a spec file
    kirocrew update               Update Kiro Crew via git fetch + rebuild
    kirocrew cron list|add|remove Manage scheduled jobs
    kirocrew spawn run "task"     Spawn a background subagent
    kirocrew spawn list           List subagents
    kirocrew learn add|list|remove Save and manage learned corrections
    kirocrew setup                Interactive credential setup
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
import faulthandler
import importlib
import logging
import os
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import NoReturn

from kiro_crew import __version__, platform_compat
from kiro_crew.apps.builtins import BUILTIN_NAMES as _BUILTIN_NAMES
from kiro_crew.browser.cli import run_browse
from kiro_crew.config import KiroCrewConfig, config_dir, ensure_data_home
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    build_provider_factory,
)
from kiro_crew.config.paths import _default_home, _legacy_home
from kiro_crew.constants import BANNER, env_flag_enabled
from kiro_crew.crash_guard import install as _install_crash_guard
from kiro_crew.dashboard.state import set_build_info
from kiro_crew.dashboard.urls import parse_dashboard_url
from kiro_crew.env import git_build_info
from kiro_crew.gateway_lock import GatewayLock, GatewayLockError
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.knowledge.dedup import dedup_sweep
from kiro_crew.knowledge.store import KnowledgeStore
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
    kirocrew-core/kirocrew-cron use) so the jailed child inherits its
    frozen/PyInstaller, venv ``bin/`` walk, and ``os.access(X_OK)`` validation —
    rather than re-implementing a bare ``shutil.which`` that misses those cases.
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


_MIN_NODE_VERSION = 16


def _ensure_node(proj_dir: str = "") -> bool:
    """Run ensure-node.sh to guarantee Node >= 16. Returns True if node is OK."""
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
    """Check if node >= MIN_NODE_VERSION is available."""
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
        return major >= _MIN_NODE_VERSION
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
    thread-per-child storm (the _node_version_manager_bins lru_cache only
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
    db_path = config_dir() / "workspace" / "knowledge" / "knowledge.db"
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
                await consolidator.consolidate_now(key)
                print(f"  {key}: done ✓")
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
    # launcher's in-namespace set is ever honored.
    os.environ.pop("KIROCREW_SANDBOX_ACTIVE", None)

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

    sub = parser.add_subparsers(dest="command")

    # Helper for commands with examples
    _fmt = argparse.RawDescriptionHelpFormatter

    # chat
    chat_parser = sub.add_parser(
        "chat",
        help="Chat with the agent",
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
    _doctor_parser = sub.add_parser("doctor", help="Verify Kiro Crew setup")
    _doctor_parser.add_argument(
        "--bundle",
        action="store_true",
        help="Collect logs + crash reports into a redacted diagnostics zip",
    )

    # gateway
    gw_parser = sub.add_parser("gateway", help="Start the Kiro Crew server (dashboard + Slack)")
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
    setup_parser = sub.add_parser("setup", help="Install agent config and configure credentials")
    setup_parser.add_argument(
        "--agent-only",
        action="store_true",
        help="Only install the agent config, skip credential prompts",
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
    manifest_parser = sub.add_parser("manifest", help="Generate Slack manifest with your alias")
    manifest_parser.add_argument("--alias", help="Override alias (default: auto-detect)")
    manifest_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    manifest_parser.add_argument(
        "--url",
        action="store_true",
        help="Print a one-click Slack app creation URL",
    )

    # cron
    cron_parser = sub.add_parser(
        "cron",
        help="Manage scheduled jobs",
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
    spawn_parser = sub.add_parser(
        "spawn",
        help="Manage background subagents",
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
    run_parser = sub.add_parser(
        "run",
        help="Run an autonomous task from a spec file",
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
    snap_parser = sub.add_parser("snapshot", help="Create a portable backup of Kiro Crew state")
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

    rest_parser = sub.add_parser("restore", help="Restore Kiro Crew state from a snapshot")
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

    # security
    sec_parser = sub.add_parser("security", help="Security audit and deny list")

    # eval (benchmark harness)
    eval_parser = sub.add_parser(
        "eval",
        help="Run multi-session evaluation scenarios",
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
    sec_sub.add_parser("verify", help="Verify security event log HMAC integrity")

    # policy — governance model inspection (read-only; MCP-safe)
    tel_parser = sub.add_parser("telemetry", help="Inspect or disable anonymous usage telemetry")
    tel_sub = tel_parser.add_subparsers(dest="telemetry_action")
    tel_sub.add_parser(
        "status", help="Show exactly what the anonymous beacon sends (and whether it will)"
    )
    tel_sub.add_parser("disable", help="Turn the anonymous beacon off permanently")
    tel_sub.add_parser("enable", help="Turn the anonymous beacon back on")

    policy_parser = sub.add_parser(
        "policy", help="Inspect the governance security policy + profiles"
    )
    policy_sub = policy_parser.add_subparsers(dest="policy_action")
    policy_sub.add_parser("show", help="Show the effective enterprise security policy")
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

    register_perf_parser(sub)
    register_bench_parser(sub)
    register_desktop_parser(sub)

    kn_parser = sub.add_parser("knowledge", help="Knowledge Base maintenance")
    kn_sub = kn_parser.add_subparsers(dest="knowledge_action")
    kn_dedup = kn_sub.add_parser(
        "dedup", help="Collapse cross-source duplicate documents (dry-run unless --apply)"
    )
    kn_dedup.add_argument(
        "--apply",
        action="store_true",
        help="Apply the deletions (default: dry-run preview that changes nothing)",
    )

    # pod — isolated, throwaway, full-stack test instances per worktree (kubectl-style)
    pod_parser = sub.add_parser(
        "pod",
        help="Isolated, throwaway, full-stack test instances per worktree (kubectl-style)",
    )
    pod_sub = pod_parser.add_subparsers(
        dest="pod_action",
        metavar="{up,down,ls,status,token,url,logs,exec,provision,install}",
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
    pod_up.add_argument("--seed", default="", help="Seed config dir (tunnel is forced off)")
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
            "Run the pod's cron scheduler. Pods boot with --no-crons by default. A "
            "pod's HOME starts with no cron definitions (only a sanitized config is "
            "seeded), so this enables an empty scheduler for testing cron behavior "
            "inside the pod. Persisted per pod; applies at boot."
        ),
    )
    pod_down = pod_sub.add_parser("down", help="Evict a pod (zero residue)")
    pod_down.add_argument("name", help="Worktree name")
    pod_ls = pod_sub.add_parser("ls", help="List running pods")
    pod_ls.add_argument("--json", action="store_true", help="Emit rows as JSON")
    pod_status = pod_sub.add_parser("status", help="Up/down + health for one pod")
    pod_status.add_argument("name", help="Worktree name")
    pod_status.add_argument("--json", action="store_true", help="Emit status as JSON")
    pod_token = pod_sub.add_parser("token", help="(Re)mint a dashboard token for a running pod")
    pod_token.add_argument("name", help="Worktree name")
    pod_token.add_argument("--ttl", default="2h", help="Token TTL (default: 2h)")
    pod_url = pod_sub.add_parser("url", help="Print a pod's base URL")
    pod_url.add_argument("name", help="Worktree name")
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
    # Hidden verbs re-entered by the systemd unit (ExecStart / ExecStopPost).
    # Registered without `help=` so they stay out of `pod --help` while remaining
    # dispatchable (the metavar above also omits them from the usage line).
    pod_run = pod_sub.add_parser("_run")
    pod_run.add_argument("name")
    pod_cleanup = pod_sub.add_parser("_cleanup")
    pod_cleanup.add_argument("name")

    sub.add_parser("update", help="Update Kiro Crew to the latest version")

    # stop
    stop_parser = sub.add_parser("stop", help="Stop a running Kiro Crew gateway")
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
    restart_parser = sub.add_parser(
        "restart", help="Restart a running Kiro Crew gateway (service-aware)"
    )
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
    svc_parser = sub.add_parser(
        "service",
        help="Manage the Kiro Crew gateway as a system service (requires sudo on Linux)",
    )
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
    sbx_parser = sub.add_parser(
        "sandbox",
        help="Manage the AppArmor profile the agent sandbox needs (Linux/Ubuntu)",
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
    sbx_status.add_argument(
        "--path", default=None, help="Executable to check instead of $APPIMAGE"
    )
    sbx_sub.add_parser(
        "remove-profile", help="Unload and remove the profile (sudo on Linux)"
    )

    # cloud — provision + run KiroCrew on the user's own AWS EC2 (bring-your-own
    # AWS; credentials resolved by the aws CLI, never stored by KiroCrew).
    cloud_parser = sub.add_parser(
        "cloud",
        help="Run Kiro Crew on your own AWS EC2 instance",
        epilog="""
Examples:
  kirocrew cloud launch                  # interactive: provision + configure + open dashboard
  kirocrew cloud launch --size power     # non-interactive size
  kirocrew cloud launch --new            # create a separate new instance
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
    logs_parser = sub.add_parser("logs", help="Show gateway logs")
    logs_parser.add_argument(
        "-f", "--follow", action="store_true", help="Follow log output (live tail)"
    )
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=100, help="Number of lines to show (default: 100)"
    )

    # token
    token_parser = sub.add_parser("token", help="Print a dashboard access URL with auth token")

    # logout
    logout_parser = sub.add_parser("logout", help="Revoke all active dashboard sessions")
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
    status_parser = sub.add_parser("status", help="Show runtime stats")
    status_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )

    # mcp-cron (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-cron", help=argparse.SUPPRESS)

    # consolidate
    consolidate_parser = sub.add_parser(
        "consolidate",
        help="Force history consolidation (triggers auto-skill extraction)",
        parents=[_jail_opts],
    )
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
    sub.add_parser("mcp-core", help=argparse.SUPPRESS)

    # mcp-computer (MCP server — spawned by the agent backend, not user-facing).
    # A THIN SHIM: it forwards to the gateway over loopback, where the
    # fail-closed governance gate and all accessibility work live.
    sub.add_parser("mcp-computer", help=argparse.SUPPRESS)

    # Builtin app MCP servers (spawned by the agent backend, not user-facing)
    for _bname in _BUILTIN_NAMES:
        sub.add_parser(f"mcp-{_bname}", help=argparse.SUPPRESS)

    # mcp-playwright-proxy (MCP proxy — compresses accessibility tree responses)
    proxy_parser = sub.add_parser("mcp-playwright-proxy", help=argparse.SUPPRESS)
    proxy_parser.add_argument("proxy_args", nargs=argparse.REMAINDER)

    # browse — auth management for Playwright MCP browsing
    browse_parser = sub.add_parser(
        "browse",
        help="Setup for Playwright MCP browsing",
        epilog="""
Examples:
  kirocrew browse setup                        # Install Playwright + browsers
  kirocrew browse auth health                  # Check auth status
""",
        formatter_class=_fmt,
    )
    browse_parser.add_argument(
        "browse_args",
        nargs=argparse.REMAINDER,
        help="browse sub-command and its arguments",
    )

    # computer — computer-use (desktop automation) diagnostics. READ-ONLY: there
    # is deliberately no CLI verb that reads a window's contents or drives an
    # app, because those are LLM-facing capabilities and the MCP-first rule puts
    # them in the ``kirocrew-computer`` MCP server instead.
    computer_parser = sub.add_parser(
        "computer",
        help="Computer-use (desktop automation) diagnostics",
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
    learn_parser = sub.add_parser(
        "learn",
        help="Save or manage learned corrections",
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
    art_parser = sub.add_parser(
        "artifact",
        help="Manage saved artifacts (LLM-generated UI)",
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
    mem_parser = sub.add_parser("memory", help="Manage vector memory system")
    mem_sub = mem_parser.add_subparsers(dest="mem_action")
    mem_sub.add_parser("list", help="Show semantic memory entries")
    mem_search = mem_sub.add_parser("search", help="Search episodic memories")
    mem_search.add_argument("query", help="Search query text")
    mem_sub.add_parser("stats", help="Show memory statistics")
    mem_sub.add_parser("audit", help="Scan memory for suspicious content")
    mem_export = mem_sub.add_parser("export", help="Export all memory to JSON")
    mem_export.add_argument("--output", "-o", help="Output file (default: stdout)")
    mem_sub.add_parser("migrate", help="Migrate legacy markdown memory to vector store")
    mem_import = mem_sub.add_parser("import", help="Import memory from JSON file")
    mem_import.add_argument("file", help="Path to JSON file (export format)")

    # agent
    agent_parser = sub.add_parser("agent", help="Manage Kiro Crew agent definitions")
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

    # workspace
    ws_parser = sub.add_parser("workspace", help="Manage workspace definitions")
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
    app_parser = sub.add_parser(
        "app",
        help="Manage Kiro Crew apps",
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
        "dev", help="Toggle dev mode (no-store UI serving + live reload on file change)"
    )
    app_dev.add_argument("name", help="App name")
    app_dev.add_argument("--off", action="store_true", help="Turn dev mode off")
    app_init = app_sub.add_parser("init", help="Scaffold a new app")
    app_init.add_argument("name", help="App name (kebab-case)")
    app_init.add_argument("--dir", default=".", help="Output directory (default: current)")
    app_init.add_argument("--backend", action="store_true", help="Include backend stub")
    app_init.add_argument("--ui", action="store_true", help="Include UI frontend (ESM + Vite)")
    app_init.add_argument("--cron", action="store_true", help="Include sample cron job")

    # config
    cfg_parser = sub.add_parser(
        "config",
        help="Get or set configuration values",
        epilog="""
Examples:
  kirocrew config get                   # Show all config
  kirocrew config get agent.provider    # Get a specific value
  kirocrew config set dashboard.url http://localhost:5476
  kirocrew config edit                  # Open in $EDITOR

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

    if len(sys.argv) > 1 and sys.argv[1] == "mcp-playwright-proxy":
        from kiro_crew.mcp_playwright_proxy import run_proxy

        run_proxy(sys.argv[2:])
        return

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

    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=logging.WARNING,  # third-party libs stay quiet
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # KiroCrew loggers: --verbose CLI flag takes precedence, otherwise
    # fall back to the persistent log_level from config.
    if args.verbose == 0:
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
    # encoding="utf-8" is REQUIRED on Windows: KiroCrew logs non-ASCII glyphs and
    # the default file encoding there is cp1252, so a RotatingFileHandler without
    # it raises UnicodeEncodeError on the first non-ASCII log record (logging
    # swallows it, but it spams "--- Logging error ---" tracebacks and drops the
    # line). ensure_utf8_console() only fixes the console streams, not this file
    # handler.
    _log_file = config_dir() / "gateway.log"
    if args.command == "gateway":
        _prev_log = _log_file.with_suffix(".log.prev")
        if _log_file.exists() and _log_file.stat().st_size > 0:
            try:
                _log_file.replace(_prev_log)
            except OSError:
                pass  # race or permission — keep going
    _fh = RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _fh.setLevel(level)
    _fh.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [PID %(process)d]: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.getLogger("kiro_crew").addHandler(_fh)

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
        asyncio.run(_chat(args.message, args.model, agent=getattr(args, "agent", None)))
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
        _cron(args)
    elif args.command == "spawn":
        _spawn(args)
    elif args.command == "run":
        asyncio.run(_run_task(args))
    elif args.command == "learn":
        _learn(args)
    elif args.command == "artifact":
        _artifact(args)
    elif args.command == "memory":
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
    elif args.command.startswith("mcp-") and args.command[4:] in _BUILTIN_NAMES:
        _mod = importlib.import_module(f"kiro_crew.apps.builtins.{args.command[4:]}.mcp_server")
        _mod.run_mcp_server()
    elif args.command == "browse":
        run_browse(getattr(args, "browse_args", []))
    elif args.command == "computer":
        # Deferred import: ``computer_use.cli`` reaches the driver seam, and the
        # macOS driver loads native frameworks on first use. Keeping it out of
        # cli.py's module imports means every OTHER command — and the whole CI
        # fleet — pays nothing for it.
        from kiro_crew.computer_use.cli import run_computer

        run_computer(getattr(args, "computer_args", []))
    elif args.command == "eval":
        asyncio.run(_run_eval(args))
    elif args.command == "security":
        _security(args)
    elif args.command == "telemetry":
        _telemetry(args)
    elif args.command == "policy":
        from kiro_crew.cli_commands import _policy

        _policy(args)
    elif args.command == "knowledge":
        _knowledge(args)
    elif args.command == "pod":
        _pod(args)
    elif args.command == "update":
        _update()
    elif args.command == "stop":
        _stop(args.port)
    elif args.command == "restart":
        _restart(args.port)
    elif args.command == "service":
        sys.exit(_service_cmd(args))
    elif args.command == "sandbox":
        sys.exit(_sandbox_cmd(args))
    elif args.command == "cloud":
        sys.exit(handle_cloud(args))
    elif args.command == "logs":
        _logs_cmd(args)
    elif args.command == "token":
        _token(args)
    elif args.command == "logout":
        _logout(resolve_client_port(args.port))
    elif args.command == "status":
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
        _handle_agent(args)
    elif args.command == "workspace":
        _handle_workspace(args)
    elif args.command == "app":
        _handle_app(args)
    else:
        print(BANNER)
        parser.print_help()


# ── Config ──


from kiro_crew.cli_bench import bench_cmd, register_bench_parser  # noqa: E402
from kiro_crew.cli_chat import _chat  # noqa: E402
from kiro_crew.cli_cloud import add_size_choices as _cloud_size_choices  # noqa: E402
from kiro_crew.cli_cloud import handle_cloud  # noqa: E402
from kiro_crew.cli_commands import (  # noqa: E402
    _artifact,
    _cron,
    _handle_agent,
    _handle_app,
    _handle_workspace,
    _learn,
    _memory_cmd,
    _pod,
    _run_eval,
    _security,
    _spawn,
    _telemetry,
)
from kiro_crew.cli_config import _config_cmd  # noqa: E402
from kiro_crew.cli_desktop import desktop_cmd, register_desktop_parser  # noqa: E402
from kiro_crew.cli_doctor import _doctor  # noqa: E402
from kiro_crew.cli_perf import perf_cmd, register_perf_parser  # noqa: E402
from kiro_crew.cli_server import (  # noqa: E402
    _gateway,
    _logout,
    _logs_cmd,
    _restart,
    _run_task,
    _sandbox_cmd,
    _service_cmd,
    _status,
    _stop,
    _token,
    _update,
    resolve_client_port,
)
from kiro_crew.cli_setup import (  # noqa: E402, F401
    _fix_shell_profiles,
    _manifest,
    _setup,
)
