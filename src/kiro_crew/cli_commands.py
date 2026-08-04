"""CLI subcommand handlers — cron, spawn, workspace, app, agent, security, eval, learn, memory."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import shutil
import stat
import sys
import time as _time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import __version__, beacon, platform_compat
from kiro_crew.apps.bridges import (
    deregister_app,
    deregister_app_crons_from_service,
    register_app,
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
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import config_dir
from kiro_crew.config.loader import (
    DASHBOARD_PORT,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    WorkspaceConfig,
    build_provider_factory,
    config_path,
)
from kiro_crew.cron import CronSchedule, CronService, format_schedule
from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.dashboard.origin import parse_dashboard_url
from kiro_crew.eval.judge import LLMJudge
from kiro_crew.eval.runner import EvalRunner, format_results, score_by_dimension
from kiro_crew.eval.scenario import AssertionType, load_scenario, load_scenarios
from kiro_crew.hooks import safe_read_file
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.security import (
    BUILTIN_DENY_PATTERNS,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    scan_history,
    scan_memory,
)
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE, CHANNEL_ID_RE, CHANNEL_MAX_LEN, WORKSPACE_NAME_RE
from kiro_crew.vector_memory import VectorMemoryStore

# Workspace dirs are confined to the data home: a workspace is agent-writable
# working state, so letting --dir escape would let it be pointed at ~/.ssh or the
# keystone policy files. The refusal is deliberate — say so, and say what to pass
# instead, rather than the bare "invalid directory path" this used to print.
_WS_DIR_OUTSIDE_HOME = (
    "Error: --dir must resolve inside the KiroCrew data home ({home}); got {given!r}. "
    "Pass a relative directory name (e.g. 'workspace-myproject')."
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


def _internal_secret() -> str:
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
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except Exception:
        return ""


def _spawn(args: argparse.Namespace) -> None:
    """Dispatch spawn subcommands: run, list."""
    base = f"http://localhost:{args.port}"
    action = getattr(args, "spawn_action", None)

    if action == "list":
        req = urllib.request.Request(
            f"{base}/api/spawn",
            headers={"X-Internal-Secret": _internal_secret()},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
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
            status = "✅" if a.get("done") else "⏳"
            print(f"  {status} {a['id']}  {a.get('task', '')[:60]}")
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
            "X-Internal-Secret": _internal_secret(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
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
    secret = _internal_secret()
    while True:
        _time.sleep(2)
        poll_req = urllib.request.Request(poll_url, headers={"X-Internal-Secret": secret})
        try:
            with urllib.request.urlopen(poll_req, timeout=5) as resp:
                status = json.loads(resp.read())
        except Exception:
            print("Error: lost connection to gateway", file=sys.stderr)
            sys.exit(1)
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
    except ImportError as exc:
        print(f"App {app_name!r} has no MCP server ({module_name}): {exc}", file=sys.stderr)
        sys.exit(1)
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
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "disable":
        _cleanup_app_crons_from_scheduler(args.name)
        deregister_app(args.name)
        result = disable_app(args.name)
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
        app_dir = scaffold_app(
            output,
            args.name,
            include_backend=include_backend,
            include_ui=include_ui,
            include_cron=include_cron,
        )
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

    else:
        print("Usage: kirocrew agent {list|create|update|delete}")


def _cron(args: argparse.Namespace) -> None:
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
        if channel:

            if len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
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
        for field in ("name", "message", "every_secs", "cron_expr", "channel"):
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
        updated = svc.update_job(args.job_id, **kwargs)
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
        if svc.remove_job(args.job_id):
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
        port = DASHBOARD_PORT
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

        def notify(self, message: str) -> None:
            print(f"[notify suppressed]: {message}")

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
        events = sel().recent(limit=limit)
        if not events:
            print("No security events recorded.")
            return
        print(f"📋 Last {len(events)} security event(s):\n")
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

        total, valid = sel().verify_integrity()
        if total == 0:
            print("No security events to verify.")
        elif total == valid:
            print(f"✅ HMAC chain intact: {total} entries verified.")
        else:
            print(
                f"⚠️  HMAC chain COMPROMISED: {valid}/{total} entries valid, {total - valid} tampered."
            )
    else:
        print("Usage: kirocrew security {audit|deny-list|events|verify}")


def _policy(args: argparse.Namespace) -> None:
    """Governance policy + profile inspection (read-only; safe to expose to LLM).

    Mirrors the ``security`` command shape.  Boot already ran (cli.main calls
    ``boot_platform`` first), so ``current_context().governance`` carries the
    effective ceiling.  No mutation — purely diagnostic, so it is MCP-safe.
    """
    from kiro_crew.platform.context import current_context
    from kiro_crew.platform.governance import SCOPE_CATALOG, gate_decision, resolve
    from kiro_crew.platform.governance_profiles import (
        get_store_profile,
        resolve_active_scope,
    )

    action = getattr(args, "policy_action", None)
    ceiling = getattr(current_context(), "governance", None)

    if action == "show":
        if ceiling is None:
            print("No enterprise security policy is active (editable secure-defaults).")
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

    elif action == "validate":
        ok = True
        if ceiling is None:
            print("Policy: none (editable secure-defaults) — nothing to validate.")
        else:
            print(f"Policy: v{ceiling.version} OK ({len(ceiling.controls)} governed scopes).")
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

    else:
        print("Usage: kirocrew policy {show|validate|explain <scope> <item>|profile <name>}")


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
            if vs.write_lesson(rule, category, negative):
                neg = f" ({negative})" if negative else ""
                print(f"Saved: {rule}{neg} [{category}]")
            else:
                lesson = Lesson(
                    ts=datetime.now(timezone.utc).isoformat(),
                    rule=rule,
                    category=category,
                    negative=negative,
                )
                jsonl_store.save(lesson)
                neg = f" ({lesson.negative})" if lesson.negative else ""
                print(f"Saved: {lesson.rule}{neg} [{lesson.category}]")

        elif action == "list":
            vs_lessons = vs.get_lessons()
            if vs_lessons:
                for e in vs_lessons:
                    val = json.loads(e["value_json"])
                    print(f"  [knowledge] {val}")
            else:
                lessons = jsonl_store.load_all()
                if not lessons:
                    print("No lessons.")
                    return
                for le in lessons:
                    neg = f" — {le.negative}" if le.negative else ""
                    print(f"  [{le.category}] {le.rule}{neg}")

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


def _memory_cmd(args: argparse.Namespace) -> None:
    """Manage vector memory system."""
    cfg = KiroCrewConfig.load()
    store = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim)
    store.init()
    try:
        action = getattr(args, "mem_action", None)

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
                print(f"  {e['key']}: {val}  (confidence={e['confidence']}, source={e['source']})")

        elif action == "search":
            results = store.search_episodic(query_text=args.query, limit=10)
            if not results:
                print("No episodic memories found.")
                return
            for r in results:
                tags = (
                    json.loads(r.get("tags", "[]"))
                    if isinstance(r.get("tags"), str)
                    else r.get("tags", [])
                )
                print(f"  [{r.get('importance', 0):.1f}] {r['text'][:120]}")
                if tags:
                    print(f"        tags: {', '.join(tags)}")

        elif action == "stats":
            stats = store.memory_stats()
            print(
                f"  Semantic: {stats['semantic_active']} active, {stats['semantic_deleted']} deleted"
            )
            print(
                f"  Episodic: {stats['episodic_active']} active, {stats['episodic_deleted']} deleted"
            )
            print(f"  FAISS index: {stats['faiss_index_size']} vectors")
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
            data = {
                "semantic": store.get_all_semantic(),
                "episodic": store.get_episodic_list(limit=10000),
                "events": store.get_events(limit=1000),
            }
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

        else:
            print("Usage: kirocrew memory {list|search|stats|audit|export|migrate|import}")
    finally:
        store.close()


def _artifact(args: argparse.Namespace) -> None:
    """List, save, view, update, or delete artifacts."""
    cfg = KiroCrewConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    base = f"http://localhost:{port}"

    action = getattr(args, "artifact_action", None) or "list"

    headers: dict[str, str] = {"X-Internal-Secret": _internal_secret()}

    def _request(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers)
        if data is not None:
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{base}{path}", data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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
        for k in ("kind", "description"):
            v = getattr(args, k, None)
            if v:
                body[k] = v
        d = _request("POST", "/api/artifacts", body)
        if d.get("error"):
            print(f"Error: {d['error']}", file=sys.stderr)
            sys.exit(1)
        print(f"Saved: slug={d.get('slug', '?')} version={d.get('version', 1)}")
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
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"❌ Could not read {path}: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        # Refuse rather than replace. Coercing to {} would make this toggle
        # silently overwrite the whole file (a JSON array, string, or number is
        # not a config we can merge into) and then print success — destroying
        # whatever the user had. A toggle must never be a data-loss path.
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
                f"❌ {path} has a non-object \"{name}\" value "
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
    # Preserve the existing permissions. atomic_write creates a NEW file and
    # renames it over the old one, so without this an operator's tightened mode
    # is silently replaced by the umask default (0600 -> 0644 on a typical host).
    # config.json can hold inline credentials, so a telemetry toggle must never
    # widen who can read it. Default 0o600 for a file we are creating.
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    except OSError:
        mode = 0o600
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # atomic_write, never path.write_text: this rewrites the user's WHOLE
        # config.json, so a disk-full or interrupted write would truncate it and
        # every later load would silently discard their configuration. Temp file
        # + rename means the old file survives any failure. fsync so the rename
        # is durable across a power loss.
        atomic_write(path, json.dumps(data, indent=2) + "\n", fsync=True, mode=mode)
        # atomic_write's `mode` is POSIX-only — it routes through fchmod_safe,
        # which is a documented NO-OP on Windows. So on Windows the replacement
        # file inherits the DIRECTORY's ACL, and a permissive data home would make
        # a config.json holding inline credentials readable by other local users.
        # restrict_to_owner applies an owner-only DACL there (and 0600 on POSIX),
        # and is fail-loud, so a lockdown that cannot be applied surfaces below
        # rather than silently leaving the file wide open.
        if not platform_compat.IS_POSIX or mode == 0o600:
            platform_compat.restrict_to_owner(path)
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
