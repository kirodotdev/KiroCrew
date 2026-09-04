"""CLI subcommand handlers — cron, spawn, workspace, app, agent, security, eval, learn, memory."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import sys
import time as _time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kiro_crew import __version__, beacon, platform_compat
from kiro_crew.agent import reset_agent_model
from kiro_crew.apps.bridges import (
    deregister_app,
    deregister_app_crons_from_service,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import (
    disable_app,
    enable_app,
    get_app,
    install_app,
    list_apps,
    trust_grant_removal_blocked,
    uninstall_app,
)
from kiro_crew.apps.scaffold import scaffold_app
from kiro_crew.cli_server import _marker_port, resolve_client_port
from kiro_crew.config import config_dir
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    build_provider_factory,
    config_local_path,
    config_path,
    read_config_for_update,
    read_local_secret,
    update_config_locked,
)
from kiro_crew.cron import CronSchedule, CronService, CronStoreUnreadable, format_schedule
from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.dashboard import tailnet, tailnet_serve
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner, format_results, score_by_dimension
from kiro_crew.eval.scenario import AssertionType, load_scenario, load_scenarios
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import safe_read_file
from kiro_crew.learn import LessonStore
from kiro_crew.loopback_http import loopback_urlopen
from kiro_crew.memory import MemoryStore
from kiro_crew.port_resolution import resolve_client_port_ex
from kiro_crew.secrets.migrate import (
    MigrationConflictError,
    format_report,
    migrate_env_secrets,
)
from kiro_crew.security import (
    BUILTIN_DENIED_RULES,
    BUILTIN_DENY_PATTERNS,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    scan_history,
    scan_memory,
)
from kiro_crew.sel import sel
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    WORKSPACE_NAME_RE,
    normalize_lesson_category,
)
from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore, _lesson_display_text

# Workspace dirs are confined to the data home: a workspace is agent-writable
# working state, so letting --dir escape would let it be pointed at ~/.ssh or the
# keystone policy files. The refusal is deliberate — say so, and say what to pass
# instead, rather than the bare "invalid directory path" this used to print.
_WS_DIR_OUTSIDE_HOME = (
    "Error: --dir must resolve inside the KiroCrew data home ({home}); got {given!r}. "
    "Pass a relative directory name (e.g. 'workspace-myproject')."
)

# Strip ANSI escape sequences and C0/C1 control characters from lesson text
# before printing to the terminal, preventing OSC-based clipboard/title attacks.
_TERMINAL_CTRL_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"  # C0/C1 controls (keep \n \t)
)


def _ws_dir_error(given: str) -> str:
    return _WS_DIR_OUTSIDE_HOME.format(home=config_dir(), given=given)


def _ws_dir_resolves_inside_home(ws_dir: str) -> bool:
    """True when *ws_dir* resolves to a STRICT descendant of the data home.

    ``expanduser()`` FIRST is what makes this honest: ``config_dir() / "~/x"``
    silently yields ``<home>/~/x`` — contained, but it creates a literal ``~``
    directory the user never asked for and quietly ignores the tilde they wrote.
    Expanding first means a tilde path is judged as the absolute path the user
    meant, so it is refused with the same clear message as ``/tmp/x`` (matching
    how the dashboard handler reads the same field).

    The test is CONTAINMENT, not "is it absolute": an absolute path that lands
    inside the data home is accepted (it resolves to the same place the relative
    form would, so there is nothing to refuse). What is rejected is anything
    resolving OUTSIDE — which is the property the boundary actually protects.

    STRICT descendant, so the root itself is refused HERE. The separate
    "cannot use config root" checks at each call site compare
    ``config_dir() / ws_dir`` WITHOUT expanding ``~``, so ``~/.kiro/crew`` used to
    become ``<home>/~/.kiro/crew`` there — unequal to the root, hence accepted —
    while the plain absolute form was refused. Deciding it in this one expanded
    place removes that split: a workspace pointed at the data-home root would put
    agent-writable memory/lessons on top of ``config.json`` / ``.env``.

    Inside the home is NOT automatically safe: the keystone paths live there too
    (``profiles/``, ``security_policy.json``, ``admission_policy.json``,
    ``denied_commands.json``, ``.env``, ``sel_hmac.key``…). ``--copy-from`` runs
    ``copytree(..., dirs_exist_ok=True)``, so a workspace dir of ``profiles``
    would OVERWRITE the governance ceiling the agent is specifically forbidden to
    write — the one mechanism that makes that ceiling un-disableable. So the
    resolved target must also clear ``is_sensitive_path()``, the shared gate used
    everywhere else for exactly this question.

    Fails CLOSED on any path we cannot resolve. ``expanduser()`` raises
    ``RuntimeError`` for a ``~unknownuser/...`` prefix (no such user, so no home
    to expand), and ``resolve()`` can raise ``OSError`` on a pathological path —
    both must return False and route into the normal refusal, never escape as a
    traceback. That is the whole point of this PR, so the guard cannot be the one
    thing that crashes.
    """
    try:
        expanded = Path(ws_dir).expanduser()
        candidate = (expanded if expanded.is_absolute() else config_dir() / expanded).resolve()
        root = config_dir().resolve()
        if candidate == root or not candidate.is_relative_to(root):
            return False
        return not is_sensitive_path(str(candidate))
    except (RuntimeError, OSError, ValueError):
        return False


def _format_schedule(schedule: object) -> str:
    """Human-readable schedule description (CLI shows full date for 'at' jobs)."""

    if not isinstance(schedule, CronSchedule):
        return str(schedule)
    if schedule.kind == "at" and schedule.at_ts:

        dt = datetime.fromtimestamp(schedule.at_ts)
        return f"at {dt:%Y-%m-%d %H:%M}"
    return format_schedule(schedule)


def _internal_secret(port: int) -> str:
    """Read the per-session IPC secret written by the gateway.

    The gateway writes ``~/.kiro/crew/.local_secret`` (mode 0600) after a
    successful port bind. CLI commands that hit internal API paths (e.g.
    ``/api/spawn``) send this value as ``X-Internal-Secret`` so the
    dashboard's ``token_auth_middleware`` accepts the request without a
    browser cookie. Mirrors `kiro_crew.mcp_core._internal_secret`.

    Returns an empty string if the file is missing or unreadable; the
    server then rejects the request with 403, which is the correct
    failure mode.
    """
    return read_local_secret(port)


def _spawn(args: argparse.Namespace) -> None:
    """Dispatch spawn subcommands: run, list."""
    base = f"http://localhost:{args.port}"
    action = getattr(args, "spawn_action", None)

    if action == "list":
        req = urllib.request.Request(
            f"{base}/api/spawn",
            headers={"X-Internal-Secret": _internal_secret(args.port)},
        )
        try:
            with loopback_urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                print(f"Error: {body.get('error', e.reason)}")
            except Exception:
                print(f"Error: {e.code} {e.reason}")
            sys.exit(1)
        except (urllib.error.URLError, OSError):
            print("Error: gateway not running (cannot reach dashboard on port %d)" % args.port)
            sys.exit(1)
        agents = data.get("agents", [])
        if not agents:
            print("No subagents.")
            return
        for a in agents:
            if a.get("done"):
                status, note = "✅", ""
            elif a.get("awaiting_approval"):
                # A run parked on its spawn-approval prompt used to render the
                # same bare hourglass as one that is executing, so `spawn list`
                # could not answer "is this working or waiting for me?" (#6484).
                status, note = "🔐", "  — waiting for spawn approval"
            else:
                status, note = "⏳", ""
            print(f"  {status} {a['id']}  {a.get('task', '')[:60]}{note}")
        return

    if action == "run":
        _spawn_run(args, base)
        return

    print("Usage: kirocrew spawn {run|list}")


def _spawn_run(args: argparse.Namespace, base: str) -> None:
    """Spawn a subagent via the dashboard API."""
    data = json.dumps({"task": args.task}).encode()
    req = urllib.request.Request(
        f"{base}/api/spawn",
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(args.port),
        },
    )
    try:
        with loopback_urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            print(f"Error: {body.get('error', e.reason)}")
        except Exception:
            print(f"Error: {e.code} {e.reason}")
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        print("Error: gateway not running (cannot reach dashboard on port %d)" % args.port)
        sys.exit(1)

    agent_id = result["id"]

    if args.fire_and_forget:
        print(f"Spawned subagent {agent_id}: {result['task']}")
        return

    # Block: poll until done

    print(f"Spawned subagent {agent_id}, waiting for result...", file=sys.stderr)
    poll_url = f"{base}/api/spawn/{agent_id}"
    secret = _internal_secret(args.port)
    told_awaiting = False
    while True:
        _time.sleep(2)
        poll_req = urllib.request.Request(poll_url, headers={"X-Internal-Secret": secret})
        try:
            with loopback_urlopen(poll_req, timeout=5) as resp:
                status = json.loads(resp.read())
        except Exception:
            print("Error: lost connection to gateway", file=sys.stderr)
            sys.exit(1)
        # Say WHY the wait is not progressing. A spawn with no parent session
        # raises its approval prompt unowned, so it appears only on the global
        # approvals surface -- not in any chat tab -- and this loop would
        # otherwise sit on "waiting for result..." indefinitely with nothing to
        # act on (#6484). Announced once, not every 2s poll.
        if status.get("awaiting_approval") and not told_awaiting:
            told_awaiting = True
            print(
                "Waiting for spawn approval: approve it in the dashboard "
                "(Approvals) to start this run.",
                file=sys.stderr,
            )
        if status.get("done"):
            if status.get("error"):
                print(f"Error: {status['error']}", file=sys.stderr)
                sys.exit(1)
            print(status.get("result", ""))
            return


def _handle_workspace(args: argparse.Namespace) -> None:
    """Dispatch workspace subcommands: list, create, update, delete."""

    action = getattr(args, "workspace_action", None)
    cfg = KiroCrewConfig.load()

    if action == "list":
        default = cfg.default_workspace
        print(f"{'NAME':<20} {'DIR':<40}")
        for name, ws in cfg.workspaces.items():
            marker = " *" if name == default else ""
            print(f"{name + marker:<20} {ws.dir:<40}")

    elif action == "create":
        if not WORKSPACE_NAME_RE.match(args.name):
            print(
                "Error: invalid workspace name (use alphanumeric, hyphens, underscores)",
                file=sys.stderr,
            )
            sys.exit(1)
        if args.name in cfg.workspaces:
            print(f"Error: workspace '{args.name}' already exists", file=sys.stderr)
            sys.exit(1)
        copy_from = getattr(args, "copy_from", None)
        if copy_from:
            if copy_from not in cfg.workspaces:
                print(
                    f"Error: source workspace '{copy_from}' not found",
                    file=sys.stderr,
                )
                sys.exit(1)

            ws_dir = args.dir if args.dir is not None else f"workspace-{args.name}"
            src_path = config_dir() / cfg.workspaces[copy_from].dir
            dst_path = config_dir() / ws_dir
            if not _ws_dir_resolves_inside_home(ws_dir):
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print(_ws_dir_error(ws_dir), file=sys.stderr)
                sys.exit(1)
            if not src_path.resolve().is_relative_to(config_dir().resolve()):
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: invalid source directory path", file=sys.stderr)
                sys.exit(1)
            # Reject config root itself to avoid copying .env / config.json
            cfg_root = config_dir().resolve()
            if src_path.resolve() == cfg_root or dst_path.resolve() == cfg_root:
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: cannot use config root as workspace directory", file=sys.stderr)
                sys.exit(1)
            # Check for directory collision BEFORE copying files
            existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
            if ws_dir in existing_dirs:
                print(
                    f"Error: directory '{ws_dir}' is already used by another workspace",
                    file=sys.stderr,
                )
                sys.exit(1)
            if src_path.is_dir():

                def _ignore_sensitive(directory: str, entries: list[str]) -> set[str]:
                    skip: set[str] = set()
                    for entry in entries:
                        full = str(Path(directory, entry).resolve())
                        if is_sensitive_path(full):
                            skip.add(entry)
                    return skip

                shutil.copytree(
                    src_path,
                    dst_path,
                    dirs_exist_ok=True,
                    symlinks=True,
                    ignore=_ignore_sensitive,
                )
        else:
            ws_dir = args.dir if args.dir is not None else f"workspace-{args.name}"

            if not _ws_dir_resolves_inside_home(ws_dir):
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print(_ws_dir_error(ws_dir), file=sys.stderr)
                sys.exit(1)
            if (config_dir() / ws_dir).resolve() == config_dir().resolve():
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.create",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: cannot use config root as workspace directory", file=sys.stderr)
                sys.exit(1)
        # Check for directory collision with existing workspaces
        existing_dirs = {ws.dir for ws in cfg.workspaces.values()}
        if ws_dir in existing_dirs:
            print(
                f"Error: directory '{ws_dir}' is already used by another workspace",
                file=sys.stderr,
            )
            sys.exit(1)
        cfg.workspaces[args.name] = WorkspaceConfig(dir=ws_dir)
        cfg.save()
        sel().log_api_access(
            caller="cli",
            operation="workspace.create",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Created workspace: {args.name}")

    elif action == "update":
        if args.name not in cfg.workspaces:
            print(f"Error: workspace '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.dir is not None:
            resolved = (config_dir() / args.dir).resolve()
            if not _ws_dir_resolves_inside_home(args.dir):
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.update",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print(_ws_dir_error(args.dir), file=sys.stderr)
                sys.exit(1)
            if resolved == config_dir().resolve():
                sel().log_api_access(
                    caller="cli",
                    operation="workspace.update",
                    outcome="denied",
                    source="cli",
                    resources=args.name,
                )
                print("Error: cannot use config root as workspace directory", file=sys.stderr)
                sys.exit(1)
            existing_dirs = {ws.dir for n, ws in cfg.workspaces.items() if n != args.name}
            if args.dir in existing_dirs:
                print(
                    f"Error: directory '{args.dir}' is already used by another workspace",
                    file=sys.stderr,
                )
                sys.exit(1)
            cfg.workspaces[args.name].dir = args.dir
        cfg.save()
        sel().log_api_access(
            caller="cli",
            operation="workspace.update",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Updated workspace: {args.name}")

    elif action == "delete":
        if args.name not in cfg.workspaces:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(f"Error: workspace '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.name == cfg.default_workspace:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(
                f"Error: cannot delete default workspace '{args.name}'",
                file=sys.stderr,
            )
            sys.exit(1)
        referencing = [a for a, ac in cfg.agents.items() if ac.workspace == args.name]
        if referencing:
            sel().log_api_access(
                caller="cli",
                operation="workspace.delete",
                outcome="denied",
                source="cli",
                resources=args.name,
            )
            print(
                f"Error: workspace '{args.name}' is referenced by agents: "
                f"{', '.join(referencing)}",
                file=sys.stderr,
            )
            sys.exit(1)
        del cfg.workspaces[args.name]
        cfg.save()
        sel().log_api_access(
            caller="cli",
            operation="workspace.delete",
            outcome="success",
            source="cli",
            resources=args.name,
        )
        print(f"Deleted workspace: {args.name}")

    else:
        print("Usage: kirocrew workspace {list|create|update|delete}")


def _cleanup_app_crons_from_scheduler(app_name: str) -> int:
    """Remove app-owned cron jobs from master scheduler before disable/uninstall.

    Mirrors the cleanup that ``hooks_integration.on_app_disable`` does for the
    HTTP disable path. Returns count removed.
    """
    svc = CronService(base_dir=config_dir())
    svc._load()
    try:
        # deregister_app_crons_from_service is async (routes through the async
        # CronSDK mutators). The CLI is a loop-less process, so drive it with a
        # one-shot event loop. No scheduler is running here, so nothing is armed.
        removed = asyncio.run(deregister_app_crons_from_service(app_name, svc))
        sel().log_api_access(
            caller="cli",
            operation="app_crons_deregister",
            outcome="completed",
            resources=f"app={app_name} removed={removed}",
        )
    except Exception as exc:
        sel().log_api_access(
            caller="cli",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        raise
    if removed:
        print(f"  removed {removed} cron job(s) from scheduler")
    return removed


def _register_app_crons_to_scheduler(app_name: str) -> list[str]:
    """Promote the enabled app's cron definitions into the shared scheduler store.

    Mirrors ``_cleanup_app_crons_from_scheduler`` for the enable direction: the
    HTTP enable route promotes app crons into the running CronService via
    ``hooks_integration.on_app_enable``, but the CLI runs in a separate process
    with no handle on the gateway's service — so without a store write here, an
    app enabled from the CLI has its crons lie dormant until the next gateway
    restart. Writing through a store-backed CronService closes that gap: the
    running gateway's timer tick re-syncs ``crons.json`` by content digest at
    least every ``_TIMER_POLL_SECS``, picking up externally-added jobs by
    design. ``register_app_crons_with_service`` applies the same trust gate and
    command/script vetting as the gateway paths and is idempotent (jobs already
    present by name are skipped). Returns the newly registered job names.
    """
    svc = CronService(base_dir=config_dir())
    svc._load()
    try:
        # register_app_crons_with_service is async (routes through the async
        # CronSDK mutators). The CLI is a loop-less process, so drive it with a
        # one-shot event loop. No scheduler is running here, so nothing is armed.
        registered = asyncio.run(register_app_crons_with_service(app_name, svc))
        sel().log_api_access(
            caller="cli",
            operation="app_crons_register",
            outcome="completed",
            resources=f"app={app_name} crons={registered}",
        )
    except Exception as exc:
        sel().log_api_access(
            caller="cli",
            operation="app_crons_register",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        raise
    if registered:
        print(f"  registered {len(registered)} cron job(s) with scheduler")
    return registered


def _warn_hooks_need_restart(app_name: str) -> bool:
    """Say plainly that a running gateway will not pick up this app's backend hooks.

    Everything else a CLI enable writes is re-read by a running gateway:
    ``enable_app`` writes ``installed.json``, ``register_app`` writes agent and
    skill files, and ``_register_app_crons_to_scheduler`` writes through the
    shared cron store that the gateway's timer tick re-syncs by content digest.
    Backend hooks are the exception -- they are Python modules imported INTO the
    gateway process, and only the gateway can replace them (``on_app_enable`` ->
    ``RouteRegistry.register_app_routes`` -> ``load_app_module``, reached by the
    HTTP enable route and by ``on_gateway_startup``). This process has no handle
    on that one's ``sys.modules``, so a CLI enable cannot load them.

    Printing only "enabled <app>" reads as though it had. The operator then
    verifies a hook change against the code the gateway imported earlier and
    concludes the change did not work -- or that it did, when it never ran
    (issue #7880).

    Deliberately NOT gated on a live-gateway probe. ``_marker_port`` is the only
    verified one available here, and it writes its own multi-gateway warning to
    stderr, which would surface out of context on an app enable. The notice is
    phrased conditionally instead, so it stays true when no gateway is up.

    Only for apps that declare ``backend.hooks``: there is nothing stale to warn
    about otherwise. Returns whether the notice was printed.
    """
    info = get_app(app_name)
    manifest = info.get("manifest") if isinstance(info, dict) else None
    backend = manifest.get("backend") if isinstance(manifest, dict) else None
    hooks = backend.get("hooks") if isinstance(backend, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return False
    declared = ", ".join(sorted(str(k) for k in hooks))
    print(
        f"  note: this app declares backend hooks ({declared}).\n"
        "  A gateway that is already running keeps executing the hook code it\n"
        "  imported earlier; this command cannot replace it. Run `kirocrew\n"
        "  restart`, or disable and re-enable the app from the dashboard, for\n"
        "  hook changes to take effect."
    )
    return True


def _run_app_mcp_server(app_name: str) -> None:
    """Run the named app's stdio MCP server in this process.

    Resolved by convention (``<app package>.mcp_server:run_mcp_server``) rather
    than a manifest field: the manifest already names the server via
    ``mcpServers.<name>.command``, and a second declaration of the same fact is
    one more thing to drift.

    Errors go to stderr and exit non-zero — stdout carries JSON-RPC, so a
    diagnostic written there would corrupt the stream kiro-cli is parsing.
    """
    module_name = f"kiro_crew.apps.builtins.{app_name.replace('-', '_')}.mcp_server"
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only the TARGET module (or one of its parent packages) missing means
        # "this app has no MCP server". A missing dependency imported INSIDE
        # mcp_server.py — or any other ImportError — is a real defect and must
        # keep its traceback rather than exit with a misleading diagnosis.
        if exc.name and (exc.name == module_name or module_name.startswith(exc.name + ".")):
            print(f"App {app_name!r} has no MCP server ({module_name}): {exc}", file=sys.stderr)
            sys.exit(1)
        raise
    runner = getattr(mod, "run_mcp_server", None)
    if runner is None:
        print(f"{module_name} defines no run_mcp_server()", file=sys.stderr)
        sys.exit(1)
    runner()


def _handle_app(args: argparse.Namespace) -> None:
    """Dispatch app subcommands: install, list, enable, disable, uninstall, info."""
    action = getattr(args, "app_action", None)

    if action == "mcp":
        # Spawned by kiro-cli as a stdio MCP server (declared in the app's
        # manifest mcpServers). stdout is the JSON-RPC channel — never print to
        # it here, or the handshake breaks.
        _run_app_mcp_server(args.name)
        return

    if action == "install":
        result = install_app(args.source)
        if result.ok:
            print(f"✅ {result.message}")
            reg = register_app(result.name)
            if reg.agents:
                print(f"   Agents: {', '.join(reg.agents)}")
            if reg.skills:
                print(f"   Skills: {', '.join(reg.skills)}")
            if reg.crons:
                print(f"   Crons:  {', '.join(reg.crons)}")
            if reg.errors:
                for e in reg.errors:
                    print(f"   ⚠️  {e}")
            print(f"\n   Run: kirocrew app enable {result.name}")
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "list":
        apps = list_apps()
        if not apps:
            print("No apps installed.")
            return
        print(f"{'NAME':<25} {'VERSION':<10} {'STATUS':<10} {'DISPLAY NAME'}")
        for app in apps:
            status = "enabled" if app.get("enabled") else "disabled"
            print(
                f"{app['name']:<25} {app.get('version', '?'):<10} "
                f"{status:<10} {app.get('displayName', '')}"
            )

    elif action == "enable":
        result = enable_app(args.name)
        if result.ok:
            reg = register_app(args.name)
            print(f"✅ {result.message}")
            if reg.agents:
                print(f"   Agents registered: {len(reg.agents)}")
            if reg.skills:
                print(f"   Skills registered: {len(reg.skills)}")
            _register_app_crons_to_scheduler(args.name)
            _warn_hooks_need_restart(args.name)
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "disable":
        _cleanup_app_crons_from_scheduler(args.name)
        # Flip the authoritative flag BEFORE tearing resources down. A running gateway
        # is a DIFFERENT process: it watches this app's backend and re-registers its MCP
        # servers and agents on a health recovery, gated on the enabled flag it reads
        # from installed.json. Deregistering first leaves a window where that flag still
        # says enabled and the resources are already gone — and a recovery landing there
        # puts them back for an app the operator is disabling. The gateway's own disable
        # path has no such window because it stops the backend first, which ends the
        # watch; the CLI cannot do that from out here, so it closes the window by
        # ordering instead.
        #
        # If the deregistration below then fails, the app is still correctly marked
        # disabled and the failure is reported to an operator already at the terminal —
        # which is the better of the two error shapes, because the alternative is a
        # silent re-registration nobody sees.
        result = disable_app(args.name)
        deregister_app(args.name)
        if result.ok:
            print(f"✅ {result.message}")
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "uninstall":
        # Precondition before anything destructive: the same reason the dashboard
        # handler checks here rather than inside uninstall_app. deregister_app()
        # below is irreversible, so a grant that cannot be dropped has to abort
        # while the app is still whole.
        blocked = trust_grant_removal_blocked(args.name)
        if blocked:
            print(
                f"❌ not uninstalling {args.name!r}: its third-party execution "
                f"grant could not be removed ({blocked}). The grant is keyed on "
                f"the name, so removing the app while it stands would let any "
                f"future app installed under this name run code without asking. "
                f"Nothing has been changed — clear the cause and retry.",
                file=sys.stderr,
            )
            sys.exit(1)
        _cleanup_app_crons_from_scheduler(args.name)
        deregister_app(args.name)
        keep_data = not getattr(args, "purge_data", False)
        result = uninstall_app(args.name, keep_data=keep_data)
        if result.ok:
            print(f"✅ {result.message}")
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "dev":
        from kiro_crew.apps.dev_mode import set_dev_mode

        enabled = not getattr(args, "off", False)
        dev_result = set_dev_mode(args.name, enabled)
        if "error" in dev_result:
            print(f"❌ {dev_result['error']}", file=sys.stderr)
            sys.exit(1)
        if enabled:
            print(f"✅ {args.name} is now in dev mode")
            print("   UI files served with no-store; edits under ui/ trigger a live reload")
            print("   in the dashboard within ~1s (picked up by the gateway watcher).")
            print("   Tip: symlink the installed ui/ to your source tree for zero-copy edits.")
            print(f"   Turn off with: kirocrew app dev {args.name} --off")
        else:
            print(f"✅ {args.name} dev mode off (normal caching restored)")

    elif action == "info":
        info = get_app(args.name)
        if not info:
            print(f"App '{args.name}' is not installed.", file=sys.stderr)
            sys.exit(1)

        print(json.dumps(info, indent=2))

    elif action == "init":
        output = Path(args.dir).expanduser().resolve()
        include_backend = getattr(args, "backend", False)
        include_ui = getattr(args, "ui", False)
        include_cron = getattr(args, "cron", False)
        try:
            app_dir = scaffold_app(
                output,
                args.name,
                include_backend=include_backend,
                include_ui=include_ui,
                include_cron=include_cron,
            )
        except ValueError as exc:
            # scaffold_app raises when a write path escapes the app directory
            # (traversal in the name, a symlink in the tree). Match the clean
            # error contract of the sibling app actions, not a raw traceback.
            print(f"❌ {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"✅ Scaffolded app: {app_dir}")
        print("   Edit app.json, add agents and skills, then:")
        if include_ui:
            print(f"   cd {app_dir}/ui && npm install && npm run build")
        print(f"   kirocrew app install {app_dir}")

    else:
        print("Usage: kirocrew app {install|list|enable|disable|uninstall|info|init}")


def _handle_agent(args: argparse.Namespace) -> None:
    """Dispatch agent subcommands: list, create, update, delete."""

    action = getattr(args, "agent_action", None)
    cfg = KiroCrewConfig.load()

    if action == "list":
        default = cfg.default_agent
        print(
            f"{'NAME':<20} {'KIRO_AGENT':<20} {'WORKSPACE':<15} "
            f"{'MEMORY_STORE':<15} {'SOURCE':<12}"
        )
        for name, agent in cfg.agents.items():
            marker = " *" if name == default else ""
            print(
                f"{name + marker:<20} {agent.kiro_agent:<20} "
                f"{agent.workspace:<15} {agent.memory_store:<15} "
                f"{getattr(agent, 'source', 'kirocrew'):<12}"
            )

    elif action == "create":
        if args.name in cfg.agents:
            print(f"Error: agent '{args.name}' already exists", file=sys.stderr)
            sys.exit(1)
        cfg.agents[args.name] = KiroCrewAgentConfig(
            kiro_agent=args.kiro_agent,
            workspace=args.workspace,
            memory_store=args.memory_store,
        )
        cfg.save()
        print(f"Created agent: {args.name}")

    elif action == "update":
        if args.name not in cfg.agents:
            print(f"Error: agent '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        agent = cfg.agents[args.name]
        if args.kiro_agent is not None:
            agent.kiro_agent = args.kiro_agent
        if args.workspace is not None:
            agent.workspace = args.workspace
        if args.memory_store is not None:
            agent.memory_store = args.memory_store
        cfg.save()
        print(f"Updated agent: {args.name}")

    elif action == "delete":
        if args.name not in cfg.agents:
            print(f"Error: agent '{args.name}' not found", file=sys.stderr)
            sys.exit(1)
        if args.name == cfg.default_agent:
            print(
                f"Error: cannot delete default agent '{args.name}'",
                file=sys.stderr,
            )
            sys.exit(1)
        del cfg.agents[args.name]
        cfg.save()
        print(f"Deleted agent: {args.name}")

    elif action == "reset-model":
        _agent_reset_model(args)

    else:
        print("Usage: kirocrew agent {list|create|update|delete|reset-model}")


def _agent_reset_model(args: argparse.Namespace) -> None:
    """Clear an agent spec's pinned model (``kirocrew agent reset-model``).

    The explicit, narrow way back to the shipped default model. It exists
    because ownership of a spec's ``model`` cannot be inferred: a value an older
    build's propagation wrote and one the user typed in by hand are
    byte-identical on disk, so nothing may reclassify a pin behind the user's
    back. Before this, the only ways out were the dashboard's Agent Templates
    editor (clear the model) and ``kirocrew setup --clean``, which also
    regenerates the whole spec and discards every other customization with it.
    """
    name = getattr(args, "agent", None) or "kirocrew"
    try:
        spec_path, previous = reset_agent_model(name)
    except FileNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        # Ambiguous: two specs claim the name and the runtime's choice between
        # them is undefined, so resetting either could strip the wrong one.
        print(f"❌ {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"❌ Could not write the agent spec: {exc}", file=sys.stderr)
        sys.exit(1)

    if previous:
        # repr on BOTH the model and the path. Neither is trusted input: an
        # installed app writes specs into the agents directory and a cloned
        # repository can ship one, so an OSC/ANSI sequence can arrive in the
        # `model` value AND in the FILENAME -- the declared-name scan returns
        # whichever file declares the requested name, so its path is attacker-
        # shaped even though *name* itself is grammar-validated.
        print(f"✅ Cleared {name}'s pinned model ({previous!r}) in {str(spec_path)!r}")
    else:
        print(f"✅ {name} had no pinned model in {str(spec_path)!r}")
    print("   It now tracks the shipped default; restart the gateway to apply.")


def _cron(args: argparse.Namespace) -> None:
    """Dispatch cron subcommands, translating a refused write into an error.

    ``CronService._save`` raises ``CronStoreUnreadable`` rather than silently
    skipping the write when the last load failed, so every mutating verb here can
    fail that way. Untranslated it reached the user as a stack trace, which names
    the raise site but not the one action that fixes it. The exception's own
    message carries that remediation, so it is surfaced verbatim.

    One wrapper rather than a handler per verb: `add`, `update`, `remove`,
    `pause`, `resume` and `adopt` all persist through the same ``_save``, so
    catching at the dispatch boundary covers them without eight duplicated
    blocks, and any verb added later is covered by construction.
    """
    try:
        _cron_dispatch(args)
    except CronStoreUnreadable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cron_dispatch(args: argparse.Namespace) -> None:
    """Dispatch cron subcommands: list, add, remove, pause, resume."""

    svc = CronService(base_dir=config_dir())

    action = getattr(args, "cron_action", None)
    if action == "list":
        jobs = svc.list_jobs(include_disabled=True)
        if not jobs:
            print("No cron jobs.")
            return
        for j in jobs:
            status = "✅" if j.enabled else "⏸️"
            sched = _format_schedule(j.schedule)
            print(f"  {status} {j.id}  {j.name}  ({sched})  {j.message[:60]}")
            # Ownership is printed because it decides which surfaces can manage
            # the job at all: a job with no owning session is outside every chat
            # session's scope, so `cron_list` from chat does not list it and the
            # mutating tools answer a deliberately vague "job not found". That is
            # the intended boundary, but with the field invisible here the CLI
            # was the only place the state existed and nothing showed it -- which
            # is what made a normal, correct state read as a job that had
            # vanished. `cron adopt` is the way back.
            owner = j.session_key or ""
            provenance = j.created_by or ""
            if owner:
                detail = f"owner: {owner}"
            else:
                detail = "owner: none (manage from CLI or the dashboard Schedule page)"
            if provenance:
                detail += f"  created by: {provenance}"
            print(f"      {detail}")

    elif action == "adopt":
        job_id = args.job_id
        if getattr(args, "release", False):
            session_key = ""
        else:
            # One flag, two accepted spellings of the same target. A bare slot
            # name gets the `dashboard:` namespace the delivery consumers strip
            # back off (messaging.py / the Slack gateway both
            # removeprefix("dashboard:")), so adding it here is their exact
            # inverse and needs no lookup. An already-namespaced key passes
            # through untouched -- there is no second flag for that case,
            # because a key with no namespace at all could never equal any
            # caller's session key and so could only ever produce a row nobody
            # can own.
            target = (getattr(args, "session_of", None) or "").strip()
            if not target:
                print("Error: --session-of requires a session", file=sys.stderr)
                sys.exit(1)
            session_key = target if ":" in target else f"dashboard:{target}"
        if not svc.adopt_job(job_id, session_key):
            print(f"Error: job not found: {job_id}", file=sys.stderr)
            sys.exit(1)
        sel().log_api_access(
            caller="cli",
            operation="cron.adopt",
            outcome="allowed",
            source="cli",
            resources=f"job_id={job_id} session_key={session_key or '(released)'}",
        )
        if session_key:
            # Ownership and delivery do not have the same reach. `_owned_by`
            # matches any namespace, so a Slack or Telegram session can own and
            # manage a job -- but only a `dashboard:` key resolves to a slot the
            # delivery path can inject into (both consumers reach a slot with
            # removeprefix("dashboard:")). Saying "results are delivered there"
            # for a `slack:` key would be a promise the code does not keep.
            if session_key.startswith("dashboard:"):
                print(
                    f"Job {job_id} now belongs to {session_key}: that session can manage it "
                    f"and its results are delivered there."
                )
                # A typo'd key is accepted by the store but resolves to no slot,
                # so the job's output would go nowhere -- the same
                # invisible-delivery state this command exists to recover from.
                # Warn rather than refuse: the delivery path resolves a live slot
                # first and only falls back to rehydrating from history, so a
                # brand-new tab that has not logged anything yet is a legitimate
                # target and absence of a log does not prove the key is wrong.
                slot = session_key.removeprefix("dashboard:")
                try:
                    known = ConversationLog().has_log(slot)
                except Exception:
                    known = True  # cannot tell -> stay quiet rather than cry wolf
                if not known:
                    print(
                        f"Warning: no recorded session named {slot!r}. If that is a typo, "
                        f"the job's results will not reach anyone -- re-run with the right "
                        f"key, or `--release` to undo.",
                        file=sys.stderr,
                    )
            else:
                print(f"Job {job_id} now belongs to {session_key}: that session can manage it.")
                print(
                    f"Note: results are not injected into a chat for a "
                    f"{session_key.split(':', 1)[0]!r} owner -- only a dashboard session is "
                    f"resolved as a delivery target. Ownership transferred; delivery did not.",
                    file=sys.stderr,
                )
        else:
            print(
                f"Job {job_id} released: no owning session, so manage it from the CLI or "
                f"the dashboard Schedule page."
            )

    elif action == "add":
        every = getattr(args, "every", None)
        cron_expr = getattr(args, "cron_expr", None)
        channel = (getattr(args, "channel", None) or "").strip() or None
        approval_mode = getattr(args, "approval_mode", "") or ""
        agent = (getattr(args, "agent", "") or "").strip()
        silent = getattr(args, "silent", False)
        if agent and not _AGENT_NAME_RE.match(agent):
            print(
                "Error: invalid agent name (alphanumeric, hyphens, underscores; 1-64 chars)",
                file=sys.stderr,
            )
            sys.exit(1)
        if channel and (len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel)):
            print(
                f"Error: invalid channel ID format (expected {CHANNEL_ID_RE.pattern.strip('^$')})"
            )
            return
        if cron_expr:
            job = svc.add_job(
                name=args.name,
                message=args.message,
                cron_expr=cron_expr,
                channel=channel,
                approval_mode=approval_mode,
            )
        elif every:
            job = svc.add_job(
                name=args.name,
                message=args.message,
                every_secs=every,
                channel=channel,
                approval_mode=approval_mode,
            )
        else:
            print("Provide --every or --cron")
            return
        # agent_id and silent are CronJob fields but not add_job kwargs;
        # mirror the MCP cron_add post-create mutation pattern so they
        # are persisted with the job.
        if agent:
            job.agent_id = agent
        if silent:
            job.silent = True
        if agent or silent:
            svc._save()
        sched_desc = _format_schedule(job.schedule)

        sel().log_api_access(
            caller="cli",
            operation="cron.add",
            outcome="allowed",
            source="cli",
            resources=(
                f"job_id={job.id} approval_mode={approval_mode or 'default'} "
                f"agent={agent or 'default'} silent={silent}"
            ),
        )
        print(f"Added job: {job.id} ({job.name}) [{sched_desc}]")

    elif action == "update":
        kwargs: dict = {}
        for field in ("name", "message", "every_secs", "cron_expr", "channel", "timeout_secs"):
            val = getattr(args, field, None)
            if val is not None:
                if field == "channel":

                    val = val.strip() or None
                    if val is None:
                        continue
                    if len(val) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(val):
                        print(
                            f"Error: invalid channel ID format (expected {CHANNEL_ID_RE.pattern.strip('^$')})"
                        )
                        return
                kwargs[field] = val
        if getattr(args, "agent", None) is not None:
            agent_val = args.agent.strip()
            if agent_val and not _AGENT_NAME_RE.match(agent_val):
                print(
                    "Error: invalid agent name (alphanumeric, hyphens, underscores; 1-64 chars)",
                    file=sys.stderr,
                )
                sys.exit(1)
            kwargs["agent_id"] = agent_val
        if getattr(args, "approval_mode", None) is not None:
            kwargs["approval_mode"] = "" if args.approval_mode == "default" else args.approval_mode
        if not kwargs:
            print("Provide at least one field to update")
            return
        if "every_secs" in kwargs and "cron_expr" in kwargs:
            print("Provide --every or --cron, not both")
            return
        try:
            updated = svc.update_job(args.job_id, **kwargs)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        if updated:

            audit_resources = f"job_id={args.job_id} fields={','.join(sorted(kwargs))}"
            if "agent_id" in kwargs:
                # Same rationale as cron.add: the agent picks the sandboxed
                # subprocess, so the value belongs in the audit trail.
                audit_resources += f" agent={kwargs['agent_id'] or 'default'}"
            sel().log_api_access(
                caller="cli",
                operation="cron.update",
                outcome="allowed",
                source="cli",
                resources=audit_resources,
            )
            print(f"Updated job: {updated.id} ({updated.name})")
        else:

            sel().log_api_access(
                caller="cli",
                operation="cron.update",
                outcome="not_found",
                source="cli",
                resources=f"job_id={args.job_id} reason=not_found",
            )
            print(f"Job not found: {args.job_id}")

    elif action == "remove":
        removed = svc.remove_job(args.job_id, actor="cli", source="cli")
        if removed:
            print(f"Removed job: {args.job_id}")
        else:
            print(f"Job not found: {args.job_id}")

    elif action == "pause":
        if svc.enable_job(args.job_id, enabled=False):
            print(f"Paused job: {args.job_id}")
        else:
            print(f"Job not found: {args.job_id}")

    elif action == "resume":
        if svc.enable_job(args.job_id, enabled=True):
            print(f"Resumed job: {args.job_id}")
        else:
            print(f"Job not found: {args.job_id}")

    elif action == "trigger":
        # Instance-aware, for the same reason as the MCP trigger: DASHBOARD_PORT reads
        # KIROCREW_PORT only, so on a --port auto gateway it names a sibling, and the
        # paired credential would let that sibling run the job.
        port, _evidence_backed = resolve_client_port_ex(None)
        secret_path = config_dir() / ".local_secret"
        ok, msg = trigger_cron_job(args.job_id, port, secret_path)
        print(msg)
        sel().log_api_access(
            caller="cli",
            operation="cron.trigger",
            outcome="allowed" if ok else "error",
            source="cli",
            resources=f"job_id={args.job_id}",
        )

    elif action == "preview":
        _cron_preview(args)

    else:
        print("Usage: kirocrew cron {list|add|update|remove|pause|resume|trigger|preview}")


def _cron_preview(args: argparse.Namespace) -> None:
    """Dry-run a script cron with real MCP tools but suppressed hooks."""
    # Imported here (not at module top) to avoid a cron_script import cycle.
    from kiro_crew.cron_script import Done, McpToolClient, Report, Skip, resolve_script_path

    # Resolve and validate script path (same validation as production cron runner:
    # format, existence, sensitive path, containment under ~/.kiro/crew/crons/)
    try:
        script_path, func_name = resolve_script_path(args.script)
    except (ValueError, FileNotFoundError, PermissionError) as e:
        sel().log_api_access(
            caller="cli",
            operation="cron.preview",
            outcome="denied",
            source="cli",
            resources=f"script={args.script} reason={type(e).__name__}",
        )
        print(f"Error: {e}")
        sys.exit(1)

    # Set env vars before loading module so top-level code can see them
    for kv in args.env or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            os.environ[k] = v

    # Load the script module
    spec = importlib.util.spec_from_file_location("_cron_preview_module", script_path)
    if spec is None or spec.loader is None:
        print(f"Error: cannot load {script_path}")
        sys.exit(1)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if func is None:
        print(f"Error: function '{func_name}' not found in {script_path}")
        sys.exit(1)
    if inspect.iscoroutinefunction(func):
        print(
            f"Error: function '{func_name}' is async; cron preview only supports synchronous functions"
        )
        sys.exit(1)

    @dataclass
    class _PreviewJob:
        id: str = "preview-dry-run"

    class _LiveTestCtx:
        """Dry-run ctx: real MCP tools, suppressed hooks.

        Runs in-process (not sandboxed) unlike production's run_script_sandboxed.
        Acceptable because: scripts are constrained to ~/.kiro/crew/crons/ via
        resolve_script_path, and the command is user-initiated from their terminal."""

        def __init__(self, message: str):
            self.message = message
            self.job = _PreviewJob()

        def call_tool(self, server: str, tool: str, tool_args: dict) -> str:
            # Redact credentials/exfiltration URLs (same as production ScriptContext.call_tool)
            args_str = json.dumps(tool_args)
            args_str = redact(args_str)
            safe_args = json.loads(args_str)
            # Per-call spawn + close (same lifecycle as production ScriptContext.call_tool)
            client = McpToolClient(server)
            outcome = "ok"
            try:
                result = client.call_tool(tool, safe_args)
            except Exception:
                outcome = "error"
                raise
            finally:
                client.close()
                sel().log_tool_invocation(
                    session_key=f"cron:{self.job.id}",
                    tool_name=f"{server}/{tool}",
                    tool_kind="cron_preview_tool",
                    outcome=outcome,
                )
            return result

        def notify(self, text: str, **kwargs: object) -> dict:
            # Signature mirrors production ScriptContext.notify: scripts pass routing
            # kwargs (session="origin" is the documented way for a cron to reach the
            # chat that created it), so a positional-only stub made preview crash on
            # the one branch a monitor cron exists for. Redaction mirrors production
            # too -- this prints to the user's terminal, and kwargs values are
            # script-supplied. Returns an empty dict: nothing was delivered.
            safe_text = redact(text)
            safe_kwargs = json.loads(redact(json.dumps(kwargs))) if kwargs else {}
            routing = f" (kwargs: {safe_kwargs})" if safe_kwargs else ""
            print(f"[notify suppressed]: {safe_text}{routing}")
            return {}

        def close(self):
            pass

    ctx = _LiveTestCtx(message=args.message)
    outcome = "ok"
    try:
        func(ctx)
        print("\n✅ Completed (no exception raised)")
    except Skip:
        print("\n⏭️  Skip (nothing to report)")
    except Report as r:
        print(f"\n📢 Report:\n{r}")
    except Done as d:
        print(f"\n🏁 Done:\n{d}")
    except Exception as e:
        outcome = "error"
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        ctx.close()
        sel().log_api_access(
            caller="cli",
            operation="cron.preview",
            outcome=outcome,
            source="cli",
            resources=f"script={script_path}:{func_name}",
        )
    if outcome == "error":
        sys.exit(1)


_TIME_SELECTOR_RE = re.compile(r"(?i)\A(?P<value>\d+)(?P<unit>[smhdw])\Z")
_TIME_SELECTOR_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def parse_time_selector(raw: str, *, now: datetime | None = None) -> datetime | None:
    """Resolve a ``--since``/``--until`` selector to an aware UTC datetime.

    Accepts a relative AGE (``30m``, ``2h``, ``7d``, ``1w``) meaning "that long
    ago", or an absolute ISO 8601 instant (``2026-08-21``,
    ``2026-08-21T04:00:00Z``). Empty input means "no bound" and returns ``None``.

    A bare ISO date/time with no offset is read as UTC — the audit log is written
    in UTC, so interpreting it as local time would silently shift the window by
    the host's offset. ``Z`` is normalized because ``fromisoformat`` only accepts
    it from Python 3.11 and this package supports 3.10.

    Raises ``ValueError`` with the accepted forms spelled out, so a typo gets a
    usable message instead of an empty result the caller reads as "no events".
    An absurd but well-formed age (``999999999999999999w``) overflows
    ``timedelta``; that surfaces as the same ``ValueError`` rather than an
    uncaught ``OverflowError`` traceback, so the CLI still exits 2 with guidance.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = _TIME_SELECTOR_RE.match(text)
    if match:
        seconds = int(match.group("value")) * _TIME_SELECTOR_UNITS[match.group("unit").lower()]
        try:
            return (now or datetime.now(tz=timezone.utc)) - timedelta(seconds=seconds)
        except (OverflowError, OSError):
            raise ValueError(
                f"{raw!r} is too far in the past to represent. Use a smaller age "
                "(30m, 2h, 7d, 1w) or an ISO 8601 instant."
            ) from None
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"cannot read {raw!r} as a time. Use a relative age (30m, 2h, 7d, 1w) "
            "or an ISO 8601 instant (2026-08-21, 2026-08-21T04:00:00Z)."
        ) from None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _security(args: argparse.Namespace) -> None:
    """Security audit and deny list commands."""

    action = getattr(args, "sec_action", None)
    if action == "deny-list":
        print("🔒 Built-in deny patterns (always enforced):")
        for p in BUILTIN_DENY_PATTERNS:
            print(f"  ✗ {p}")
        cfg_path = config_dir() / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text())
            extra = data.get("hooks", {}).get("auto_deny_tools", [])
            if extra:
                print("\n🔧 User-configured deny patterns:")
                for p in extra:
                    print(f"  ✗ {p}")
    elif action == "audit":
        history_dir = config_dir() / "history"
        findings = scan_history(history_dir)
        if findings:
            print(f"⚠️  {len(findings)} suspicious entries found:\n")
            for f in findings:
                print(f"  📄 {f['file']}")
                print(f"     {f['warning']}")
                print(f"     {f['snippet'][:120]}…\n")
        else:
            print("✅ No suspicious tool usage found in recent history.")

        mem_findings = scan_memory()
        if mem_findings:
            print(f"\n⚠️  {len(mem_findings)} suspicious memory entries:\n")
            for f in mem_findings:
                print(f"  [{f['type']}] {f['key']}: {f['warning']}")
                print(f"    {f['value'][:120]}\n")
        elif not findings:
            pass
        else:
            print("✅ No suspicious content in vector memory.")
    elif action == "events":

        limit = getattr(args, "limit", 20)
        try:
            since = parse_time_selector(getattr(args, "since", "") or "")
            until = parse_time_selector(getattr(args, "until", "") or "")
        except ValueError as exc:
            print(f"❌ {exc}")
            sys.exit(2)
        if since and until and since >= until:
            print("❌ --since must be earlier than --until")
            sys.exit(2)
        events = sel().recent(limit=limit, since=since, until=until)
        window = ""
        if since or until:
            window = (
                f" in [{since.isoformat() if since else '-'}, "
                f"{until.isoformat() if until else 'now'})"
            )
        if not events:
            print(f"No security events recorded{window}.")
            return
        print(f"📋 Last {len(events)} security event(s){window}:\n")
        for e in events:
            ts = e.get("timestamp", "?")[:19]
            etype = e.get("event_type", "?")
            op = e.get("operation", "?")
            outcome = e.get("outcome", "?")
            src = e.get("source", "?")
            caller = e.get("caller_identity", "?")
            print(f"  {ts}  [{src}] {etype}: {op} → {outcome}  (caller: {caller})")
            if e.get("error"):
                print(f"    error: {e['error'][:120]}")
            if e.get("downstream_service"):
                print(f"    downstream: {e['downstream_service']}")
    elif action == "verify":

        # detailed=True: a segment dir that refused to pin (or was swapped
        # mid-verification) leaves the ROTATED segments unchecked, and the
        # command whose job is to surface tampering must not call that run
        # "intact" over the live log alone (#5051 review).
        result = sel().verify_integrity(detailed=True)
        if not result.history_verifiable:
            # The live-log clause is derived from the SAME pass's counts, so
            # it can never claim "intact" over entries that did not verify.
            if result.total and result.total != result.valid:
                live = (
                    f"the live log shows tampered entries: "
                    f"{result.valid}/{result.total} entries valid"
                )
            elif result.total:
                live = f"the live log verified intact: {result.valid}/{result.total} entries"
            else:
                live = "no events to verify"
            print(
                f"⚠️  Audit history UNVERIFIABLE: {result.reason}. "
                f"Rotated segments were not checked — {live}."
            )
        elif result.total == 0:
            print("No security events to verify.")
        elif result.total == result.valid:
            print(f"✅ HMAC chain intact: {result.total} entries verified.")
        else:
            print(
                f"⚠️  HMAC chain COMPROMISED: {result.valid}/{result.total} entries "
                f"valid, {result.total - result.valid} tampered."
            )
    else:
        print("Usage: kirocrew security {audit|deny-list|events|verify}")


def _print_denied_command_summary(*, ids: bool) -> None:
    """Print the built-in denied-command catalog as grouped counts (or, with
    ``--ids``, each category's rule ids).

    The built-in rules are visible and configurable to the USER (Settings
    → Security renders them in category accordions, backed by
    ``GET /api/security/denied-commands``) but were invisible to the AGENT --
    ``policy show`` reported everything except them, so an agent planning a
    multi-step task had no way to learn a class of work is hard-denied before
    committing to a plan that turns out to be impossible. See issue #3454.

    Deliberately just counts + ids, not the full 139 regex patterns: enough
    for planning ("this class of work is blocked") and for citing a rule id
    when relaying a refusal, without bloating the output the way dumping
    every pattern would.
    """
    by_category: dict[str, list] = {}
    for rule in BUILTIN_DENIED_RULES:
        by_category.setdefault(rule.category, []).append(rule)
    counts = Counter({cat: len(rules) for cat, rules in by_category.items()})
    print(
        f"   • commands.denied: {len(BUILTIN_DENIED_RULES)} rules "
        f"in {len(by_category)} categories"
    )
    if ids:
        for cat, rules in sorted(by_category.items(), key=lambda kv: -len(kv[1])):
            rule_ids = ", ".join(r.id for r in rules)
            print(f"       {cat}({len(rules)}): {rule_ids}")
    else:
        summary = " ".join(f"{cat}({n})" for cat, n in counts.most_common())
        print(f"       {summary}")
        print("     (add --ids for rule ids, or see Settings → Security)")


def _policy(args: argparse.Namespace) -> None:
    """Governance policy + profile inspection (read-only; safe to expose to LLM).

    Mirrors the ``security`` command shape.  Boot already ran (cli.main calls
    ``boot_platform`` first), so ``current_context().governance`` carries the
    effective ceiling.  No mutation — purely diagnostic, so it is MCP-safe.
    """
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance import (
        CAPABILITY,
        SCOPE_CATALOG,
        gate_decision,
        resolve,
    )
    from kiro_crew.platform.governance_profiles import (
        get_store_profile,
        resolve_active_scope,
    )

    action = getattr(args, "policy_action", None)
    ceiling = getattr(current_context(), "governance", None)

    if action == "show":
        if ceiling is None:
            print("No enterprise security policy is active (editable secure-defaults).")
            _print_denied_command_summary(ids=getattr(args, "ids", False))
            return
        # Report the PROVEN provenance, not the claimed one: printing a bare
        # issuer implied a trust decision nothing had made.  signature_summary()
        # distinguishes verified / signed-but-unverified / unsigned so an operator
        # can tell an established issuer from a decorative one.
        print(f"🛡️  Security policy v{ceiling.version}")
        print(f"   provenance: {ceiling.signature_summary()}")
        print(
            f"   boot: require_sandbox={ceiling.boot.require_sandbox} "
            f"allow_terminal={ceiling.boot.allow_terminal} fail_closed={ceiling.boot.fail_closed}"
        )
        if not ceiling.controls:
            print("   (no governed scopes)")
        for scope in sorted(ceiling.controls):
            print(f"   • {scope}: {ceiling.controls[scope]}")
        _print_denied_command_summary(ids=getattr(args, "ids", False))

    elif action == "validate":
        ok = True
        if ceiling is None:
            print("Policy: none (editable secure-defaults) — nothing to validate.")
        else:
            print(f"Policy: v{ceiling.version} OK ({len(ceiling.controls)} governed scopes).")
            # A capability the policy does not name is UNGOVERNED, and an
            # ungoverned control is permitted — omission never denies (see the
            # CAPABILITY-DEFAULT CONTRACT in platform/governance.py). That is the
            # same rule every other archetype follows, but it is the one authors
            # most often get wrong, because a partial `capabilities` block LOOKS
            # like a complete statement. Report the gap so an unpinned row reads
            # as a choice instead of an oversight.
            unnamed = sorted(
                scope
                for scope, spec in SCOPE_CATALOG.items()
                if spec.kind == CAPABILITY and scope not in ceiling.controls
            )
            if unnamed and len(unnamed) < sum(
                1 for spec in SCOPE_CATALOG.values() if spec.kind == CAPABILITY
            ):
                print(
                    f"   ⚠️  governs capabilities but leaves {len(unnamed)} "
                    "row(s) UNGOVERNED (therefore permitted):"
                )
                for scope in unnamed:
                    print(f"        {scope}")
                print(
                    "      Omission does not deny. Name each row explicitly "
                    "(enabled true or false) if you meant to decide it."
                )
        # Force-load every profile; the store records invalid ones as deny-all.
        from kiro_crew.platform.governance_profiles import _profiles_dir

        pdir = _profiles_dir()
        if pdir.is_dir():
            for f in sorted(pdir.glob("*.json")):
                prof = get_store_profile(f.stem)
                status = "OK" if prof and not prof.name.startswith("_deny") else "INVALID→deny-all"
                if status != "OK":
                    ok = False
                print(f"   profile {f.name}: {status}")
        else:
            print("   (no profiles directory)")
        print("✅ valid" if ok else "⚠️  some profiles failed validation (fell back to deny-all)")

    elif action == "explain":
        scope = args.scope
        if scope not in SCOPE_CATALOG:
            print(f"Unknown scope {scope!r}. Known: {', '.join(sorted(SCOPE_CATALOG))}")
            return
        profile = resolve_active_scope(args.session_key, agent=args.agent, app=args.app)
        decision = resolve(ceiling, profile, scope, args.item)
        verdict = "ALLOWED" if decision.permitted else "DENIED"
        print(f"{verdict}: {scope} → {args.item!r}")
        print(f"   surface session: {args.session_key!r}")
        print(f"   active profile : {profile.name if profile else '(none — policy only)'}")
        print(f"   rule/layer     : {decision.rule} / {decision.layer or '—'}")
        print(f"   reason         : {decision.reason}")
        # Also show the raw title-classified path (mirrors the live gate).
        gate = gate_decision(ceiling, profile, args.item)
        print(f"   gate verdict   : {'ALLOWED' if gate.permitted else 'DENIED'} ({gate.reason})")

    elif action == "profile":
        prof = get_store_profile(args.name)
        if prof is None:
            print(f"No profile named {args.name!r} in ~/.kiro/crew/profiles/.")
            return
        bind = f"{prof.bind.type}:{prof.bind.id}" if prof.bind else "(unbound)"
        print(f"📄 Profile {prof.name!r}  bind={bind}  extends={prof.extends or '—'}")
        if not prof.controls:
            print("   (no governed scopes — inherits policy ceiling unchanged)")
        for scope in sorted(prof.controls):
            print(f"   • {scope}: {prof.controls[scope]}")

    elif action == "source":
        _print_policy_source()

    elif action == "fetch":
        _policy_fetch(force=getattr(args, "force", False))

    else:
        print(
            "Usage: kirocrew policy {show|validate|explain <scope> <item>|"
            "profile <name>|source|fetch}"
        )


def _print_policy_source() -> None:
    """Report whether this host follows a centrally distributed ceiling.

    Prints the source's SCHEME rather than its URL, matching what the dashboard
    viewer exposes: this command is reachable from a shell the agent may drive, and
    the endpoint is the fleet's control plane. An operator who needs the URL reads
    it from the policy file or the environment, out of band.
    """
    from kiro_crew.platform.policy_distribution import (
        POLICY_URL_ENV,
        distribution_posture,
        registered_policy_schemes,
    )

    posture = distribution_posture()
    if posture.get("error_code"):
        print(
            "⚠️  Central policy distribution is misconfigured; see the gateway log "
            "for the reason and check the 'distribution' block in your policy."
        )
        return
    if not posture.get("configured"):
        print("Central policy distribution: not configured (this host uses a local policy).")
        print(
            f"   Set {POLICY_URL_ENV}, or add a 'distribution' block to the policy, to enable it."
        )
        print(f"   Transports available: {', '.join(registered_policy_schemes())}")
        return

    interval = posture.get("refresh_interval_seconds") or 0
    print("🌐 Central policy distribution: ACTIVE")
    print(f"   transport        : {posture.get('source_scheme') or '—'}")
    print(f"   refresh          : {f'every {interval}s' if interval else 'at boot only'}")
    print(f"   polling now      : {'yes' if posture.get('refresher_running') else 'no'}")
    max_age = posture.get("max_cache_age_seconds") or 0
    print(f"   staleness bound  : {f'{max_age}s' if max_age else 'none'}")
    print(f"   if unavailable   : {posture.get('on_unavailable')}")
    if posture.get("cache_present"):
        print(f"   cached copy      : {posture.get('cache_age_seconds')}s old")
    else:
        print("   cached copy      : none (an outage would leave this host with no ceiling)")
    if posture.get("last_refresh_status"):
        print(
            f"   last refresh     : {posture['last_refresh_status']} "
            f"({posture.get('last_refresh_age_seconds')}s ago)"
        )


def _policy_fetch(*, force: bool) -> None:
    """Fetch the central policy now, applying it when it is usable.

    Exits non-zero on a refusal or an unreachable source so this is usable as a
    fleet-verification step in a config-management run: an admin rolling a change
    needs a check that FAILS on the host that did not take it, not one that prints
    a warning into a log nobody reads.

    **What it can and cannot claim.** ``refresh_now`` installs the ceiling in the calling
    process, and this process exits immediately — so a bare "applied" would overclaim: a
    running gateway is a different process and keeps its own ceiling until its refresher
    polls. What this command really establishes is that the endpoint serves a document
    this host accepts, and that the document is now the host's last-known-good. The
    message says which of those happened and when a running gateway takes it, because a
    boot-only source (no ``refresh_interval_secs``) has no next cycle to take it on.
    """
    from kiro_crew.platform.policy_distribution import (
        REFRESH_APPLIED,
        REFRESH_NOT_CONFIGURED,
        REFRESH_REJECTED,
        REFRESH_UNCHANGED,
        effective_refresh_interval,
        refresh_now,
    )

    outcome = refresh_now(force=force)
    if outcome.status == REFRESH_NOT_CONFIGURED:
        print("Central policy distribution is not configured; nothing to fetch.")
        return
    if outcome.status == REFRESH_UNCHANGED:
        print("✅ The central policy is unchanged; this host is current.")
        if outcome.detail:
            print(f"   {outcome.detail}")
        return
    if outcome.status == REFRESH_APPLIED:
        print("✅ Fetched a valid governance ceiling and cached it as this host's own.")
        if outcome.signature_state:
            print(f"   provenance: {outcome.signature_state}")
        interval = effective_refresh_interval()
        if interval:
            print(
                f"   A running gateway adopts it within {interval}s, on its next refresh; "
                "a newly started one adopts it immediately."
            )
        else:
            print(
                "   This source is boot-only (no refresh_interval_secs), so a gateway "
                "already running keeps its current ceiling until it is restarted. Set a "
                "refresh interval if a push should bind without one."
            )
        return
    # Rejected or unreachable. The running ceiling is untouched either way, which
    # is worth saying: an operator reading a failure needs to know whether the host
    # is now ungoverned (it is not).
    # "was refused", not "refused": the policy is the object of the refusal, not the
    # thing doing it.
    label = "was refused" if outcome.status == REFRESH_REJECTED else "could not be reached"
    print(f"❌ The central policy {label}: {outcome.detail}")
    print("   The ceiling already in effect is unchanged.")
    raise SystemExit(1)


async def _run_eval(args: argparse.Namespace) -> None:
    """Run multi-session evaluation scenarios."""

    scenarios_dir = Path(__file__).resolve().parent / "eval" / "scenarios"

    if args.all_scenarios:
        scenarios = load_scenarios(scenarios_dir)
    elif args.scenarios:
        scenarios = []
        for name in args.scenarios:
            resolved = None
            for ext in (".json", ".yaml", ".yml"):
                candidate = scenarios_dir / f"{name}{ext}"
                if candidate.exists():
                    resolved = candidate
                    break
            if resolved is None:
                available = sorted(
                    f.stem
                    for f in scenarios_dir.iterdir()
                    if f.suffix in (".json", ".yaml", ".yml")
                )
                print(f"Error: scenario '{name}' not found.")
                print(f"Available scenarios: {', '.join(available)}")
                return
            scenarios.append(load_scenario(resolved))
    else:
        scenarios = [load_scenario(scenarios_dir / "smoke_test.json")]

    total_turns = sum(len(sess.turns) for s in scenarios for sess in s.sessions)
    names = ", ".join(s.name for s in scenarios)
    print(f"Running: {names} ({total_turns} turns)\n")

    config = KiroCrewConfig.load()
    provider_factory = build_provider_factory(config)

    runner = EvalRunner(
        provider_factory=provider_factory, judge_enabled=getattr(args, "judge", False)
    )
    results = await runner.run_scenarios(scenarios)

    # LLM Judge scoring
    if getattr(args, "judge", False):
        judge = LLMJudge(provider_factory=provider_factory)
        await judge.start()
        try:
            for scenario, result in zip(scenarios, results):
                criteria = scenario.judge_criteria or scenario.description
                for sr in result.sessions:
                    for tr in sr.turns:
                        for idx, (a, _) in enumerate(tr.assertion_results):
                            if a.type == AssertionType.JUDGE:
                                try:
                                    verdict = await judge.judge_turn(
                                        scenario.description,
                                        a.value or criteria,
                                        tr.user_message,
                                        tr.agent_response,
                                    )
                                    tr.assertion_results[idx] = (
                                        a,
                                        verdict.score >= judge.pass_threshold,
                                    )
                                    reason, _ = redact_exfiltration_urls(verdict.reason)
                                    reason, _ = redact_credentials(reason)
                                    print(f"  🧑‍⚖️ Judge: {verdict.score}/5 — {reason}")
                                except Exception as exc:
                                    print(f"  ⚠️ Judge failed for turn: {exc}")
                                    tr.assertion_results[idx] = (a, False)
        finally:
            await judge.shutdown()

    report = format_results(results)
    print("\n" + report)

    dims = score_by_dimension(results)
    if dims:
        print("## Dimension Summary")
        for dim, s in sorted(dims.items()):
            status = "✅" if s["rate"] >= 0.75 else "❌"
            print(f"  {status} {dim}: {s['passed']}/{s['total']} ({s['rate']:.0%})")

    overall = sum(1 for r in results if r.passed)
    print(f"\nOverall: {overall}/{len(results)} scenarios passed")

    # Save results
    results_dir = Path.cwd() / "eval_results"
    results_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    report_path = results_dir / f"eval_{ts}.md"
    report_path.write_text(report + "\n")

    json_path = results_dir / f"eval_{ts}.json"
    json_data = {
        "timestamp": ts,
        "scenarios": [r.summary() for r in results],
        "dimensions": dims,
        "overall_passed": overall,
        "overall_total": len(results),
    }
    json_path.write_text(json.dumps(json_data, indent=2) + "\n")

    print(f"\nResults saved to:\n  {report_path}\n  {json_path}")


# Appended to every `learn add` output that wrote something. This command builds
# its store with no embed_fn (loading the embedding model would add its startup
# cost to every CLI invocation), so an insert lands with a NULL vector and an
# enrichment CLEARS the stored one (the upsert keeps a vector only when the value
# is unchanged). Either way the row is repaired by the gateway's boot-time
# re-embed sweep, not by this process — an unqualified success message would
# overstate what happened, and a user searching semantically before the next
# gateway start would not find the lesson they were just told was saved.
# "once its embedding backend is ready" is the sweep's own guarantee, not
# hedging: _wait_then_backfill defers the sweep to a later boot when the
# embedding model has not landed, so "on its next start" would over-promise.
_LEARN_EMBED_NOTE = (
    "  Stored and keyword-searchable now; the embedding vector is filled by the\n"
    "  gateway's re-embed sweep after it next starts, once its embedding backend\n"
    "  is ready."
)

# INSERTED only. An enrichment resolves against the ONE existing row it rewrites
# (write_lesson pass 1 sets ``matched`` and pass 2's generic scan runs over
# ``[] if matched else lesson_rows``), so the substring/topic-overlap claim is
# true only for an insert — printing it on ENRICHED would report checks that
# never ran, the same defect this change fixes.
_LEARN_DEDUP_NOTE = (
    "  (Semantic dedup did not run at write time; substring/topic-overlap dedup\n" "  still did.)"
)


def _learn(args: argparse.Namespace) -> None:
    """Save, list, or remove learned corrections."""

    jsonl_store = LessonStore()
    cfg = KiroCrewConfig.load()
    vs = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim)
    vs.init()
    try:
        action = getattr(args, "learn_action", None)

        if action == "add":
            rule = args.rule
            category = args.category
            negative = getattr(args, "negative", None)
            # The reporting form, not the bool. This command used to read EVERY falsy
            # return as "the vector store did not take it" and write a second record
            # into lessons.jsonl. Most of those returns mean the opposite -- the
            # lesson is already stored exactly as submitted -- so the fallback wrote a
            # redundant record for a lesson that was fine, and printed "Saved:" when
            # nothing needed saving.
            #
            # The one return that really does mean "nothing was stored" is a REFUSAL,
            # and routing that into the JSONL store was worse than redundant: that
            # store validates no content at all, so a value this store rejected (an
            # injection-pattern clause) landed there anyway, and the context builder
            # reads lessons.jsonl whenever the vector store holds no lessons.
            #
            # So the fallback is REMOVED, not narrowed. There is no "vector store
            # unavailable" state to fall back from here: ``vs`` is constructed and
            # ``init()``-ed unconditionally above, which means a falsy return was the
            # only way into that branch.
            result = vs.write_lesson(rule, category, negative)
            neg = f" ({negative})" if negative else ""
            # The category is echoed ONLY where the store adopted the submitted one.
            # It is write-once (vector_memory.py builds an enrichment with the STORED
            # category, falling back to the submitted one only when the row has none),
            # so an insert is the single outcome where what was typed is what is held.
            # Anything else printing it would show a value the store may not have --
            # the same defect this PR fixes on the reporting side. `learn list` is
            # where stored values belong.
            if result.outcome is LessonWriteOutcome.INSERTED:
                print(f"Saved: {rule}{neg} [{category}]\n{_LEARN_EMBED_NOTE}\n{_LEARN_DEDUP_NOTE}")
            elif result.outcome is LessonWriteOutcome.ENRICHED:
                # No _LEARN_DEDUP_NOTE here: an enrichment matched its existing row in
                # pass 1, which SKIPS the generic dedup scan. Instead say what the
                # rewrite cost — the upsert cleared the vector the row already had.
                print(
                    f"Updated the stored lesson with this clause: {rule}{neg}\n"
                    "  The stored category is kept; `learn list` shows it.\n"
                    "  This rewrite cleared the row's existing embedding vector.\n"
                    + _LEARN_EMBED_NOTE
                )
            elif result.outcome is LessonWriteOutcome.UNCHANGED:
                # Nothing was written, and the store keeps the stored category and
                # NOT-clause rather than rewriting them on a re-submit.
                print(
                    f"Already stored, nothing written: {rule}\n"
                    "  A re-submit keeps the stored category and NOT-clause; "
                    "changing one means `learn remove` then `learn add`."
                )
            elif result.outcome is LessonWriteOutcome.DEDUPED:
                print(f"Not saved: an existing lesson already covers this ({result.reason})")
            else:
                # REFUSED -- and any outcome a later change adds, which is deliberate:
                # every branch above names ONE outcome, so a new one lands here and
                # exits non-zero rather than being silently reported as a success. A
                # non-zero exit so a script driving this command sees the failure.
                reason = f" ({result.reason})" if result.reason else ""
                print(f"NOT saved: the memory store refused this lesson{reason}", file=sys.stderr)
                sys.exit(1)

        elif action == "list":
            vs_lessons = vs.get_lessons()
            if vs_lessons:
                for e in vs_lessons:
                    val = json.loads(e["value_json"])
                    # Rendered text for either storage shape: mapping-shaped rows
                    # (write_lesson's format and the onboarding import's) would
                    # otherwise print as a Python dict repr.
                    #
                    # The label reads the row's own category so this surface agrees
                    # with the dashboard's lessons panel; a legacy string row
                    # carries none, and the shared helper supplies the store's
                    # own "knowledge" default (display policy, strict=False).
                    category = normalize_lesson_category(
                        val.get("category") if isinstance(val, dict) else None,
                        strict=False,
                    )
                    text = _TERMINAL_CTRL_RE.sub("", _lesson_display_text(val) or str(val))
                    print(f"  [{_TERMINAL_CTRL_RE.sub('', category)}] {text}")
            else:
                lessons = jsonl_store.load_all()
                if not lessons:
                    print("No lessons.")
                    return
                for le in lessons:
                    neg = f" — {_TERMINAL_CTRL_RE.sub('', str(le.negative))}" if le.negative else ""
                    # Same display policy as the vector-store branch above and
                    # the dashboard's JSONL path: a blank/legacy category gets
                    # the store's own "knowledge" default instead of printing [].
                    category = normalize_lesson_category(le.category, strict=False)
                    print(
                        f"  [{_TERMINAL_CTRL_RE.sub('', category)}] {_TERMINAL_CTRL_RE.sub('', str(le.rule))}{neg}"
                    )

        elif action == "remove":
            if vs.get_lessons() and vs.delete_lesson(args.query):
                print(f"Removed lessons matching: {args.query}")
            elif jsonl_store.remove(args.query):
                print(f"Removed lessons matching: {args.query}")
            else:
                print(f"No lessons match: {args.query}")

        else:
            print("Usage: kirocrew learn {add|list|remove}")
    finally:
        vs.close()


def _markdown_memory_store() -> MemoryStore:
    """MemoryStore anchored where the DEFAULT runtime writer writes.

    The markdown layer this surface exposes is written by the gateway's
    consolidator, which is constructed over a bare ``MemoryStore()`` (the
    hard-coded ``workspace`` dir under the data home). The reader must resolve
    identically or an install whose config maps the default workspace
    elsewhere would export a tree the consolidator never writes to. In a stock
    config this is the same directory ``workspace_dir_for()`` returns; when
    they diverge, the writer wins.
    """
    return MemoryStore()


def _memory_search_history(args: argparse.Namespace) -> None:
    """Print FTS5 hits from the markdown memory layer.

    Reads the same index the heartbeat and gateway keep current, so this needs
    no embedder and no vector store — it answers "where did I write this word"
    against preferences, projects and the dated daily-history files.

    Resolves the store through ``_markdown_memory_store`` for the reason spelled
    out there: the reader must anchor exactly where the gateway's consolidator
    writes, or a config that remaps the default workspace searches a tree
    nothing writes to.
    """
    store = _markdown_memory_store()
    results = store.search(args.query, limit=10)
    if not results:
        # An empty index is not the same statement as an absent word, so the two
        # are reported differently.
        if not store.index_row_count():
            print("Memory history index is empty or unavailable; nothing was searched.")
        else:
            print("No memory-history matches.")
        return
    print("  Daily history / preferences / projects:")
    for r in results:
        # Strip terminal control sequences for the same reason the semantic
        # listing does: memory holds whatever the user pasted into a session.
        path = _TERMINAL_CTRL_RE.sub("", str(r.get("path", "?")))
        snippet = _TERMINAL_CTRL_RE.sub("", str(r.get("snippet", ""))).strip()
        print(f"    {path}")
        if snippet:
            print(f"        {snippet}")


def _memory_show(args: argparse.Namespace) -> None:
    """Read-only view of the markdown memory layer (preferences/projects/history).

    Routes through :class:`MemoryStore`'s own readers so consumers depend on an
    interface rather than the on-disk layout. A missing or empty file is a
    normal state and prints as empty rather than erroring. Validation failures
    go to stderr with a non-zero exit so scheduled (non-TTY) consumers get a
    real failure signal instead of non-JSON text on stdout.
    """
    target = getattr(args, "target", None)
    since_raw = getattr(args, "since", None)
    since = None
    if since_raw:
        if target not in (None, "history"):
            print("--since applies only to history", file=sys.stderr)
            sys.exit(1)
        try:
            since = datetime.strptime(since_raw, "%Y-%m-%d").date()
        except ValueError:
            print(f"Invalid --since date (expected YYYY-MM-DD): {since_raw}", file=sys.stderr)
            sys.exit(1)
    snapshot = _markdown_memory_store().markdown_snapshot(since=since)
    if getattr(args, "format", "md") == "json":
        payload = snapshot[target] if target else snapshot
        print(json.dumps(payload, indent=2))
        return
    parts: list[str] = []
    for key in [target] if target else ["preferences", "projects", "history"]:
        if key == "history":
            parts.extend(e["content"].strip() for e in snapshot[key] if e["content"].strip())
        elif snapshot[key]["content"].strip():
            parts.append(snapshot[key]["content"].strip())
    text = "\n\n".join(parts)
    if text:
        # The markdown layer is consolidator (LLM) written — strip terminal
        # control sequences before printing, same policy as `memory list`.
        print(_TERMINAL_CTRL_RE.sub("", text))


def _memory_cmd(args: argparse.Namespace) -> None:
    """Manage the memory system (vector store + markdown layer)."""
    action = getattr(args, "mem_action", None)
    # "show" reads only the markdown layer — don't open (or create) the
    # vector store for it.
    if action == "show":
        _memory_show(args)
        return
    # "search --layer history" reads only the markdown FTS index, so don't open
    # (or create) the vector store for it — same reason as "show" above.
    if action == "search" and getattr(args, "layer", "all") == "history":
        _memory_search_history(args)
        return
    cfg = KiroCrewConfig.load()
    store = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim)
    store.init()
    try:
        if action == "list":
            entries = store.get_all_semantic()
            if not entries:
                print("No semantic memory entries.")
                return
            for e in entries:
                try:
                    val = json.loads(e["value_json"])
                except Exception:
                    val = e["value_json"]
                # A lesson row stores its rule and NOT-clause as separate fields,
                # so printing the decoded value would show a Python dict repr on
                # this surface while every other reader shows the prose.
                if str(e["key"]).startswith("lesson."):
                    val = _lesson_display_text(val) or val
                safe_val = _TERMINAL_CTRL_RE.sub("", str(val))
                print(
                    f"  {e['key']}: {safe_val}  (confidence={e['confidence']}, source={e['source']})"
                )

        elif action == "search":
            layer = getattr(args, "layer", "all")
            if layer in ("vector", "all"):
                results = store.search_episodic(query_text=args.query, limit=10)
                if not results:
                    print("No episodic memories found.")
                    # Under "all" the markdown layer is still to come: an empty
                    # vector result is not an empty answer.
                    if layer == "vector":
                        return
                elif layer == "all":
                    # Both sections are printed, so both are named. Unlabelled,
                    # the first block of hits reads as the whole answer. Held
                    # back under "vector", whose output shape is a promise.
                    print("  Episodic recall:")
                for r in results:
                    tags = (
                        json.loads(r.get("tags", "[]"))
                        if isinstance(r.get("tags"), str)
                        else r.get("tags", [])
                    )
                    print(f"  [{r.get('importance', 0):.1f}] {r['text'][:120]}")
                    if tags:
                        print(f"        tags: {', '.join(tags)}")
            if layer == "all":
                _memory_search_history(args)

        elif action == "stats":
            stats = store.memory_stats()
            print(
                f"  Semantic: {stats['semantic_active']} active, {stats['semantic_deleted']} deleted"
            )
            print(
                f"  Episodic: {stats['episodic_active']} active, {stats['episodic_deleted']} deleted"
            )
            print(f"  Embedded: {stats['embedded_count']}/{stats['episodic_active']}")
            if stats["faiss_available"]:
                print(f"  FAISS accelerator: {stats['faiss_index_size']} vectors indexed")
            else:
                print("  FAISS accelerator: not installed — stdlib cosine fallback (exact)")
            print(f"  Audit events: {stats['events_count']}")

        elif action == "audit":
            findings = scan_memory()
            if findings:
                print(f"⚠️  {len(findings)} suspicious entries:\n")
                for f in findings:
                    print(f"  [{f['type']}] {f['key']}: {f['warning']}")
                    print(f"    {f['value'][:120]}\n")
            else:
                print("✅ No suspicious content in memory.")

        elif action == "export":
            data: dict[str, object] = {
                "semantic": store.get_all_semantic(),
                "episodic": store.get_episodic_list(limit=10000),
                "events": store.get_events(limit=1000),
            }
            if getattr(args, "include_markdown", False):
                # Opt-in so the default payload shape stays byte-identical
                # for existing consumers.
                data["markdown"] = _markdown_memory_store().markdown_snapshot()
            output = json.dumps(data, indent=2, default=str)
            out_file = getattr(args, "output", None)
            if out_file:
                Path(out_file).write_text(output, encoding="utf-8")
                print(f"Exported to {out_file}")
            else:
                print(output)

        elif action == "migrate":
            counts = store.migrate_from_markdown()
            print(f"Migration complete:")  # noqa: F541
            print(f"  Semantic: {counts['semantic']}")
            print(f"  Episodic: {counts['episodic']}")
            print(f"  Skipped:  {counts['skipped']}")

        elif action == "import":
            import_file = getattr(args, "file", None)
            if not import_file:
                print("Usage: kirocrew memory import <file>")
                return
            path = Path(import_file)
            if not path.is_file():
                print(f"File not found: {import_file}")
                return
            data = json.loads(safe_read_file(str(path)))
            counts = store.import_memory(data)
            print(f"Import complete:")  # noqa: F541
            print(f"  Semantic: {counts['semantic']}")
            print(f"  Episodic: {counts['episodic']}")
            print(f"  Skipped:  {counts['skipped']}")
            if "markdown" in data:
                # The markdown collection is export-only: the markdown layer is
                # consolidator-owned, so import never writes it. Say so rather
                # than letting a backup/restore silently drop it.
                print(
                    "Note: the 'markdown' collection is export-only and was NOT imported "
                    "(the markdown memory layer has no write path here)."
                )

        else:
            print("Usage: kirocrew memory {list|search|show|stats|audit|export|migrate|import}")
    finally:
        store.close()


def _artifact(args: argparse.Namespace) -> None:
    """List, save, view, update, or delete artifacts."""
    cfg = KiroCrewConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    base = f"http://localhost:{port}"

    action = getattr(args, "artifact_action", None) or "list"

    headers: dict[str, str] = {"X-Internal-Secret": _internal_secret(port)}

    def _request(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers)
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{base}{path}", data=data, headers=h, method=method)
        try:
            with loopback_urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                return {"error": json.loads(exc.read()).get("error", str(exc))}
            except Exception:
                return {"error": str(exc)}
        except Exception as exc:
            return {"error": str(exc)}

    def _read_content(args: argparse.Namespace) -> str:
        if getattr(args, "content_file", None):
            p = Path(args.content_file).expanduser().resolve()
            if is_sensitive_path(str(p)):
                # Defense in depth: refuse to read credential files even
                # though the artifact API would also redact on serialize.
                print(
                    f"Error: refusing to read sensitive path: {p}",
                    file=sys.stderr,
                )
                sys.exit(1)
            return p.read_text(encoding="utf-8")
        if getattr(args, "content", None):
            return args.content
        if not sys.stdin.isatty():
            return sys.stdin.read()
        print(
            "Error: provide --content, --content-file, or pipe content via stdin", file=sys.stderr
        )
        sys.exit(1)

    def _parse_tags(s: str | None) -> list[str]:
        if not s:
            return []
        return [t.strip() for t in s.split(",") if t.strip()]

    if action == "list":
        params: list[str] = []
        for k in ("tag", "kind", "q"):
            v = getattr(args, k, None)
            if v:
                params.append(f"{k}={urllib.parse.quote(v)}")
        path = "/api/artifacts" + (f"?{'&'.join(params)}" if params else "")
        d = _request("GET", path)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        items = d.get("artifacts", [])
        if not items:
            print("No artifacts.")
            return
        for a in items:
            tags = f"  [{', '.join(a.get('tags') or [])}]" if a.get("tags") else ""
            print(
                f"{a.get('slug', '?'):<40s}  v{a.get('version', '?'):<3} "
                f"{a.get('kind', '?'):<10}{tags}  {a.get('name', '?')}"
            )
        return

    if action == "show":
        slug = args.slug
        version = getattr(args, "version", None)
        path = f"/api/artifacts/{slug}"
        if version:
            path = f"/api/artifacts/{slug}/versions/{int(version)}"
        d = _request("GET", path)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        if getattr(args, "meta", False):
            d.pop("content", None)
            print(json.dumps(d, indent=2))
        else:
            print(d.get("content") or "")
        return

    if action == "save":
        body: dict = {
            "name": args.name,
            "content": _read_content(args),
            "tags": _parse_tags(getattr(args, "tags", None)),
        }
        # An explicit --slug is forwarded whenever it is not None, INCLUDING the
        # empty string. "" is a slug the caller named, not a request to derive
        # one, and the store refuses it; truthy filtering would swallow it and
        # silently take the derive-and-suffix branch instead.
        slug_arg = getattr(args, "slug", None)
        if slug_arg is not None:
            body["slug"] = slug_arg
        for k in ("kind", "description"):
            v = getattr(args, k, None)
            if v:
                body[k] = v
        d = _request("POST", "/api/artifacts", body)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        slug = d.get("slug", "?")
        print(f"Saved: slug={slug} version={d.get('version', 1)}")
        # Present only when the slug was derived from --name, because that is the
        # branch where a taken slug resolves by suffixing. That reads as success
        # (exit 0, "version=1"), which is how a re-save of corrected content ends
        # up published at a slug nobody looks at while the original keeps serving
        # the old text. Name the slug that was taken and the verb that versions in
        # place. An explicit --slug cannot land here: the store refuses it rather
        # than renaming — 409 when the slug is taken, 400 when it is malformed
        # (the empty string included) — so both surface on the error path above.
        taken = d.get("slug_collided_with")
        if taken:
            print(
                f"Warning: slug '{taken}' is already taken, so this created a NEW "
                f"artifact at '{slug}' rather than a new version of '{taken}'. "
                f"To version the existing artifact in place, use: "
                f"kirocrew artifact update {taken}",
                file=sys.stderr,
            )
        # Relayed from the gateway (same pattern as slug_collided_with): the
        # handler computes the theme-contrast verdict at the convergence
        # point, so this CLI path -- the one the motivating incident traveled
        # -- hears it without duplicating the detector. Advisory only.
        if d.get("theme_contrast_warning"):
            print(
                "Warning: content hardcodes colors with no theme variables — it "
                "will clash in dark/light/custom dashboard themes. Prefer "
                "var(--text,#111) / var(--bg,#fff) style fallbacks (see the "
                "widgets skill's theme table).",
                file=sys.stderr,
            )
        return

    if action == "update":
        slug = args.slug
        body = {}
        # Only read stdin when explicit content args are absent. In non-
        # interactive environments (CI, cron, piped /dev/null) sys.stdin.isatty()
        # returns False even when the user only intends a metadata update —
        # if we read stdin unconditionally, an empty pipe would send
        # content="" and wipe the artifact's content. Require an explicit
        # content arg or non-empty stdin to overwrite content.
        if getattr(args, "content", None) or getattr(args, "content_file", None):
            body["content"] = _read_content(args)
        elif not sys.stdin.isatty():
            stdin_data = sys.stdin.read()
            if stdin_data:
                body["content"] = stdin_data
        if getattr(args, "name", None):
            body["name"] = args.name
        if getattr(args, "description", None) is not None:
            body["description"] = args.description
        if getattr(args, "tags", None) is not None:
            body["tags"] = _parse_tags(args.tags)
        if not body:
            print("Error: provide content/--name/--description/--tags", file=sys.stderr)
            sys.exit(1)
        d = _request("PATCH", f"/api/artifacts/{slug}", body)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"Updated: slug={d.get('slug', slug)} version={d.get('version', '?')}")
        # Relayed from the gateway (see the save action above) -- the PATCH
        # path is exactly how the motivating incident's script pushed its
        # hardcoded-palette page. Advisory only; the update succeeded.
        if d.get("theme_contrast_warning"):
            print(
                "Warning: content hardcodes colors with no theme variables — it "
                "will clash in dark/light/custom dashboard themes. Prefer "
                "var(--text,#111) / var(--bg,#fff) style fallbacks (see the "
                "widgets skill's theme table).",
                file=sys.stderr,
            )
        return

    if action == "delete":
        slug = args.slug
        d = _request("DELETE", f"/api/artifacts/{slug}")
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"Deleted: {slug}")
        return

    if action == "versions":
        slug = args.slug
        d = _request("GET", f"/api/artifacts/{slug}/versions")
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        versions = d.get("versions", [])
        if not versions:
            print(f"No versions for {slug}.")
        else:
            print(", ".join(f"v{v}" for v in versions))
        return

    print(
        "Usage: kirocrew artifact {list|show|save|update|delete|versions}",
        file=sys.stderr,
    )
    sys.exit(2)


def _pod(args: argparse.Namespace) -> None:
    """Dispatch ``kirocrew pod <verb>`` to the pod verb layer (isolated worktree
    test instances)."""
    from kiro_crew.pod.cli import dispatch

    dispatch(args)


def _container_valued_sections() -> dict[str, type]:
    """Top-level config keys the model expects to be a JSON object or array.

    Derived from the dataclass rather than hardcoded, so a section added to
    ``KiroCrewConfig`` later is covered without anyone remembering to edit this.
    Both shapes matter: a nested-config or mapping field must be an object, and a
    ``list[...]`` field must be an array — the loader *iterates* the latter, so a
    scalar there is an uncaught ``TypeError`` rather than a merge that loses data.
    """
    out: dict[str, type] = {}
    for field in dataclasses.fields(KiroCrewConfig):
        ann = field.type
        if isinstance(ann, str):  # from __future__ import annotations
            if ann.startswith("list"):
                out[field.name] = list
            elif ann.endswith("Config") or ann.startswith("dict"):
                out[field.name] = dict
            continue
        origin = getattr(ann, "__origin__", None)
        if origin is list:
            out[field.name] = list
        elif origin is dict or ann is dict:
            out[field.name] = dict
        elif dataclasses.is_dataclass(ann):
            out[field.name] = dict
    return out


def _assert_config_sections_are_objects(raw: dict) -> None:
    """Refuse a config whose section values have the wrong shape, before anything loads it.

    Not just the sections this command writes: this runs ahead of
    ``KiroCrewConfig.load()``, and ``load()`` is itself destructive on a file it
    cannot parse — its migration write-back **rewrites the file**, so a section it
    chokes on is replaced by defaults merely because a read-only command like
    ``tailnet status`` was run. ``{"slack": 5}`` was enough to destroy the operator's
    Slack settings that way, and ``{"registries": 5}`` is worse: the loader iterates
    that field, so a scalar ends the command in an uncaught ``TypeError``.

    Coercing a wrong type would discard whatever the operator had there and still
    report success, so a wrong shape is a refusal, never something to normalise.
    """
    for name, expected in sorted(_container_valued_sections().items()):
        value = raw.get(name)
        if value is None or isinstance(value, expected):
            continue
        want = "an object" if expected is dict else "an array"
        raise ConfigReadError(
            f'"{name}" is {type(value).__name__}, not {want}; refusing to run '
            "because loading this file would replace it with defaults"
        )
    tailscale = (raw.get("dashboard") or {}).get("tailscale")
    if tailscale is not None and not isinstance(tailscale, dict):
        raise ConfigReadError(
            f'"dashboard.tailscale" is {type(tailscale).__name__}, not an object; '
            "refusing to replace it"
        )


def _tailnet(args: argparse.Namespace) -> None:
    """Publish, withdraw, or inspect tailnet dashboard access (``kirocrew tailnet``).

    The command that was missing. Reaching the dashboard from another device on
    your tailnet has always taken **two** independent steps — publish it with
    ``tailscale serve``, and tell the gateway to trust the resulting origin — and
    Kiro Crew only ever did the second. Doing one without the other is the failure
    this exists to remove: publish without trusting and every request is refused
    by the Origin check with a bare 403; trust without publishing and there is
    nothing on the tailnet to open.

    So ``up`` does both, in the order that cannot leave a half-state visible: it
    publishes first and only records the config once publishing succeeded. A
    config write followed by a failed publish would leave a host claiming tailnet
    access is on with nothing serving it.
    """
    action = getattr(args, "tailnet_action", None) or "status"
    # Validate the raw file BEFORE anything calls ``KiroCrewConfig.load()``, for
    # EVERY action. ``load()`` performs a migration write-back, so a config that is
    # valid JSON but wrongly typed (``{"dashboard": 5}``) gets normalised — and
    # therefore silently rewritten — by the mere act of reading it. That makes even
    # ``status`` a write, which is indefensible for a command that reports state.
    #
    # An earlier revision guarded only ``up``, reasoning that refusing to *report* or
    # to *withdraw* over a malformed config would be the worse failure. That reasoning
    # had a hole: both paths need the dashboard port, which resolves through
    # ``resolve_client_port`` → ``KiroCrewConfig.load()``, so there is no version of
    # them that reads the file without rewriting it. Given the choice between
    # rewriting the operator's config and declining, declining wins — and withdrawal
    # stays ACHIEVABLE because the refusal prints the exact daemon command.
    #
    # BOTH files, not just the base one. ``load()`` merges ``config.local.json`` over
    # ``config.json``, so a wrongly-typed section in the overlay reaches the loader
    # just as surely -- `config set --local registries 5` is enough -- and a scalar
    # where a list is expected is iterated, ending the command in a traceback rather
    # than a refusal. Guarding only the base file left the overlay as an open door to
    # the very failure the guard exists to prevent.
    bad_path: Path | None = None
    try:
        for candidate in (config_path(), config_local_path()):
            bad_path = candidate
            if candidate.exists():
                _assert_config_sections_are_objects(read_config_for_update(candidate))
        bad_path = None
    except ConfigReadError as exc:
        print(
            f"❌ {bad_path} is not usable ({exc}); refusing to continue, because "
            "even reading it would rewrite it. Fix that file, then retry.",
            file=sys.stderr,
        )
        if action == "down":
            print(
                "   To withdraw right now without touching the config, run: "
                f"`tailscale serve --https {tailnet_serve.SERVE_HTTPS_PORT} "
                f"--set-path={tailnet_serve.SERVE_MOUNT} off`",
                file=sys.stderr,
            )
        sys.exit(1)
    cfg = KiroCrewConfig.load()
    # Evidence before intent. ``resolve_client_port`` ranks the configured
    # ``dashboard.url`` ABOVE the run marker, which is right for a client that wants
    # to talk to the dashboard the operator configured — and wrong here. If the
    # configured port was occupied and the gateway moved (``--port``), publishing in
    # front of the configured port aims `tailscale serve` at whatever unrelated
    # loopback service now holds it, exposing it on the tailnet. So the verified run
    # marker wins: it only reports a port where a Kiro Crew gateway process is actually
    # listening (`_gateway_owns_port`, and it refuses when several are up), which is
    # evidence, whereas ``dashboard.url`` is a statement of intent.
    #
    # An explicit ``--port``/``KIROCREW_PORT`` still wins over both, because that is
    # the operator naming the target directly — hence the marker is consulted only
    # when neither is set.
    # An explicit --port outranks everything: it is the operator naming the target,
    # which no discovery heuristic should override.
    port = int(getattr(args, "port", None) or 0)
    port_source = "the --port you gave" if port else ""
    if not port and os.environ.get("KIROCREW_PORT") is not None:
        port_source = "KIROCREW_PORT"
    if not port and not port_source:
        try:
            port = _marker_port() or 0
        except Exception:  # pragma: no cover - discovery must never break the command
            port = 0
        if port:
            port_source = "the running gateway's run marker"
    if not port:
        # ``resolve_client_port`` also carries the guard for a non-string
        # ``dashboard.url`` (user-editable JSON can hold ``"url": 123``, and urlparse
        # raises TypeError on it), so it stays the fallback rather than a hand-rolled
        # parse that would have to repeat that guard.
        port = resolve_client_port(None)
    enabled = bool(cfg.dashboard.tailscale.enabled)

    if action == "up" and not port_source:
        # Publishing needs EVIDENCE about the port, not a default. Every source above
        # is evidence -- an explicit flag/env is the operator naming the target, and
        # the run marker only reports a port a Kiro Crew gateway is actually listening
        # on. `resolve_client_port` is not: it falls back to the configured
        # `dashboard.url` (or the built-in default) whether or not anything answers
        # there. `tailscale serve` does not care what is behind the port -- so if the
        # gateway is down, or moved after its configured port was taken, publishing
        # that number puts WHATEVER now holds it on the tailnet, for every device on
        # it. A private service exposed tailnet-wide is not a recoverable mistake, so
        # this refuses rather than guesses. `status` and `down` still accept the
        # fallback: one only reports, and the other checks mount ownership before
        # removing anything.
        print(
            f"❌ Cannot tell which port the dashboard is on, so refusing to publish "
            f"{port} — nothing is verified to be listening there, and `tailscale "
            f"serve` would expose whatever is. Start the dashboard "
            f"(`kirocrew dashboard`) and re-run, or name the port yourself with "
            f"`kirocrew tailnet up --port <port>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    if action == "status":
        pinned = tailnet.is_governance_pinned_off()
        # A LIVE read is correct here and would be wrong in the dashboard's status
        # endpoint. This command reports what the machine can do next; the
        # endpoint reports what the running server already trusts, which is the
        # startup value. Conflating them is how "resolvable" gets rendered as
        # "in the allowlist".
        name = tailnet.self_dns_name()
        state = tailnet_serve.serve_state(port)
        print("👻 Tailnet dashboard access")
        if pinned:
            print(
                "   Policy:     PINNED OFF by your administrator " "(capabilities.tailnet_origin)"
            )
        print(
            f"   Trust:      {'enabled' if enabled else 'disabled'} "
            f"(dashboard.tailscale.enabled)"
        )
        print(f"   Name:       {name or '— (no tailnet name resolvable right now)'}")
        published = state.published
        label = {True: "yes", False: "no", None: "unknown"}[published]
        print(f"   Published:  {label} — {state.detail}")
        if name and published is True:
            print(f"   URL:        https://{name}")
        if name and enabled and published is not True:
            print("   Next:       kirocrew tailnet up")
        return

    if action not in ("up", "down"):
        print(f"❌ Unknown tailnet action: {action}", file=sys.stderr)
        sys.exit(1)

    if action == "down":
        result = tailnet_serve.unpublish(port)
        print(("✅ " if result.ok else "❌ ") + result.detail)
        if result.ok:
            # The trust setting is deliberately left ON. It contributes one origin
            # that nothing can reach while serve is off, so clearing it would be
            # an unrequested second change — and would force a gateway restart to
            # undo a withdrawal that took effect immediately.
            print(
                "   dashboard.tailscale.enabled is unchanged; the trusted origin "
                "is unreachable while serve is off."
            )
        sys.exit(0 if result.ok else 1)

    if not enabled:
        # Checked BEFORE publishing, and never written. Three reasons this is a
        # check rather than the config write it used to be:
        #
        # 1. A read-modify-write of the shared config cannot be made atomic from a
        #    second process. Every construction tried here -- a caller-side
        #    fingerprint, a lock plus compare-and-swap inside the shared writer, a
        #    lock plus digest local to this command -- leaves some window where a
        #    dashboard save landing mid-flight is replaced by our older snapshot, or
        #    (when the lock went into the shared writer) blocks the gateway's event
        #    loop. Closing it needs one primitive that ALL ~29 writers take, which is
        #    #2147, not this feature.
        # 2. Failing here beats the alternative ordering. Writing after publishing
        #    left a published-but-untrusted dashboard whenever the write failed --
        #    reachable on the tailnet and answering 403, which is the confusing state
        #    this command exists to eliminate.
        # 3. The cost is paid once per machine, not per invocation. After the operator
        #    enables the setting, `kirocrew tailnet up` is a single command forever;
        #    the one-time step is the same `config set` they would run anyway.
        #
        # `cfg` is the EFFECTIVE value, so an overlay in config.local.json that
        # disables this is caught here too -- printing "published" while the gateway
        # will still refuse the origin is the exact false promise to avoid.
        print(
            "❌ dashboard.tailscale.enabled is false, so the gateway would refuse "
            "your tailnet origin even once published — refusing to publish a "
            "dashboard that would answer 403.\n"
            "   Enable it once, then re-run this command:\n"
            "     kirocrew config set dashboard.tailscale.enabled true\n"
            "   (If config.local.json disables it, set it there instead: "
            "`kirocrew config set --local dashboard.tailscale.enabled true`.)",
            file=sys.stderr,
        )
        sys.exit(1)

    result = tailnet_serve.publish(port)
    if not result.ok:
        print(f"❌ {result.detail}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ {result.detail}")

    name = tailnet.self_dns_name()
    if name:
        print(f"👻 URL:        https://{name}")
    else:
        print(
            "⚠️  No tailnet name is resolvable right now, so the gateway will not "
            "trust anything on restart. Check `tailscale status`."
        )
    # Said unconditionally, including when the switch was already on: the origin
    # is resolved once at startup, so a gateway that booted before this command
    # has an allowlist that does not contain the name yet.
    print("👻 Restart the gateway for the tailnet origin to be trusted.")


def _telemetry(args: argparse.Namespace) -> None:
    """Inspect or toggle the anonymous usage beacon (``kirocrew telemetry``).

    ``status`` is read-only and never materializes an install id. ``disable`` /
    ``enable`` persist ``telemetry.beacon_enabled`` to config.json, so the choice
    survives restarts and upgrades — an env-var-only opt-out would silently lapse
    the next time the user launched from a different shell.
    """
    action = getattr(args, "telemetry_action", None) or "status"
    cfg = KiroCrewConfig.load()

    if action == "status":
        print(
            beacon.format_status(
                beacon.status(
                    cfg.telemetry.beacon_endpoint,
                    enabled=cfg.telemetry.beacon_enabled,
                    app_version=__version__,
                    acked=cfg.dashboard.privacy_acked,
                )
            )
        )
        return

    if action not in ("disable", "enable"):
        print(f"❌ Unknown telemetry action: {action}", file=sys.stderr)
        sys.exit(1)

    want = action == "enable"
    # Refuse a re-enable an enterprise ceiling has pinned off, mirroring the
    # dashboard PATCH route's 403. Without this the CLI would write
    # beacon_enabled: true and print "ENABLED" on a host where should_send()
    # blocks every heartbeat — the exact false-promise-on-a-privacy-control this
    # command's overlay check below already exists to prevent. Only the ENABLE
    # direction is gated: writing false is always allowed (tightest-wins).
    # audit_tool: this is an ENFORCEMENT decision (it refuses a write), so it
    # routes through the audited seam and lands a governance_decision SEL record —
    # same disposition as the send gate. A distinct tool name per call site keeps
    # the trail readable about WHICH control refused.
    if want and beacon.is_governance_pinned_off(audit_tool="telemetry_enable_cli"):
        print(
            "❌ The anonymous beacon is pinned OFF by your administrator's "
            "security policy (capabilities.telemetry).",
            file=sys.stderr,
        )
        print(
            "   Not writing config.json — the setting would have no effect.",
            file=sys.stderr,
        )
        sys.exit(1)
    path = config_path()

    def _mutate_telemetry(data: dict) -> dict:
        """Apply telemetry toggle inside the config lock."""
        if not isinstance(data, dict):
            # Should not happen (read_config_for_update rejects non-objects),
            # but guard defensively.
            print(
                f"❌ {path} is not a JSON object ({type(data).__name__}); refusing to "
                "overwrite it. Fix or move the file, then retry.",
                file=sys.stderr,
            )
            sys.exit(1)
        # Same rule as the whole-file check above, applied per section: coercing a
        # non-object section to {} would DISCARD whatever the user had there and then
        # print success. Absent is fine (create it); present-but-wrong-type is a
        # refusal, because this command cannot know what the value was meant to be.
        sections: dict[str, dict[str, object]] = {}
        for name in ("telemetry", "dashboard"):
            existing = data.get(name)
            if existing is None:
                sections[name] = {}
                continue
            if not isinstance(existing, dict):
                print(
                    f'❌ {path} has a non-object "{name}" value '
                    f"({type(existing).__name__}); refusing to overwrite it. Fix or "
                    "remove it, then retry.",
                    file=sys.stderr,
                )
                sys.exit(1)
            sections[name] = existing

        sections["telemetry"]["beacon_enabled"] = want
        data["telemetry"] = sections["telemetry"]
        # Running this command IS the informed choice the first-run chapter exists to
        # collect, so record the ack. Otherwise `telemetry enable` on a fresh
        # headless install would write beacon_enabled: true and still send nothing,
        # because the first-egress gate would keep waiting for a dashboard screen the
        # user may never open.
        sections["dashboard"]["privacy_acked"] = True
        data["dashboard"] = sections["dashboard"]
        return data

    try:
        update_config_locked(path, mutate=_mutate_telemetry, fsync=True, stamp_meta=False)
        # restrict_to_owner: the atomic write creates a NEW inode, so without
        # this an operator's tightened mode is silently replaced by the umask
        # default.  config.json can hold inline credentials, so a telemetry
        # toggle must never widen who can read it.  The locked helper preserves
        # mode on POSIX; restrict_to_owner applies the owner-only DACL on
        # Windows (and 0600 on POSIX for new files). Fail-loud so a lockdown
        # that cannot be applied surfaces rather than silently leaving the file
        # wide open.
        try:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        except OSError:
            mode = 0o600
        if not platform_compat.IS_POSIX or mode == 0o600:
            platform_compat.restrict_to_owner(path)
    except ConfigReadError as exc:
        err_str = str(exc)
        if "not a JSON object" in err_str:
            print(
                f"❌ {path} is not a JSON object; refusing to "
                "overwrite it. Fix or move the file, then retry.",
                file=sys.stderr,
            )
        else:
            print(f"❌ Could not read {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"❌ Could not write {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    # Verify the write actually took EFFECT before claiming success.
    # config.local.json deep-merges OVER config.json at load, so a host that
    # previously set this key locally would keep sending while this command
    # printed "DISABLED" — a false promise on a privacy control is worse than an
    # error, so re-read the effective config and report the shadowing file.
    try:
        effective = KiroCrewConfig.load().telemetry.beacon_enabled
    except Exception:  # noqa: BLE001 - diagnostics must not mask the write
        effective = want
    if effective != want:
        state = "ENABLED" if effective else "DISABLED"
        print(
            f"⚠️  Wrote {path.name}, but the beacon is still {state}: an overlay "
            "in config.local.json takes precedence.",
            file=sys.stderr,
        )
        print(
            "   Edit telemetry.beacon_enabled there too, or export "
            f"{beacon.DISABLE_ENV}=1 to override everything.",
            file=sys.stderr,
        )
        sys.exit(1)

    if want:
        print("✅ Anonymous usage beacon ENABLED (one heartbeat per day).")
        print("   Run 'kirocrew telemetry status' to see exactly what is sent.")
    else:
        print("✅ Anonymous usage beacon DISABLED. Nothing will be sent.")
        print(f"   You can also delete {beacon.INSTALL_ID_FILE} from the data home.")


def _handle_secrets(args: argparse.Namespace) -> None:
    """Dispatch secrets subcommands. Currently only ``import`` (migration)."""

    action = getattr(args, "secrets_action", None)

    if action == "import":
        # Import ONLY from the fixed data-home .env — never an arbitrary path.
        # A caller-supplied file would let a sandbox-off agent import attacker
        # Jira values into the vault and have the vault-first consumer trust
        # them, so there is deliberately no --file option.
        # A concurrent .env change or an undecryptable pre-existing vault entry
        # aborts the migration with MigrationConflictError. That is an expected
        # operational condition (retry after the concurrent write settles, or
        # repair the vault entry), so surface it as a clean CLI error with a
        # nonzero exit — never an uncaught traceback.
        try:
            report = migrate_env_secrets(dry_run=not args.apply)
        except MigrationConflictError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(1)
        except (OSError, ValueError) as exc:
            # A truncated/corrupt `secrets.enc` makes the vault's `list_names()`
            # (or a decrypt) raise `ValueError`/`OSError` rather than
            # `MigrationConflictError`. Surface it as the same concise CLI error
            # with a nonzero exit instead of an uncaught traceback — the store is
            # unreadable, which the operator must repair before importing.
            print(
                f"error: could not read the secrets vault "
                f"({exc.__class__.__name__}: {exc}); repair or remove the vault "
                f"store, then re-run `kirocrew secrets import --apply`.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(format_report(report))
    else:
        print("Usage: kirocrew secrets import [--apply]", file=sys.stderr)
        sys.exit(1)
