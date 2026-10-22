"""CLI server lifecycle commands — update, stop, token, logout, status, gateway, run."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import enum
import http.client
import importlib as _importlib
import io
import json
import logging
import math
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import __version__, dep_sync, platform_compat
from kiro_crew.beacon import distribution, is_default_home
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    _session_work_dir,
    build_provider_factory,
    config_dir,
    config_path,
    read_local_secret,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import tailnet_serve
from kiro_crew.dashboard.handlers.core import DASHBOARD_HTML_NOT_FOUND_MARKER
from kiro_crew.dashboard.origin import (
    dashboard_origin,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.tailnet import is_governance_pinned_off, tailnet_origin
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.embeddings import (
    make_sync_embed_fn,
    model_file_present,
    store_embedding_space_is_stale,
)
from kiro_crew.env import activate_mise, resolved_command_casing
from kiro_crew.frontend import build_frontend_sync, ensure_dev_dist_symlink
from kiro_crew.gateway_lock import LockProbeError, lock_holder
from kiro_crew.git_divergence import (
    UNREADABLE_UNPARSEABLE,
    DivergenceUnreadable,
    count_divergence_sync,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HookManager, hooks_config_from_config_dict
from kiro_crew.instances import run_marker
from kiro_crew.kiro_cli import PATH_ONLY_INSTALL_NOTE, is_bundled_kiro_cli, pin_kiro_cli
from kiro_crew.learn import LessonStore
from kiro_crew.loopback_http import loopback_urlopen, tcp_verified_urlopen, unix_socket_urlopen
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid
from kiro_crew.memory import MemoryStore
from kiro_crew.owner_only_files import owner_only_opener
from kiro_crew.platform.update_capability import (
    EXTERNALLY_MANAGED_MESSAGES,
    MANAGED_BY_GIT,
    derive_capability,
)

# Client-side port resolution lives in kiro_crew.port_resolution, a light leaf
# module the MCP stdio server can import without paying for this module's
# import graph. Re-exported here (not merely used) because this namespace is
# the historical public surface: external callers and a large body of tests
# import and patch these names as ``kiro_crew.cli_server.<name>``, and the
# chain's internal calls deliberately resolve through this namespace whenever
# it is loaded (see port_resolution._patchable) so those patches keep
# intercepting.
from kiro_crew.port_resolution import (  # noqa: F401
    _KIROCREW_SERVER_SUBCOMMANDS,
    _args_look_like_kirocrew,
    _basename_stem,
    _config_url_port,
    _gateway_owns_port,
    _is_kirocrew_process,
    _marker_port,
    resolve_client_port,
    resolve_client_port_ex,
    resolve_client_port_src,
)
from kiro_crew.preflight import run_preflight_checks
from kiro_crew.sel import sel
from kiro_crew.service import controller as service_controller
from kiro_crew.service import linux as svc_linux
from kiro_crew.service import macos as svc_macos
from kiro_crew.service.common import (
    RESTART_NOT_UP,
    RESTART_REFUSED,
    RESTART_UNCONFIRMED,
    SERVICE_NAME,
    Platform,
    current_platform,
)
from kiro_crew.session import SessionManager
from kiro_crew.skill_usage import register_skill_read_observer
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.gateway import run_gateway
from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.vector_memory import VectorMemoryStore

# NOTE: wheel_engine is imported lazily inside _update_wheel(), not here — it
# pulls the manifest-verify / crypto path, and `kirocrew gateway` imports this
# module on the boot path before the dashboard socket binds
# (no-new-work-on-gateway-boot-path).


def _file_delivery_approve() -> None:
    """Approve a flagged-file delivery consent armed from the dashboard.

    Same step-up shape as :func:`_update_approve`: the proof of host identity is
    READING THE NONCE FILE, which lives on the keystone floor with owner-only
    permissions, so presenting its nonce back to the gateway demonstrates
    filesystem access as the gateway's own user -- the step an owner-authenticated
    but agent-DRIVEN browser cannot perform, which is the hole this closes. The
    gateway records the grant only after the nonce validates.
    """
    from kiro_crew.file_delivery_consent import read_pending_grant

    print("👻 Approving the pending flagged-file delivery consent…\n")
    pending = read_pending_grant()
    if pending is None:
        print("❌ No armed grant request (it may have expired).")
        print("   Confirm from the dashboard's Security panel first, then re-run this.")
        sys.exit(1)
    print(f"  📦 {pending.destination_class}, expires in {pending.expires_in}s")

    port = resolve_client_port(None)
    url = f"http://127.0.0.1:{port}/api/file-delivery/consent/approve"
    payload = json.dumps({"nonce": pending.nonce}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    # Same local-secret / unix-socket authentication as _update_approve: reading
    # the secret is itself host-local evidence, and an absent secret still works
    # on a default loopback install where no token auth runs.
    secret = read_local_secret(port, dial_host="127.0.0.1")
    if secret:
        headers["X-Internal-Secret"] = secret
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        from kiro_crew.dashboard.urls import (  # noqa: F811 - header import serves owners
            dashboard_socket_path,
        )

        socket_path: str | None = str(dashboard_socket_path(port))
    except Exception:
        socket_path = None
    try:
        # NOT `loopback_urlopen`: this request carries the single-use nonce AND
        # the local secret, and that opener's own contract says a caller whose
        # request carries a credential another process could capture on the port
        # wants `unix_socket_urlopen` instead. Its TCP fallback fires on a STALE
        # socket -- exactly the dead-gateway case in which a foreign process may
        # hold the loopback port -- so falling back would hand both credentials
        # to whatever answers. A gateway that is gone is reported as not running
        # rather than retried on a port nothing trustworthy is holding.
        #
        # TCP stays the transport only where there is no unix socket to prefer
        # (native Windows has no `AF_UNIX`): there the port IS the only local
        # transport, so no fallback decision exists to get wrong.
        if socket_path is not None and hasattr(socket, "AF_UNIX"):
            approve_resp = unix_socket_urlopen(req, 15, socket_path=socket_path)
        else:
            approve_resp = loopback_urlopen(req, timeout=15)
        with approve_resp as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("error", "")
        except Exception:
            detail = ""
        print(
            f"❌ Gateway refused the approval (HTTP {e.code})" + (f": {detail}" if detail else "")
        )
        sys.exit(1)
    except (urllib.error.URLError, OSError) as e:
        # A timeout means the gateway took the request and is still working, so
        # it is up but busy, not gone. A refused connection keeps the
        # not-running remedy. The request carries a single-use nonce the gateway
        # may already have consumed, so the busy line does not invite a retry
        # (a re-run would read the nonce as gone and look like a stale arm).
        # Imported in-function: a module-scope import would surface on this
        # split module's star-import facade.
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(e):
            print(
                f"❌ Gateway on port {port} is up but busy (did not answer); it may "
                "have taken the request — check the dashboard before re-running."
            )
        else:
            print(
                "❌ Gateway is not running — start it, then re-run: "
                "kirocrew file-delivery approve"
            )
        sys.exit(1)
    grant = body.get("grant") if isinstance(body, dict) else None
    dest = grant.get("destination_class") if isinstance(grant, dict) else pending.destination_class
    print(f"\n✅ Confirmed delivery to {dest}. The dashboard now shows it as confirmed.")


def _status(args: argparse.Namespace) -> None:
    """Query the running gateway for stats, or print offline message."""
    port = resolve_client_port(getattr(args, "port", None))
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with loopback_urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("Kiro Crew gateway is running (token auth enabled).")
            print("  For detailed stats, see the Overview page in the dashboard.")
        else:
            print(f"Kiro Crew gateway is running but returned HTTP {e.code}.")
        return
    except (urllib.error.URLError, OSError) as e:
        # A timeout means something took the request, so a gateway IS listening
        # -- it is up but busy (a loop thread starved of the GIL), not gone.
        # Saying "not running" here would send the operator to restart a
        # healthy gateway. Imported in-function: a module-scope import would
        # surface on this split module's star-import facade.
        from kiro_crew.loopback_http import deadline_expired

        if deadline_expired(e):
            print(f"Kiro Crew gateway is running on port {port} but is busy (did not answer).")
            print("  It may be under load; try again in a moment.")
        else:
            print("Kiro Crew gateway is not running.")
            print("  Start it with: kirocrew gateway")
        return
    except Exception:
        print("Kiro Crew gateway is running but returned an unexpected response.")
        return

    print(f"Kiro Crew v{__version__} 👻\n")
    print(f"  Uptime:      {data.get('uptime', '—')}")
    print(f"  Sessions:    {data.get('sessions', 0)}")
    print(f"  Messages:    {data.get('messages', 0)}")
    print(f"  Tool calls:  {data.get('tool_calls', 0)}")
    print(f"  Subagents:   {data.get('subagents', 0)}")
    print(f"  Cron jobs:   {_format_count(data, 'cron_jobs')}")
    print(f"  Lessons:     {_format_count(data, 'lessons')}")
    print(f"  Memory:      {_format_memory_line(data)}")


def _format_count(data: dict, key: str) -> str:
    """One of the two cached counts from a status payload, or a dash for unknown.

    ``/api/status`` serves ``cron_jobs`` and ``lessons`` from the gateway-wide
    count cache (``dashboard/status_counts.py``) and publishes ``null`` while
    that cache has not refreshed yet or the store is failing — the same
    unknown the dashboard renders as a loading skeleton. A dash keeps the CLI
    from printing ``None`` or fabricating a zero for a count nobody measured;
    an older gateway that omits the key prints the same dash.
    """
    value = data.get(key)
    return "—" if value is None else str(value)


def _format_memory_line(data: dict) -> str:
    """``<gateway rss> MiB rss, session ceiling <n> MiB`` from a status payload.

    Reads the two fields ``/api/status`` publishes for exactly this line
    (``gateway_rss_mb`` / ``watchdog_rss_max_mb``). An older gateway that does
    not publish them prints a dash rather than a fabricated zero, and a ceiling
    of ``0`` is spelled out as disabled so "0 MiB" cannot read as a bound.
    """
    rss = data.get("gateway_rss_mb")
    ceiling = data.get("watchdog_rss_max_mb")
    rss_text = f"{rss} MiB rss" if isinstance(rss, (int, float)) and rss > 0 else "— rss"
    if not isinstance(ceiling, (int, float)):
        ceiling_text = "session ceiling —"
    elif ceiling > 0:
        ceiling_text = f"session ceiling {int(ceiling)} MiB"
    else:
        ceiling_text = "session ceiling disabled (session.watchdog_rss_max_mb = 0)"
    return f"{rss_text}, {ceiling_text}"


def _should_reconcile_launchd_launcher() -> bool:
    """Whether this gateway may repair the shared launchd launcher.

    Only a production instance running outside the desktop bundle may.

    ``LIVE_PROGRAM`` is a per-user path under Application Support that
    ``KIROCREW_HOME`` does not scope, so a dev, pod, or worktree gateway
    repairing it would repoint the user's REAL agent at its own venv —
    recreating the serving-vs-managed mismatch the reconcile exists to prevent,
    and doing it without the operator ever acting on the production instance.
    ``is_default_home`` is reused rather than re-derived so the two cannot drift
    on what counts as the real home.

    A bundled interpreter is excluded for a different reason: the launchd agent is
    a ``service install`` artifact belonging to a source or pip install, while a
    packaged app manages its own backend lifecycle and supplies environment its
    interpreter needs — notably ``PYTHONPYCACHEPREFIX``, which keeps bytecode out
    of the signed bundle. A launcher naming the bundled executable would be run by
    launchd WITHOUT that environment, so the interpreter would write
    ``__pycache__`` inside the app and invalidate its signature. The packaged app
    has no business owning this artifact at all. The bundle is identified by its
    interpreter's location (:func:`platform_compat.is_bundled_interpreter`), which
    is the one runtime reading of the packaging layout.
    """
    return (
        sys.platform == "darwin"
        and not platform_compat.is_bundled_interpreter()
        and is_default_home()
    )


async def _gateway(
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_tunnel: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
    test_mode: bool = False,
) -> None:
    """Load config and start the gateway (dashboard + configured messaging channels)."""
    # Activate mise once at gateway start so every subprocess we
    # later spawn — MCP servers, script crons, kiro-cli — inherits the user's
    # mise-managed toolchain. Without this, Node-based MCP servers spawn against
    # the system /usr/bin/node (v18 on AL2) and die during `initialize` with a
    # stderr-only "Node version 18 detected" error. No-op when mise is absent.
    _mise_changed = activate_mise()
    if _mise_changed:
        logging.getLogger(__name__).info(
            "Activated mise at gateway start (updated %s)", ", ".join(_mise_changed)
        )

    # Ensure Node >= 16 so frontend builds work (avoids legacy fallback).
    from kiro_crew.cli import _ensure_node, _node_ok  # circular import: cli -> cli_server -> cli

    if not _node_ok():
        _ensure_node()

    # Resolve the dashboard's React build. Skipped in slack-only mode since no
    # dashboard will be served. When the prebuilt dist/ is missing the gateway
    # has no dashboard shell to serve and returns the "not found" guidance page;
    # build the frontend to restore the full dashboard. Off the event loop: under
    # an edition it waits for the staging lock and copies the whole bundle, and
    # a lock acquire on the loop thread never waits at all.
    if not no_dashboard and await asyncio.to_thread(ensure_dev_dist_symlink) is None:
        logging.getLogger(__name__).warning(
            "Dashboard dist/ not found — the dashboard will show the "
            "'not built' guidance page until the SPA is bundled. "
            "Run `npm ci && npm run build` in the website/ directory to build "
            "the full dashboard."
        )

    # Reconcile the other derived artifact that lives outside the install: the
    # launchd agent's launcher script. It sits under Application Support, so a
    # "reset the app" gesture that clears that directory leaves the agent loaded
    # with nothing to execute and no in-product way back. Self-healing here
    # rather than in the service installer keeps a hand-customized plist intact.
    if _should_reconcile_launchd_launcher():
        try:
            svc_macos.ensure_live_program()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Could not restore the launchd live-gateway launcher: %s", exc
            )

    if not config_path().exists():
        cfg = KiroCrewConfig()
        # Boot has installed the context, so an edition's default STT provider is
        # known here; seeding the field default would persist ``local`` as a choice.
        from kiro_crew import speech_providers

        if speech_providers.edition_default_stt() is not None:
            from kiro_crew.config.sections_stt import default_stt_provider

            cfg.stt.provider = default_stt_provider()
        # _gateway is a coroutine, so this runs on the event loop: save() takes
        # the sidecar advisory flock and a contended wait (another
        # process writing config at boot) must block a worker thread, not the
        # loop. run_config_write does not fit here — the dashboard's asyncio
        # config lock guards loop-side handler writers, none of which exist
        # before run_gateway starts serving.
        await asyncio.to_thread(cfg.save)
        print(f"👻 Created default config: {config_path()}")

    cfg = KiroCrewConfig.load()
    await run_gateway(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_tunnel=no_tunnel,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
        test_mode=test_mode,
    )


async def _run_task(args: argparse.Namespace) -> None:
    """Execute a spec file autonomously via TaskRunner."""

    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        print(f"❌ Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    cfg = KiroCrewConfig.load()
    factory = build_provider_factory(cfg)
    sessions = SessionManager(cfg, provider_factory=factory)  # type: ignore[arg-type]

    auto_test = not getattr(args, "no_test", False)
    fresh = getattr(args, "fresh", False)
    timeout = float(getattr(args, "timeout", 0))

    # Initialize history + lessons for learning and memory formation
    memory = MemoryStore()
    memory.init()

    # Vector memory (structured semantic store)

    vector_memory = VectorMemoryStore(embedding_dim=cfg.memory.embedding_dim, config=cfg)
    # CALLER CONTRACT (vector_memory_lifecycle.py): async callers offload init() — it is
    # blocking file IO end to end (sqlite connect, migrations, lockdown pass)
    # and would stall the loop.
    await asyncio.to_thread(vector_memory.init)
    # Embeddings are always-on: wire the factory; bind embed_fn when the model
    # is already present. Deliberately NO download kick here — `kirocrew run`
    # is a one-shot CLI and must not start a 610MB download it will abandon at
    # exit; the long-lived gateway owns the background download.
    vector_memory.embed_fn_factory = make_sync_embed_fn
    if model_file_present():
        vector_memory.embed_fn = make_sync_embed_fn()
    else:
        print(
            "Embedding model not downloaded yet — keyword search for this run "
            "(the gateway downloads it in the background)",
            file=sys.stderr,
        )
    # A stale vector space means the loaded FAISS index was built by a different
    # model, so a new-model query vector would be scored against incomparable
    # vectors. Degrade THIS run to keyword search rather than reconciling:
    # clearing is destructive, a one-shot CLI cannot re-embed a corpus, and with a
    # rejected custom path nothing could ever regenerate what it cleared. The
    # gateway reconciles and re-embeds on its next boot.
    if vector_memory.embed_fn is not None and store_embedding_space_is_stale(vector_memory):
        vector_memory.embed_fn = None
        vector_memory.embed_fn_factory = None
        print(
            "Embedding model changed — keyword search for this run "
            "(the gateway re-embeds in the background)",
            file=sys.stderr,
        )
    memory.vector_store = vector_memory

    conv_log = ConversationLog()
    conv_log.init()
    lessons = LessonStore()
    # Constructed on a running loop, so construction-time sync skips itself;
    # standalone `kirocrew run` has no gateway to own the sync, so run the
    # explicit seam in a worker thread (mirrors gateway startup). A failed
    # sync must not gate the task: continue with the skills already on disk.
    skills = SkillsLoader(install_builtins=False)
    try:
        await asyncio.to_thread(skills.sync_builtins)
    except Exception:
        logging.getLogger(__name__).warning(
            "builtin-skill sync failed; continuing with the skills already " "on disk",
            exc_info=True,
        )
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=memory,
        sessions=sessions,
        lesson_store=lessons,
        history_idle_secs=cfg.memory.history_idle_hours * 3600,
        vector_store=vector_memory,
        migrated=cfg.memory.migrated,
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

    async def _cli_notify(title: str, body: str, task_id: str = "") -> None:
        print(f"\n{title}")
        if body:
            print(f"  {body}")

    # Opt-out state is sourced from the keystone denied_commands.json, not
    # config.json's hooks section (the agent cannot write the keystone file).
    hooks = HookManager(hooks_config_from_config_dict(cfg.hooks))
    ctx = ContextBuilder(
        memory=memory, skills=skills, hooks=hooks, lessons=lessons, bot_name=cfg.agent.bot_name
    )
    register_skill_read_observer(ctx)
    runner = TaskRunner(
        sessions=sessions,
        context_builder=ctx,
        auto_test=auto_test,
        on_notify=_cli_notify,
        work_dir=_session_work_dir("taskrunner:main"),
        conversation_log=conv_log,
        consolidator=consolidator,
        lesson_store=lessons,
        fresh=fresh,
        global_timeout=timeout,
        workspace_dir=cfg.taskrunner.workspace_dir,
        max_parallel_steps=cfg.taskrunner.max_parallel_steps,
        test_cmd=cfg.taskrunner.test_command,
        require_verification=cfg.taskrunner.require_verification,
    )

    # Pre-warm session pool (background session for lesson extraction)
    await sessions.start_pool()

    if fresh:
        print(f"👻 Running spec (fresh): {spec_path}")
    else:
        print(f"👻 Running spec: {spec_path}")
    task_name = getattr(args, "name", "")
    result = await runner.run(spec_path, name=task_name)

    label = result.name or result.task_id
    if result.status == "completed":
        print(f"\n✅ Task completed — {label} ({len(result.tasks)} steps)")
    elif result.status == "failed":
        print(f"\n❌ Task failed ({label}): {result.error}", file=sys.stderr)
        sys.exit(1)
    elif result.status == "cancelled":
        print("\n⚠️  Task cancelled")
        sys.exit(1)

    await sessions.close_all()


def _service_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kirocrew service {install,uninstall,status}``.

    Wraps :mod:`kiro_crew.service.controller` so that platform detection
    and the underlying systemctl/launchctl calls live there. The CLI
    layer only handles argument parsing, audit logging, and exit codes.
    """
    action = getattr(args, "service_action", None)
    if action == "install":
        rc = service_controller.install_service()
        sel().log_api_access(
            caller="cli",
            operation="service_install",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "uninstall":
        rc = service_controller.uninstall_service()
        sel().log_api_access(
            caller="cli",
            operation="service_uninstall",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "status":
        rc = service_controller.service_status()
        sel().log_api_access(
            caller="cli",
            operation="service_status",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    print("Usage: kirocrew service {install|uninstall|status}", file=sys.stderr)
    return 2


def _sandbox_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kirocrew sandbox {install-profile,remove-profile,status}``.

    Mirrors :func:`_service_cmd`: platform detection and the privileged calls
    live in :mod:`kiro_crew.service.controller`, and this layer only parses
    arguments, writes the audit record, and returns an exit code.

    Installing an AppArmor profile is a privileged, security-relevant change to
    the host, so it is audited exactly like a service install.
    """
    action = getattr(args, "sandbox_action", None)
    path = getattr(args, "path", None)
    if action == "install-profile":
        rc = service_controller.install_launcher_profile(path)
        sel().log_api_access(
            caller="cli",
            operation="sandbox_profile_install",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc} path={path or '$APPIMAGE'}",
        )
        return rc
    if action == "remove-profile":
        rc = service_controller.remove_launcher_profile()
        sel().log_api_access(
            caller="cli",
            operation="sandbox_profile_remove",
            outcome="allowed" if rc == 0 else "error",
            source="cli",
            resources=f"rc={rc}",
        )
        return rc
    if action == "status":
        # Read-only, so no audit record — it changes nothing and is expected to
        # be polled by the desktop app on every launch.
        return service_controller.sandbox_profile_status(path)
    print(
        "Usage: kirocrew sandbox {install-profile|remove-profile|status}",
        file=sys.stderr,
    )
    return 2


def _logs_cmd(args: argparse.Namespace) -> None:
    """Tail gateway logs from the most appropriate source.

    Order of preference:
      1. the USER journal (``journalctl --user``) when the per-user unit — the
         SELinux remedy's gateway — is the one running, or the only one installed
      2. systemd journal (if the system service is installed on Linux)
      3. launchd stdout file (macOS)
      4. ``~/.kiro/crew/gateway.log`` (foreground gateway)
    """
    follow = bool(getattr(args, "follow", False))
    lines = int(getattr(args, "lines", 100) or 100)
    plat = current_platform()
    unit = f"{SERVICE_NAME}.service"

    # Audit before any os.execvp branch — the exec replaces this process
    # so a post-exec audit call would never run.
    sel().log_api_access(
        caller="cli",
        operation="logs",
        outcome="allowed",
        source="cli",
        resources=f"follow={follow} lines={lines} platform={plat.value}",
    )

    system_unit = plat == Platform.SYSTEMD and svc_linux.UNIT_PATH.exists()
    user_unit = plat == Platform.SYSTEMD and svc_linux.user_unit_installed()
    # A gateway running as the per-user unit (what the SELinux refusal hands the
    # operator) logs to the account's OWN journal, which the system-scope arm
    # below never opens — and that arm always execs or exits once the system unit
    # file exists, so on a host where a stopped system unit was left beside the
    # running user unit it would tail the dead unit's journal. The user journal
    # therefore goes first whenever its unit is the running one (or the only
    # one). `journalctl --user` reads it without privilege, so there is no sudo
    # rung here: an empty probe means the user journal holds nothing readable
    # (no persistent journal, or none for this unit yet), and the next source is
    # the honest fallback rather than a password prompt. `--quiet` matters: a
    # journal with no matching entries prints `-- No entries --` on STDOUT with
    # exit 0 (systemd 252), which would pass the emptiness check and exec a tail
    # of nothing; quiet suppresses that notice so an empty journal reads empty.
    if user_unit and (not system_unit or svc_linux.user_unit_active()):
        base = ["journalctl", "--user", "--no-pager", "-u", unit, "-n", str(lines)]
        probe = subprocess.run(
            ["journalctl", "--user", "--quiet", "-u", unit, "-n", "1", "--no-pager"],
            capture_output=True,
            check=False,
            **UTF8_TEXT,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            if follow:
                base.append("-f")
            os.execvp("journalctl", base)

    if system_unit:
        # Try journalctl unprivileged first — it works if the user is in
        # the `systemd-journal` or `adm` group. Only fall back to sudo
        # journalctl if the unprivileged probe returns no rows. Without
        # this fall-through, `kirocrew logs` would hang on hosts without
        # passwordless sudo, which is a surprising failure mode for a
        # read-only log-viewer.
        base = ["journalctl", "--no-pager", "-u", unit, "-n", str(lines)]
        probe = subprocess.run(
            ["journalctl", "-u", unit, "-n", "1", "--no-pager"],
            capture_output=True,
            check=False,
            **UTF8_TEXT,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            if follow:
                base.append("-f")
            os.execvp("journalctl", base)
        # Refuse to invoke sudo without a TTY: in non-interactive
        # contexts (cron, piped scripts, systemd ExecStartPre) the sudo
        # password prompt would block forever with no way to cancel.
        if not sys.stdin.isatty():
            print(
                "👻 Insufficient permissions to read the journal without sudo, "
                "and stdin is not a TTY so sudo can't prompt.\n"
                "   Add your user to the `systemd-journal` or `adm` group, or run:\n"
                f"   sudo journalctl -u {unit} -f",
                file=sys.stderr,
            )
            sys.exit(1)
        # Fall back to sudo journalctl. `--no-pager` prevents the pager
        # (`less`) from taking over after exec, which behaves badly in
        # piped/non-interactive contexts.
        sudo_cmd = ["sudo", *base]
        if follow:
            sudo_cmd.append("-f")
        os.execvp("sudo", sudo_cmd)

    # Both guards mirror checks the systemd arm above already makes on its own
    # branch. current_platform() returns LAUNCHD for any macOS host whether or
    # not the agent is installed, so without PLIST_PATH this arm captures `logs`
    # even when the gateway runs in the foreground; and a 0-byte STDOUT_LOG from
    # an install that never started the agent still satisfies exists().
    #
    # Either miss is silent and total: this arm os.execvp()s, so there is no
    # fall-through to the config_dir() log below and `kirocrew logs` exits 0
    # having printed nothing.
    if (
        plat == Platform.LAUNCHD
        and svc_macos.PLIST_PATH.exists()
        and svc_macos.STDOUT_LOG.exists()
        and svc_macos.STDOUT_LOG.stat().st_size > 0
    ):
        cmd = ["tail", "-n", str(lines)]
        if follow:
            cmd.append("-f")
        cmd.append(str(svc_macos.STDOUT_LOG))
        os.execvp("tail", cmd)

    fallback = config_dir() / "gateway.log"
    if not fallback.exists():
        print(
            "👻 No gateway logs found. Either install the service "
            "(`kirocrew service install`) or start the gateway "
            "(`kirocrew gateway`).",
            file=sys.stderr,
        )
        sys.exit(1)
    if plat == Platform.UNSUPPORTED:
        try:
            _tail_log_file(fallback, lines, follow)
        except OSError as exc:
            print(
                f"Unable to read gateway log: {exc}. "
                "Check file access or retry `kirocrew logs` if the log is rotating.",
                file=sys.stderr,
            )
            sys.exit(1)
        return
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(fallback))
    os.execvp("tail", cmd)


def _tail_log_file(path: Path, lines: int, follow: bool) -> None:
    """Follow the log by name, allowing the writer to rotate it during reads."""
    identity: tuple[int, int] | None = None
    offset = 0
    first = True
    decoder = io.IncrementalNewlineDecoder(
        codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
    )
    try:
        while True:
            try:
                fd = platform_compat.open_log_file_for_tail(path)
            except FileNotFoundError:
                if not follow:
                    raise
            else:
                with os.fdopen(fd, "rb") as log:
                    info = os.fstat(log.fileno())
                    current_identity = (info.st_dev, info.st_ino)
                    if current_identity != identity or info.st_size < offset:
                        offset = 0
                        decoder.reset()
                    if first:
                        data = b"".join(deque(log, maxlen=abs(lines)))
                        first = False
                    else:
                        log.seek(offset)
                        data = log.read()
                    offset = log.tell()
                    identity = current_identity
                # Close before output or sleep can block. Rotation after open
                # is picked up by name on the next poll, without a retry loop.
                sys.stdout.write(decoder.decode(data, final=not follow))
                sys.stdout.flush()
            if not follow:
                return
            time.sleep(0.2)
    except KeyboardInterrupt:
        return


# ── The split-out owners ──
# This module keeps the file-delivery approval, status, gateway, run, service, sandbox and logs
# commands; the gateway peer proof, the token, logout and update-approve requests, ``stop``,
# ``restart`` and ``update`` live in the owners below. Every name an owner defines still resolves as
# ``kiro_crew.cli_server.<name>``, and a write through this module (``monkeypatch.setattr``,
# ``mock.patch``) reaches every owner holding the name (``kiro_crew._facade_mirror``). No owner
# imports this module; a higher owner imports a lower one. The owners log as
# ``kiro_crew.cli_server``.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.cli_server_peer",
    "kiro_crew.cli_server_requests",
    "kiro_crew.cli_server_stop",
    "kiro_crew.cli_server_restart",
    "kiro_crew.cli_server_update",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(sys.modules[__name__], _OWNERS, excluded=()).served)
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.cli_server_peer import (  # noqa: F401
        _CLI_LOOPBACK,
        _PEER_RESOLVE_RETRY_INTERVAL,
        _PEER_RESOLVE_RETRY_SECONDS,
        _REPUBLISH_IDENTITY_HINT,
        _audit_peer_allowed,
        _audit_peer_denied,
        _connected_peer_verdict,
        _connected_unix_peer_is_recorded_gateway,
        _ConnectedPeerVerdict,
        _existing_dashboard_socket,
        _GatewayPeerRefused,
        _owner_verified_secret_urlopen,
        _recorded_gateway_pid,
    )
    from kiro_crew.cli_server_requests import (  # noqa: F401
        _GATEWAY_NOT_LISTENING_HINT,
        _TOKEN_REQUEST_TIMEOUT,
        _emit_session_urls,
        _logout,
        _probe_dashboard_health,
        _token,
        _update_approve,
    )
    from kiro_crew.cli_server_restart import (  # noqa: F401
        _READY_DIED,
        _READY_OK,
        _READY_TIMEOUT,
        _RESTART_AUDIT_KIND,
        _RESTART_LOG_RELAY_BYTES,
        _RESTART_LOG_RELAY_LINE_CHARS,
        _RESTART_LOG_RELAY_LINES,
        _RESTART_READY_POLL_INTERVAL,
        _RESTART_READY_SOFT_CHECKPOINT,
        _RESTART_READY_TIMEOUT_DEFAULT,
        _RESTART_READY_TIMEOUT_MAX,
        _RESTART_READY_TIMEOUT_MIN,
        _RESTART_REMEDY_LABEL,
        _RESTART_STOP_TIMEOUT,
        _RESTART_TOKEN_TTL,
        _RESTART_TOKEN_WAIT,
        _gateway_log_lines_since,
        _gateway_log_mark,
        _GatewayLogMark,
        _incumbent_from_lock_holder,
        _own_console_script,
        _print_still_starting,
        _print_token_url,
        _probe_gateway_ready,
        _read_log_tail,
        _replacement_is_serving,
        _resolve_restart_ready_timeout,
        _restart,
        _spawn_detached_gateway,
        _wait_for_pids_exit,
        _wait_gateway_ready,
        _warn_restart_ready_override,
    )
    from kiro_crew.cli_server_stop import (  # noqa: F401
        _MAX_ECHOED_NAME_LEN,
        _SHUTDOWN_RESPONSE_MAX_BYTES,
        _gateway_pid,
        _manual_stop_command,
        _pid_exited,
        _refuse_if_lock_held,
        _refuse_lock_holder,
        _refuse_service_alias,
        _report_authenticated_shutdown,
        _request_gateway_shutdown,
        _stop,
        _stop_expected_pid,
        _stop_mcp_gateway_daemon,
        _terminal_safe_name,
        _verified_loopback_gateway_pids,
    )
    from kiro_crew.cli_server_update import (  # noqa: F401
        _refresh_agent_config,
        _revalidate_gateway_update_check,
        _snapshot_memory_or_exit,
        _update,
        _update_managed_venv,
        _update_wheel,
    )
