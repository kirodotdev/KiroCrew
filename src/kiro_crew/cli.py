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
# so we must run this here as well. The inherited llama.cpp path is removed
# before any thread or child process exists because a concurrent environment
# snapshot can observe a removal between enumerating and reading the key.
from kiro_crew._llama_lib_path import drop_inherited_lib_path
from kiro_crew._ssl_compat import _ensure_ssl_certs

_ensure_ssl_certs()
drop_inherited_lib_path()

import argparse
import asyncio
import faulthandler
import importlib
import importlib.machinery
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# An import marked ``noqa: F401`` binds names only the modules split out of this one read
# (other names on it may still be read here). The facade keeps binding them, so
# ``kiro_crew.cli.<name>`` stays the object callers import, type checkers see and patches
# replace (the mirror at the end of this file sends such a patch to every owner holding it).
from kiro_crew import __version__, _facade_mirror, cli_help, platform_compat
from kiro_crew.apps import builtins as _builtins_pkg
from kiro_crew.apps.builtins import BUILTIN_NAMES as _BUILTIN_NAMES
from kiro_crew.cli_log_files import (  # noqa: F401 -- re-exported for existing callers
    _AppendOnlyLogFileHandler,
    _open_owner_only,
    _OwnerOnlyRotatingFileHandler,
)
from kiro_crew.config import KiroCrewConfig, config_dir, ensure_data_home
from kiro_crew.config.loader import (  # noqa: F401
    DASHBOARD_PORT,
    build_provider_factory,
)
from kiro_crew.config.paths import _default_home, _legacy_home  # noqa: F401
from kiro_crew.constants import (  # noqa: F401
    BANNER,
    MIN_NODE_VERSION,
    env_flag_enabled,
    node_too_old_message,
    node_version_meets_floor,
    parse_node_version,
)
from kiro_crew.crash_guard import install as _install_crash_guard
from kiro_crew.env import git_build_info
from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE, GatewayLock, GatewayLockError
from kiro_crew.history import ConversationLog, HistoryConsolidator  # noqa: F401
from kiro_crew.knowledge import store as knowledge_store  # noqa: F401
from kiro_crew.knowledge.dedup import dedup_sweep  # noqa: F401
from kiro_crew.knowledge.store import KnowledgeStore  # noqa: F401
from kiro_crew.log_redaction import install_log_redaction  # noqa: F401
from kiro_crew.memory import MemoryStore  # noqa: F401
from kiro_crew.platform import (  # noqa: F401
    PlatformCompositionError,
    boot_platform,
    current_context,
)
from kiro_crew.preflight import run_preflight_checks
from kiro_crew.project_dir import PROJECT_MARKERS, detect_project_dir
from kiro_crew.seed import seed_cmd
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.service.live_target import maybe_reexec
from kiro_crew.session import SessionManager  # noqa: F401
from kiro_crew.skills import SkillsLoader  # noqa: F401

# The owners bind this same logger by name; ``__name__`` is ``__main__`` under
# ``python -m kiro_crew.cli``, where one object per name must still hold.
logger = logging.getLogger("kiro_crew.cli")

# The project-root signature lives in kiro_crew.project_dir; kept here by name.
_PROJECT_MARKERS = PROJECT_MARKERS


def _project_dir_file() -> Path:
    """Return the path to the saved project_dir file, respecting KIROCREW_HOME."""
    return config_dir() / "project_dir"


def _ensure_node(proj_dir: str = "") -> bool:
    """Run ensure-node.sh to guarantee a supported Node. Returns True if node is OK.

    Skipped on Windows, where it is not merely unhelpful but actively harmful.
    ``ensure-node.sh`` is a POSIX shell script and the repo ships no Windows
    equivalent, so the spawn resolves ``bash`` through ``PATH`` -- and on Windows
    ``C:\\Windows\\System32\\bash.exe`` is WSL's launcher, so the call prints a
    UTF-16 "Windows Subsystem for Linux has no installed distributions" banner into
    whatever stdout it inherited and installs nothing. A pod gateway inherits its
    wrapper's redirected stdout, so that banner lands in the pod's own log and
    reads as pod output. ``env.ensure_node`` already returns None here for the same
    reason; this is the second caller of the same script.

    A Windows host gets its Node from the platform installer or a version manager,
    which ``_node_ok`` already sees, so returning that answer is the whole
    behaviour rather than a degraded one.
    """
    if platform_compat.IS_WINDOWS:
        return _node_ok()
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
    """Check that a node of the supported MAJOR is on PATH.

    The answer gates the ensure-node repair at gateway boot and stays major-only,
    so the full floor adds no boot-time install. A node of that major but below
    the full ``MIN_NODE_VERSION`` still passes and logs a warning naming the exact
    required version and how to update.
    """
    node = shutil.which("node")
    if not node:
        return False
    try:
        node_ver = subprocess.run(
            # The RESOLVED path, not the bare name. ``shutil.which`` is PATHEXT-aware
            # and can answer ``node.CMD`` on Windows (nvm-windows, volta and corepack
            # all install shims), while ``CreateProcess`` extends a bare name with
            # ``.exe`` only -- so on such a host spawning "node" raises
            # ``FileNotFoundError`` and this returns False while node works. A host
            # whose node resolves to ``node.EXE`` is unaffected either way, which is
            # why the bug is invisible on most machines and total on some. Harmless
            # on POSIX, where the resolved path is what the bare name would have
            # found anyway.
            [node, "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = parse_node_version(node_ver.stdout)
        if version is None:
            return False
        if not node_version_meets_floor(version, MIN_NODE_VERSION):
            logging.getLogger(__name__).warning(node_too_old_message(version, MIN_NODE_VERSION))
        return version[0] >= MIN_NODE_VERSION[0]
    except Exception:
        return False


def _detect_project_dir() -> str | None:
    """Find the project root containing agents/ and skills/.

    Search order:
    1. Walk up from CWD
    2. Read saved path from config_dir()/project_dir (respects KIROCREW_HOME)
    """
    return detect_project_dir(_project_dir_file)


def _builtin_mcp_server_available(name: str) -> bool:
    """True when ``kiro_crew.apps.builtins.<name>.mcp_server`` resolves.

    ``_BUILTIN_NAMES`` answers two different questions: which builtins get HTTP
    routes (all of them) and which get an ``mcp-<name>`` CLI verb (only those
    that actually ship an ``mcp_server`` module). Gating the verb on this
    predicate keeps the two decoupled — a builtin without the module is simply
    not registered, instead of advertising a command that dies with a raw
    ``ModuleNotFoundError`` traceback.

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

    # Each command's arguments come from a registrar, called here in registration order: a
    # ``cli_parser_*`` module's, or for ``perf``, ``bench`` and ``desktop`` their own module's.
    # The ``gateway`` block stays inline (``pod.runtime_boot.target_supports_flag`` reads its
    # flags from this file's text), and so do the internal ``mcp-*`` servers.
    cli_parser_agent.register_chat_parser(sub, _jail_opts)
    cli_parser_gateway.register_doctor_parser(sub)
    cli_parser_gateway.register_ledger_sweep_parser(sub)

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

    cli_parser_gateway.register_setup_parser(sub)
    cli_parser_gateway.register_manifest_parser(sub)
    cli_parser_agent.register_cron_parser(sub)
    cli_parser_agent.register_spawn_parser(sub)
    cli_parser_agent.register_run_parser(sub, _jail_opts)
    cli_parser_admin.register_snapshot_parser(sub)
    cli_parser_admin.register_restore_parser(sub)
    cli_parser_admin.register_security_parser(sub)
    cli_parser_admin.register_eval_parser(sub, _jail_opts)
    cli_parser_admin.register_tailnet_parser(sub)
    cli_parser_admin.register_telemetry_parser(sub)
    cli_parser_admin.register_policy_parser(sub)

    register_perf_parser(sub)
    register_bench_parser(sub)
    register_desktop_parser(sub)

    cli_parser_agent.register_knowledge_parser(sub)
    cli_parser_admin.register_secrets_parser(sub)
    cli_parser_admin.register_pod_parser(sub)
    cli_parser_gateway.register_update_parser(sub)
    cli_parser_admin.register_file_delivery_parser(sub)
    cli_parser_gateway.register_stop_parser(sub)
    cli_parser_gateway.register_restart_parser(sub)
    cli_parser_gateway.register_gateway_pid_parser(sub)
    cli_parser_gateway.register_service_parser(sub)
    cli_parser_gateway.register_sandbox_parser(sub)
    cli_parser_gateway.register_kas_parser(sub)
    cli_parser_admin.register_cloud_parser(sub)
    cli_parser_gateway.register_logs_parser(sub)
    cli_parser_gateway.register_token_parser(sub)
    cli_parser_gateway.register_logout_parser(sub)
    cli_parser_gateway.register_status_parser(sub)

    # mcp-cron (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-cron")

    cli_parser_agent.register_consolidate_parser(sub, _jail_opts)

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
    # mcp-work (MCP server — the conductor work ledger's four tools). Opt-in
    # like mcp-dashboard: mounted only for an agent whose spec grants it, so a
    # session that is neither a conductor nor a worker spends nothing on it.
    sub.add_parser("mcp-work")
    # mcp-crew-log (MCP server — the three read-only crew-log tools). Opt-in like
    # the two above: the crew log is an optional subsystem behind a flag, so a
    # session that never verifies or audits it spends nothing on the set.
    sub.add_parser("mcp-crew-log")
    # mcp-debug (MCP server — the five read-only debug tools). Opt-in for the same
    # reason: debugging a gateway is something a person asks for on purpose, so a
    # session that never does it should not carry the schemas.
    sub.add_parser("mcp-debug")
    # mcp-panel (MCP server -- an agent publishes its own dashboard panel).
    # Mounted only for an agent whose spec grants the opt-in set.
    sub.add_parser("mcp-panel")
    # mcp-guide (MCP server -- the always-on platform guide server).
    sub.add_parser("mcp-guide")
    # mcp-mate (MCP server -- a crewmate changing its own record from its own
    # chat: ``update_self`` and the card-backed asks).
    sub.add_parser("mcp-mate")

    # Builtin app MCP servers (spawned by the agent backend, not user-facing).
    # Only builtins that actually ship an ``mcp_server`` module get a verb —
    # ``_BUILTIN_NAMES`` is load-bearing for HTTP route registration and lists
    # every builtin, so registering unconditionally would advertise commands
    # that crash with a raw ModuleNotFoundError traceback. A builtin that
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

    cli_parser_agent.register_computer_parser(sub)
    cli_parser_agent.register_learn_parser(sub)
    cli_parser_agent.register_artifact_parser(sub)
    cli_parser_agent.register_memory_parser(sub)
    cli_parser_agent.register_agent_parser(sub)
    cli_parser_agent.register_workspace_parser(sub)
    cli_parser_agent.register_app_parser(sub)
    cli_parser_gateway.register_config_parser(sub)

    # Last registration done: stop argparse from answering an unknown command
    # with the internal mcp-* server names. Must come after every add_parser,
    # and before parse_args.
    cli_help.hide_internal_commands(sub)

    args = parser.parse_args()

    # Cap glibc malloc arenas before the gateway starts any thread pool: an
    # uncapped glibc gives a busy thread its own 64 MiB arena heaps, and on a
    # many-core host those idle heaps were most of the gateway's RSS. In-process
    # so every launcher (service unit, pod, detached, live re-exec) gets it; an
    # operator's MALLOC_ARENA_MAX wins. No-op off Linux/glibc. The cap only
    # holds while this process is still single-threaded, so nothing that can
    # start a thread may run above this call; test_malloc_arena_limit pins it.
    # The outcome is logged once logging is configured below.
    arena_cap = platform_compat.MallocArenaCap()
    if args.command == "gateway":
        arena_cap = platform_compat.limit_malloc_arenas()

    # The mcp-* stdio servers run one per agent session, so a gateway hosts
    # dozens. If any import reaches numpy, OpenBLAS/OpenMP start one spinning
    # thread per core. Cap both at one before anything below imports the
    # server; an operator's own value wins. Set in-process rather than in the
    # managed MCP spec env so every spec writer and builtin app server gets it.
    #
    # The gateway's own servers (bare ``kirocrew mcp-<name>``, valid port,
    # stderr not gateway.log) start through kiro_crew.mcp_entry.slim_server,
    # not here. A start-up step added to this function for mcp-* commands must
    # be added there too. test_mcp_slim_entry compares the two paths' steps it
    # spies on and the environment each leaves; a new step that is neither of
    # those needs a spy added there.
    if (args.command or "").startswith("mcp-"):
        for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
            os.environ.setdefault(_var, "1")

    # MCP servers and CLI commands hold their managed-venv tree for the process
    # lifetime, so another process's update cannot prune it underneath them.
    # The gateway takes the same hold off-loop AFTER readiness, before updates;
    # even the no-op stat must stay off its boot path.
    if args.command != "gateway":
        from kiro_crew.platform.tree_liveness import hold_running_tree_lock

        hold_running_tree_lock()

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
    platform_compat.log_malloc_arena_cap(arena_cap)

    # No subcommand given (`kirocrew` with no args) — show banner + help and exit.
    # Without this guard, the `args.command.startswith("mcp-")` branch later
    # in the dispatch chain raises AttributeError on None.
    if args.command is None:
        print(BANNER)
        parser.print_help()
        return

    if args.command == "gateway":  # before companion code can repoint SSH_AUTH_SOCK
        from kiro_crew.mcp_gateway.agent_socket import record_boot_agent_socket

        record_boot_agent_socket()

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

    # ── Member memory upgrade (CLI commands only) ──
    # A member store written before member identities existed cannot be resolved
    # until its identity is published, and no command can do it later: the
    # resolvers refuse the store outright. So every CLI command runs it here, in
    # the sync prologue, BEFORE it resolves a member. Three commands are exempt:
    # `gateway`, whose boot path admits no new work (the dashboard socket must
    # accept requests first) and which runs the same repair in its post-readiness
    # memory worker instead; `doctor`, the read-only triage command, which
    # reports the same stores; and the mcp-* stdio servers, children of a gateway
    # that already ran it. A no-op scan of the loaded config when there is
    # nothing to repair; never raises.
    if args.command not in ("gateway", "doctor") and not args.command.startswith("mcp-"):
        from kiro_crew.memory_stores import repair_legacy_member_stores

        repair_legacy_member_stores()

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
        # The gateway's status lines are plain print() calls. Off a terminal
        # (systemd journal, launchd file, the Desktop supervisor's log fd, a
        # detached gateway's own gateway.log) CPython block-buffers stdout, so
        # they surface at exit, stamped with the stop time, or never after a
        # kill or an in-app os.execv. One reconfigure here, before the first
        # status print, covers every launcher; a terminal is already
        # line-buffered and is left alone. Gateway-only: no other subcommand
        # is long-lived under a service manager.
        platform_compat.ensure_line_buffered_stdout()
        # Seam-supplied pre-launch checks (CPP IdentityProvider seam). Runs
        # HERE in the gateway dispatch — not in boot_platform (which runs for
        # every subcommand incl. the mcp-core/mcp-cron stdio servers, where an
        # interactive prompt would corrupt the JSON-RPC stream) and not in
        # DashboardContributor.start_services (which never fires for `token`
        # and only inside gateway async startup). Public default = no checks.
        run_preflight_checks()
        # Under the desktop shell this process inherited Electron's Crashpad
        # exception port, and so would every child it spawns (kiro-cli, MCP
        # servers, and whatever THEY run). Their crashes would then land in
        # OUR Crashpad database as foreign dumps nobody prunes. Cleared here,
        # before the first child, so children fall back to the OS default.
        from kiro_crew.crashpad_inherit import detach_inherited_crash_handler

        detach_inherited_crash_handler()
        # Enable faulthandler for the long-lived gateway process: it makes
        # `kill -ABRT <pid>` dump every thread's stack to stderr (the gateway
        # log) on demand, and it is the signal the dashboard's stall watchdog
        # keys off to decide whether to arm itself (see start_dashboard). Cheap
        # and gateway-only — other CLI subcommands are short-lived and skip it.
        faulthandler.enable()
        # An in-app restart reaches this image through os.execv, and execve
        # preserves ITIMER_REAL while resetting a caught SIGALRM to its default
        # disposition: the predecessor cancels its stall alarm before it execs,
        # and this clears any deadline that still arrived -- one this image
        # never armed -- before boot spends the seconds the watchdog is not yet
        # running. Only while SIGALRM is at its default disposition, the same
        # ownership rule the watchdog applies when it arms.
        from kiro_crew.dashboard.loop_watchdog import disarm_inherited_alarm

        disarm_inherited_alarm()
        # Install crash breadcrumbs (atexit + excepthook) before asyncio.run
        # so any fatal exception writes to crash.log.
        # The asyncio loop handler is installed later inside run().
        _install_crash_guard()
        gw_kwargs = _resolve_gateway_args(args)
        # Deferred imports: ``dashboard.state`` pulls
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
            _gw_lock = GatewayLock(
                config_dir(),
                port=_diagnostic_port(gw_kwargs),
                bind_address=_diagnostic_bind_address(),
            ).acquire()
        except GatewayLockError as exc:
            print(f"👻 {exc}", file=sys.stderr)
            # A live holder is a sibling gateway already serving this home, so
            # retrying can only meet the same refusal: exit the code the systemd
            # unit's RestartPreventExitStatus= names (service/linux.py) and let
            # the supervisor stand down. Every other refusal is one a later
            # attempt may find cleared, so it keeps the restartable exit 1.
            sys.exit(LIVE_HOLDER_EXIT_CODE if exc.live_holder else 1)
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
        _doctor(
            platform_boot_error=_platform_boot_error,
            bundle=getattr(args, "bundle", False),
        )
    elif args.command == "ledger-sweep":
        # Lazy on purpose, through ``importlib`` like ``secrets`` above: the
        # store modules behind the sweep are not part of the CLI's start-up cost.
        importlib.import_module("kiro_crew.ledger_sweep").run_command(
            purge=args.purge,
            older_than_days=args.older_than_days,
            purge_unreadable=args.purge_unreadable,
        )
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
    elif args.command == "mcp-work":
        # Same importlib form and the same reason as mcp-dashboard above: a
        # default-off optional subsystem must not be imported to start the gateway.
        importlib.import_module("kiro_crew.mcp_work").run_mcp_server()
    elif args.command == "mcp-crew-log":
        # Same importlib form and the same reason again, and here the subsystem
        # the module reads is itself flag-gated: `kirocrew gateway` boots through
        # this module and must not import the crew log to start.
        importlib.import_module("kiro_crew.mcp_crew_log").run_mcp_server()
    elif args.command == "mcp-debug":
        # Same importlib form and the same reason again. It matters more here than
        # anywhere else on this list: this module's routes import kiro_crew.diag
        # lazily and that package may not exist in the build at all, so importing
        # this server eagerly would make a diagnostics gap break `kirocrew gateway`.
        importlib.import_module("kiro_crew.mcp_debug").run_mcp_server()
    elif args.command == "mcp-panel":
        # Lazily imported like mcp-dashboard above: a default-off optional
        # subsystem must not be imported just to start the gateway.
        importlib.import_module("kiro_crew.mcp_panel").run_mcp_server()
    elif args.command == "mcp-guide":
        # Lazily imported for the same reason: an opt-in server must not be
        # imported just to start the gateway.
        importlib.import_module("kiro_crew.mcp_guide").run_mcp_server()
    elif args.command == "mcp-mate":
        # Lazily imported for the same reason as the guide.
        importlib.import_module("kiro_crew.mcp_mate").run_mcp_server()
    elif args.command.startswith("mcp-") and args.command[4:] in _BUILTIN_NAMES:
        # Registration gates this verb on _builtin_mcp_server_available, and
        # _run_app_mcp_server is the ONE dispatch-time spelling of "import the
        # builtin's mcp_server and run it or refuse cleanly" — the same helper
        # the `kirocrew app mcp <name>` manifest path uses (clean stderr line +
        # exit 1 on ImportError or a missing run_mcp_server entrypoint), so an
        # unresolvable module cannot reach a raw traceback here.
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
        # `cli_commands` is deliberately avoided (it costs ~556 ms
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
    elif args.command == "file-delivery":
        from kiro_crew.cli_server import _file_delivery_approve

        _file_delivery_approve()
    elif args.command == "stop":
        from kiro_crew.cli_server import _stop

        _stop(args.port, expect_pid=args.expect_pid)
    elif args.command == "restart":
        from kiro_crew.cli_server import _restart

        _restart(args.port)
    elif args.command == "gateway-pid":
        from kiro_crew.cli_server import _gateway_pid

        _gateway_pid()
    elif args.command == "service":
        from kiro_crew.cli_server import _service_cmd

        sys.exit(_service_cmd(args))
    elif args.command == "sandbox":
        from kiro_crew.cli_server import _sandbox_cmd

        sys.exit(_sandbox_cmd(args))
    elif args.command == "kas":
        from kiro_crew.cli_kas import kas_cmd

        sys.exit(kas_cmd(args))
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


# NOTE: ``cli_commands`` and ``cli_server`` are deliberately NOT
# imported at module scope. ``cli_commands`` costs ~556 ms and ``cli_server``
# ~549 ms (it pulls ``slack.gateway``), and the MCP stdio servers
# (``kirocrew mcp-core`` / ``mcp-cron`` / ``mcp-computer``) — which dispatch
# through this module and hold its imports RESIDENT for their whole lifetime —
# need neither. Every name from those two modules is imported inside the one
# ``main()`` dispatch branch that uses it. ``test_cli_lazy_imports.py`` ratchets
# this: a new module-scope import that reaches them fails the suite.
from kiro_crew import cli_parser_admin, cli_parser_agent, cli_parser_gateway  # noqa: E402
from kiro_crew.cli_bench import bench_cmd, register_bench_parser  # noqa: E402
from kiro_crew.cli_chat import _run_chat  # noqa: E402
from kiro_crew.cli_cloud import add_size_choices as _cloud_size_choices  # noqa: E402, F401
from kiro_crew.cli_cloud import handle_cloud  # noqa: E402
from kiro_crew.cli_config import _config_cmd  # noqa: E402
from kiro_crew.cli_desktop import desktop_cmd, register_desktop_parser  # noqa: E402
from kiro_crew.cli_doctor import _doctor  # noqa: E402
from kiro_crew.cli_gateway_boot import (  # noqa: E402
    _diagnostic_bind_address,
    _diagnostic_port,
    _install_child_watcher,
    _resolve_gateway_args,
)
from kiro_crew.cli_jail import _JAILED_COMMANDS, _jail_reexec_gate  # noqa: E402
from kiro_crew.cli_knowledge import _consolidate_cmd, _knowledge  # noqa: E402
from kiro_crew.cli_logging import _setup_cli_logging  # noqa: E402
from kiro_crew.cli_perf import perf_cmd, register_perf_parser  # noqa: E402
from kiro_crew.cli_setup import (  # noqa: E402, F401
    _fix_shell_profiles,
    _manifest,
    _setup,
)

# ── The split-out owners ──
# This module is the ``kirocrew`` entry point and the facade of the code split out of it:
# ``main`` keeps the prologue, the top-level parser with the ``gateway`` command and the
# internal ``mcp-*`` servers, the boot steps and the dispatch chain; every other command's
# arguments come from a registrar (a ``cli_parser_*`` module's, or ``perf``, ``bench`` and
# ``desktop`` from their own modules), and the helpers live in the owners
# below. Every name this module held still resolves as ``kiro_crew.cli.<name>``: an owner's
# name is served from the owner that binds it, and a write through this module
# (``monkeypatch.setattr``, ``mock.patch``) reaches every owner holding the name
# (``kiro_crew._facade_mirror``). Owners never import this module, nor each other.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.cli_logging",
    "kiro_crew.cli_jail",
    "kiro_crew.cli_gateway_boot",
    "kiro_crew.cli_knowledge",
)
for _owner in _OWNERS:
    importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.cli_jail import (  # noqa: F401
        _JAILED_ENV_MARKER,
        _already_jailed,
        _child_argv,
        _refuse_unjailed,
    )
    from kiro_crew.cli_knowledge import _knowledge_stats  # noqa: F401
    from kiro_crew.cli_logging import (  # noqa: F401
        _LOG_ERROR_STREAK_LIMIT,
        _LOG_FATAL_ERRNOS,
        _LOG_QUEUE_LISTENER,
        _LONG_LIVED_COMMANDS,
        _CliLogQueueHandler,
        _fd_targets_file,
        _FdTrackingRotatingFileHandler,
        _redirect_fds_to,
        _stop_log_queue_listener,
        drain_log_queue_before_hard_exit,
    )
