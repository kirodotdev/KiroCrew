"""``kirocrew app``: install, import, list, enable, disable, uninstall and inspect apps.

``_handle_app`` dispatches every verb, ``dev``, ``info``, ``init`` and ``mcp`` (serve an
app's MCP server, ``_run_app_mcp_server``) included. A lifecycle change goes through a
running gateway when one answers (``_apply_through_gateway``); otherwise it is applied
to the files alone, ``enable`` and ``disable`` say so (``_print_file_only_app_result``),
and the backend, cron and hook follow-ups are applied or warned about here.

An owner of the mirror facade ``kiro_crew.cli_commands``: callers and tests reach every
name here as ``kiro_crew.cli_commands.<name>``, and a patch through the facade reaches
this module. This module never imports the facade.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from kiro_crew import app_lifecycle_client
from kiro_crew.apps.backend import recorded_backend_port
from kiro_crew.apps.bridges import (
    SessionPointerCleanup,
    deregister_app,
    deregister_app_crons_from_service,
    discard_app_session_pointers,
    register_app,
    register_app_crons_with_service,
)
from kiro_crew.apps.manager import (
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    trust_grant_removal_blocked,
    uninstall_app,
)
from kiro_crew.apps.plugin_import import (
    PluginImportError,
    convert_plugin_package,
    find_plugin_manifest,
    normalize_app_name,
    read_manifest_name,
)
from kiro_crew.apps.scaffold import scaffold_app
from kiro_crew.config import config_dir
from kiro_crew.cron import CronService
from kiro_crew.sel import sel
from kiro_crew.terminal_safe import safe_terminal_line


def _run_app_action_through_gateway(
    action: str, app_name: str, *, payload: dict[str, object] | None = None
) -> bool:
    """Return true when a live gateway handled an app lifecycle request."""
    return _apply_through_gateway(
        action,
        app_name,
        lambda: app_lifecycle_client.toggle_app(app_name, action, payload=payload),
        hint="Toggle the app in the dashboard instead.",
    )


def _install_app_through_gateway(source: str) -> bool:
    """Return true when a live gateway installed the app directory *source*."""
    return _apply_through_gateway(
        "install",
        source,
        lambda: app_lifecycle_client.install_app(source),
        # The dashboard's install calls the same route, so pointing there would not
        # help; the gateway opens the directory and its app.json as its own user.
        hint="Nothing was installed; the gateway reads the app directory as its own user.",
    )


def _apply_through_gateway(
    action: str,
    subject: str,
    send: Callable[[], dict[str, object] | None],
    *,
    hint: str,
) -> bool:
    """Run one gateway lifecycle request and render its outcome for the CLI."""
    try:
        result = send()
    except app_lifecycle_client.AppGatewayTimeout as exc:
        # The outcome is unknown, not negative: the gateway may still be applying
        # the action, so this is neither a refusal nor an invitation to retry.
        print(f"⏳ {exc}", file=sys.stderr)
        sys.exit(1)
    except app_lifecycle_client.AppGatewayError as exc:
        if action == "enable" and exc.code == _SESSION_APPROVAL_CONSENT_REQUIRED:
            _refuse_session_approval_from_cli(subject)
        print(f"❌ gateway refused: {exc}. {hint}", file=sys.stderr)
        sys.exit(1)
    if result is None:
        return False
    name = result.get("name")
    app_name = name if action == "install" and isinstance(name, str) and name else subject
    app_lifecycle_client.print_result(action, app_name, result)
    return True


def _print_file_only_app_result(app_name: str, *, enabled: bool) -> None:
    """Report a persisted lifecycle change without claiming it is live."""
    state = "enabled" if enabled else "disabled"
    print(
        f"✅ Recorded {app_name} as {state}. No running gateway was reached, so "
        "this takes effect at the next gateway start (on Windows and in sandboxed "
        "shells the CLI always uses this path). If a gateway is running now, apply "
        "it live from the dashboard."
    )


_SESSION_APPROVAL_CONSENT_REQUIRED = "session_approval_consent_required"
# Same sentence the dashboard's app detail page shows for ``permissions.sessionApproval``
# (``pages.appDetailPage.session_approval_desc``), so both surfaces disclose the same grant.
_SESSION_APPROVAL_DESC = (
    "Can send messages and choose response options in your chats, approve or deny "
    "tool prompts, and switch a chat's approval mode."
)


def _refuse_session_approval_from_cli(app_name: str) -> NoReturn:
    """Say what *app_name* asks for and where a person allows it, then exit 1.

    The CLI never gives this consent: there is no flag and no prompt, and it never
    sends ``sessionApprovalConsent``. An agent with a shell could pass a flag or
    answer a prompt itself, so only the dashboard can grant control of the owner's
    chats. The app stays off.
    """
    from kiro_crew.apps.permissions import format_permissions_summary
    from kiro_crew.dashboard.urls import resolve_dashboard_host
    from kiro_crew.port_resolution import resolve_client_port

    shown = safe_terminal_line(app_name)
    print(f"❌ {shown} asks to control your chats. Nothing was enabled.", file=sys.stderr)
    print(f"   Chats:     {_SESSION_APPROVAL_DESC}", file=sys.stderr)
    try:
        manifest = get_app_manifest(app_name)
    except Exception:  # noqa: BLE001 - an unreadable manifest must not hide the link
        manifest = None
    if manifest is not None:
        # Manifest text is app-authored, so every line goes through the sanitizer.
        for line in format_permissions_summary(manifest).splitlines():
            print(f"   {safe_terminal_line(line.strip())}", file=sys.stderr)
    host = resolve_dashboard_host(local_only=True)
    port = resolve_client_port(None)
    link = f"http://{host}:{port}/apps/detail/{urllib.parse.quote(app_name, safe='')}"
    print(f"   To allow it, open the app in the dashboard: {link}", file=sys.stderr)
    sys.exit(1)


def _app_declares_backend(app_name: str) -> bool:
    """Whether *app_name*'s manifest declares a backend process.

    One of the two positive signals the file-only uninstall's warning fires on, and
    sound only as one of two. A DECLARATION is a positive property of the app, so
    record-absence must not silence the warning: an app with no persisted record is
    exactly the case where nothing may be concluded. But this answer is read from
    the app's own ``app.json``, writable by any app trusted to run code, so
    manifest-absence must not silence it either — an app that drops its
    ``entryPoint`` would otherwise hide the process it is still running. The caller
    therefore also reads the gateway-owned recorded port, which the app cannot
    reach, and warns on either. An unreadable or missing manifest answers ``True``
    for the same reason both halves exist: not knowing is not the same as knowing
    there is nothing to stop.
    """
    try:
        manifest = get_app_manifest(app_name)
    except Exception:  # noqa: BLE001 - a malformed manifest must not fail an uninstall
        return True
    if manifest is None:
        return True
    return bool(getattr(getattr(manifest, "backend", None), "entryPoint", ""))


def _warn_backend_not_stopped(app_name: str) -> None:
    """Report that a file-only uninstall could not stop the app's backend."""
    print(
        f"⚠️  No running gateway was reached, so {app_name}'s backend was not "
        "stopped: signalling a process another gateway started is not something "
        "the CLI can do from out here (on Windows and in sandboxed shells the CLI "
        "always uses this path, so a gateway may well be running). If one is still "
        "running it holds its port until the next gateway start, which terminates "
        "backends left behind by a previous generation. To stop it now, restart the "
        "gateway, or uninstall from the dashboard instead.",
        file=sys.stderr,
    )


def _cleanup_app_crons_from_scheduler(app_name: str) -> int:
    """Remove app-owned cron jobs from the shared scheduler store before disable/uninstall.

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
    """Tell the operator how a CLI enable's backend-hook change reaches a gateway.

    Everything else a CLI enable writes is re-read by a running gateway:
    ``enable_app`` writes ``installed.json``, ``register_app`` writes agent and
    skill files, and ``_register_app_crons_to_scheduler`` writes through the
    shared cron store that the gateway's timer tick re-syncs by content digest.
    Backend hooks are Python modules imported INTO the gateway process, and only
    the gateway can replace them (``on_app_enable`` ->
    ``RouteRegistry.register_app_routes`` -> ``load_app_module``, reached by the
    HTTP enable route and by ``on_gateway_startup``). This CLI process has no
    handle on that one's ``sys.modules``, so it cannot load them directly --
    and it does not need to: the gateway runs a hook reconciler
    (``apps/hook_reconcile.py``) that notices an on-disk hook change this CLI
    made and reloads it in-process within a poll interval. A
    stopped gateway loads the new code on its next start.

    Printing only "enabled <app>" reads as though the change were already live in
    a running gateway, when in fact it lands a few seconds later. This notice
    states the actual timing so an operator does not verify a hook change against
    stale code and draw the wrong conclusion.

    Deliberately NOT gated on a live-gateway probe. ``_marker_port`` is the only
    verified one available here, and it writes its own multi-gateway warning to
    stderr, which would surface out of context on an app enable. The notice is
    phrased conditionally instead, so it stays true whether or not a gateway is
    up.

    Only for apps that declare ``backend.hooks``: there is nothing to say
    otherwise. Returns whether the notice was printed.
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
        "  A gateway that is already running reloads them automatically within a\n"
        "  few seconds (the gateway reconciles hook changes it did not perform\n"
        "  itself); if the gateway is stopped, they load on its next start."
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


def _handle_app_import(args: argparse.Namespace) -> None:
    """Convert a manifest-declared plugin package into an app directory.

    Conversion reads and copies only: nothing in the source package is imported or
    executed while this command runs. It says nothing about later -- a converted
    MCP server entry names a command, and enabling the emitted app launches it
    like any other app's. See ``kiro_crew.apps.plugin_import``.
    """
    source = Path(args.source).expanduser()
    try:
        manifest_path, _ = find_plugin_manifest(source)
        # The manifest is foreign input, and the module that owns its contract
        # does the parsing: it refuses a document that is not a JSON object and
        # reads a non-string `name` as absent. Parsing it here instead loses both
        # checks, and every refusal it raises is a PluginImportError the arm
        # below already prints.
        manifest_name = read_manifest_name(manifest_path)
        derived = normalize_app_name(
            getattr(args, "name", None) or (manifest_name or source.resolve().name)
        )
    except PluginImportError as exc:
        # Every refusal message in this command embeds a path taken from the
        # package under conversion -- a declared resource, a missing file, the
        # source root -- and that text lands after a ``❌`` prefix on the operator's
        # terminal. Untouched, a crafted package name could carry escapes that
        # repaint the screen, or a newline that opens an unprefixed line reading as
        # this tool's own output. ``exc.code`` is an internal literal, so only the
        # message needs the repo's shared one-line sanitizer.
        print(f"❌ {exc.code}: {safe_terminal_line(exc.message)}", file=sys.stderr)
        sys.exit(1)
    except (OSError, json.JSONDecodeError) as exc:
        # str() of either carries the offending filename, which is package-supplied.
        print(
            f"❌ cannot read the plugin manifest: {safe_terminal_line(str(exc))}",
            file=sys.stderr,
        )
        sys.exit(1)

    out_dir = Path(args.out).expanduser() if getattr(args, "out", None) else Path(f"{derived}-app")
    try:
        report = convert_plugin_package(source, out_dir, name_override=derived)
    except PluginImportError as exc:
        print(f"❌ {exc.code}: {safe_terminal_line(exc.message)}", file=sys.stderr)
        sys.exit(1)

    print(report.render_text())
    # out_dir needs no sanitizing on either line below: when it is derived it comes
    # from normalize_app_name, which folds every non-alphanumeric character to a
    # hyphen and then requires a full kebab-case match, so no control character
    # survives it; when it is given it was typed by the operator reading this line.
    print(f"\n✅ wrote {out_dir / 'app.json'}")

    if not getattr(args, "install", False):
        print(f"\n   Run: kirocrew app install {out_dir}")
        return

    result = install_app(str(out_dir))
    if not result.ok:
        print(f"❌ {safe_terminal_line(result.error)}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ {safe_terminal_line(result.message)}")
    # NOT registered here. Installing writes the app's files; it does not turn the
    # app on, and this path leaves it disabled -- the line below is what tells the
    # operator so. Registering at this point activates the app's executable
    # resources (its agents, its MCP servers) for a disabled app, so the next agent
    # session launches a command from a third-party package that nobody enabled.
    # The enable path registers, which is where the decision to run it is made.
    print(f"\n   Run: kirocrew app enable {result.name}")


def _app_already_gone(result: object) -> bool:
    """Whether an uninstall failed only because the app is not installed.

    Matched on the message `uninstall_app` produces for that case rather than on a
    code, because `AppResult` carries no code; kept narrow on purpose — every other
    failure leaves the app whole, and clearing the pointers its slots are still
    entitled to resume would be the bug this whole path exists to prevent.
    """
    return "is not installed" in str(getattr(result, "error", "") or "")


def _print_pointer_cleanup(name: str, cleanup: SessionPointerCleanup) -> None:
    """Say what happened to the app's resume pointers, including nothing.

    A clear that did not happen cannot stay quiet, whether it declined or failed.
    Either way it leaves a pointer keyed by a name a reinstall reuses, so the next
    installation's first turn resumes the removed app's transcript — and the
    operator who would have to notice that is standing right here, at a command
    that otherwise printed a success tick. The two get different text because they
    need different actions: stop the gateway, versus fix the storage or lock-file
    error the log names.
    """
    if cleanup.dropped:
        print(f"   dropped {cleanup.dropped} conversation pointer(s) — a reinstall starts fresh")
    elif cleanup.declined:
        print(
            f"   ⚠️  left {name}'s conversation pointers in place: a running gateway owns "
            f"session_map.json, and a second writer would drop rows it has not flushed. "
            f"Reinstalling under this name may resume the removed app's transcript. "
            f"Stop the gateway and run `kirocrew app uninstall {name}` again to clear them.",
            file=sys.stderr,
        )
    elif cleanup.failed:
        print(
            f"   ⚠️  could not clear {name}'s conversation pointers: the session map "
            f"or its lock file could not be used (see the log for the error). Reinstalling "
            f"under this name may resume the removed app's transcript. Fix the cause "
            f"and run `kirocrew app uninstall {name}` again to clear them.",
            file=sys.stderr,
        )


def _handle_app(args: argparse.Namespace) -> None:
    """Dispatch app subcommands: install, list, enable, disable, uninstall, info."""
    action = getattr(args, "app_action", None)

    if action == "mcp":
        # Spawned by kiro-cli as a stdio MCP server (declared in the app's
        # manifest mcpServers). stdout is the JSON-RPC channel — never print to
        # it here, or the handshake breaks.
        _run_app_mcp_server(args.name)
        return

    if action == "import":
        _handle_app_import(args)
        return

    if action == "install":
        # Ask a running gateway first, as enable, disable and uninstall do. Only
        # the gateway can start the new app's backend and publish it to the
        # dashboard it is serving; installing from files alone leaves a running
        # gateway unaware of the app until it restarts.
        if _install_app_through_gateway(args.source):
            return
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
        if _run_app_action_through_gateway("enable", args.name):
            return
        result = enable_app(args.name)
        if result.ok:
            reg = register_app(args.name)
            _print_file_only_app_result(args.name, enabled=True)
            if reg.agents:
                print(f"   Agents registered: {len(reg.agents)}")
            if reg.skills:
                print(f"   Skills registered: {len(reg.skills)}")
            _register_app_crons_to_scheduler(args.name)
            _warn_hooks_need_restart(args.name)
        elif getattr(result, "error_code", "") == _SESSION_APPROVAL_CONSENT_REQUIRED:
            _refuse_session_approval_from_cli(args.name)
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "disable":
        if _run_app_action_through_gateway("disable", args.name):
            return
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
            _print_file_only_app_result(args.name, enabled=False)
        else:
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "uninstall":
        # Ask a running gateway first, exactly as enable and disable do above.
        # Uninstall has to stop the app's backend, and out here it cannot: the
        # gateway is a DIFFERENT process and holds the only live handle on that
        # child. Doing the whole uninstall locally deleted the app's files while
        # its backend kept running -- holding its port, its app secret and its
        # proxied routes -- and still printed success. The gateway's own handler
        # runs the same trust-grant and cron preconditions before anything
        # destructive, so nothing is skipped by handing the work over.
        #
        # The purge flag travels in the body because the handler defaults an
        # absent one to "preserve data"; without it a delegated `--purge-data`
        # would quietly keep the data it was told to destroy.
        if _run_app_action_through_gateway(
            "uninstall",
            args.name,
            payload={"purge_data": bool(getattr(args, "purge_data", False))},
        ):
            return
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
        # Read BEFORE the uninstall, and read BOTH: either one alone can be made to
        # say "no backend here" when there is one. The declaration is the app's own
        # `app.json`, writable by any app trusted to run code, so an app that drops
        # its `entryPoint` would silence the warning about the process it is still
        # running. The recorded port is gateway-owned -- the pidfile lives under
        # KIROCREW_HOME, not in the app directory -- but its ABSENCE proves nothing,
        # since a backend this gateway never tracked leaves no row. So the warning
        # fires on either positive signal, and stays quiet only when neither says a
        # backend exists.
        declares_backend = _app_declares_backend(args.name)
        recorded_port = recorded_backend_port(args.name)
        result = uninstall_app(args.name, keep_data=keep_data)
        if result.ok:
            # AFTER success, matching this function's trust-grant reasoning: a
            # failed uninstall leaves nothing changed, so a still-installed app
            # keeps the pointers its slots are still entitled to resume.
            cleanup = discard_app_session_pointers(args.name)
            print(f"✅ {result.message}")
            _print_pointer_cleanup(args.name, cleanup)
            if declares_backend or recorded_port is not None:
                _warn_backend_not_stopped(args.name)
        else:
            # The pointers outlive the app, so "already gone" is the one failure
            # whose bookkeeping half is still worth doing. It is also the residual's
            # only self-correction: the clear runs under `GatewayLock` and declines
            # while a gateway owns `session_map.json`, so an uninstall done with the
            # gateway up leaves pointers behind — and re-running this command with
            # the gateway stopped is what clears them. That recovery only exists if
            # the clear does not require the app to still be installed, which is why
            # it runs here rather than only on the success path.
            if _app_already_gone(result):
                _print_pointer_cleanup(args.name, discard_app_session_pointers(args.name))
            print(f"❌ {result.error}", file=sys.stderr)
            sys.exit(1)

    elif action == "dev":
        from kiro_crew.apps.dev_mode import set_dev_mode

        enabled = not getattr(args, "off", False)
        confirm_root = getattr(args, "confirm_out_of_install_root", False)
        if confirm_root and not enabled:
            # The confirmation only applies to enabling: a disable never
            # grants. Say so rather than silently ignoring the flag — an
            # operator who reaches for it on the wrong invocation would
            # otherwise read the exit-0 as "confirmed".
            print(
                "⚠️  --confirm-out-of-install-root has no effect with --off "
                "(disabling grants nothing)",
                file=sys.stderr,
            )
        dev_result = set_dev_mode(
            args.name,
            enabled,
            confirm_out_of_install_root=confirm_root,
        )
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
