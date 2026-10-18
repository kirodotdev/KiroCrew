"""Argparse definitions for the ``Start here``, ``Run the gateway`` and ``Set it up`` commands.

One registrar per command of those ``cli_help.COMMAND_GROUPS`` sections: doctor, service,
status, restart, stop, gateway-pid, logs, token, logout, ledger-sweep, update, setup,
config, sandbox, kas and manifest. ``kiro_crew.cli.main`` builds the top-level parser and
calls every registrar in the command line's registration order, so a new command of these
sections gets a registrar here and a call there. The ``gateway`` command's own arguments
stay in ``cli.py``: ``pod.runtime_boot.target_supports_flag`` reads them from that file.

Parser construction only: the handlers live where ``main`` dispatches them, and nothing
here reads a name a test patches through ``kiro_crew.cli``
(``test/test_cli_split_facade.py`` pins that).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from kiro_crew import cli_help

if TYPE_CHECKING:
    _Commands = argparse._SubParsersAction[argparse.ArgumentParser]

# Helper for commands with examples
_fmt = argparse.RawDescriptionHelpFormatter


def register_doctor_parser(sub: _Commands) -> None:
    _doctor_parser = cli_help.add_command(sub, "doctor")
    _doctor_parser.add_argument(
        "--bundle",
        action="store_true",
        help="Collect logs + crash reports into a redacted diagnostics zip",
    )


def register_ledger_sweep_parser(sub: _Commands) -> None:
    # ledger-sweep -- its own command, NOT a ``doctor`` mode. ``doctor`` is
    # read-only by its own contract (a diagnostic you run because something
    # broke must not unlink files), and ``--purge`` is irreversible; hosting the
    # purge there also gave every modifier a way to be parsed without its mode.
    ls_parser = cli_help.add_command(
        sub,
        "ledger-sweep",
        description=(
            "List the session and conductor-work ledgers that look finished. A dry "
            "run by default: nothing is deleted without --purge, and --purge is a "
            "second, separate invocation over the report the dry run printed."
        ),
    )
    ls_parser.add_argument(
        "--purge",
        action="store_true",
        help="Actually delete the listed ledgers (irreversible)",
    )
    ls_parser.add_argument(
        "--older-than-days",
        type=float,
        default=None,
        metavar="N",
        help="Idle window before a ledger qualifies (default: 30)",
    )
    ls_parser.add_argument(
        "--purge-unreadable",
        action="store_true",
        help=(
            "With --purge: also delete records this sweep could not parse "
            "(they are listed but kept by default)"
        ),
    )


def register_setup_parser(sub: _Commands) -> None:
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


def register_manifest_parser(sub: _Commands) -> None:
    manifest_parser = cli_help.add_command(sub, "manifest")
    manifest_parser.add_argument("--alias", help="Override alias (default: auto-detect)")
    manifest_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    manifest_parser.add_argument(
        "--url",
        action="store_true",
        help="Print a one-click Slack app creation URL",
    )


def register_update_parser(sub: _Commands) -> None:
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


def register_stop_parser(sub: _Commands) -> None:
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
    stop_parser.add_argument(
        "--expect-pid",
        type=int,
        default=None,
        help=(
            "Stop only if this pid is the sole listener on --port AND holds this "
            "home's gateway.lock; otherwise refuse and signal nothing. For callers "
            "that already identified the gateway they mean to stop."
        ),
    )


def register_restart_parser(sub: _Commands) -> None:
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


def register_gateway_pid_parser(sub: _Commands) -> None:
    # gateway-pid — read-only: print the PID holding this home's gateway.lock as
    # JSON, resolved through the same socket-independent lock_holder oracle that
    # `stop --expect-pid`/`restart` use. The desktop supervisor calls this when
    # its listener snapshot cannot name a draining gateway (socket released, lock
    # still held) so recovery can wait for that pid instead of giving up.
    cli_help.add_command(sub, "gateway-pid")


def register_service_parser(sub: _Commands) -> None:
    # service — install/uninstall/status as a system-level systemd unit (Linux,
    # /etc/systemd/system/, requires sudo) or launchd LaunchAgent (macOS,
    # ~/Library/LaunchAgents/, no sudo) so the gateway survives SSH disconnect,
    # auto-restarts on crash, and auto-starts on boot.
    svc_parser = cli_help.add_command(sub, "service")
    svc_sub = svc_parser.add_subparsers(dest="service_action")
    svc_sub.add_parser("install", help="Install and start the gateway service (sudo on Linux)")
    svc_sub.add_parser("uninstall", help="Stop and remove the gateway service (sudo on Linux)")
    svc_sub.add_parser("status", help="Show service status (systemctl/launchctl)")


def register_sandbox_parser(sub: _Commands) -> None:
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


def register_kas_parser(sub: _Commands) -> None:
    # kas — Kiro Crew's own KAS engine bundle (agent.kas_bundle route).
    kas_parser = cli_help.add_command(
        sub,
        "kas",
        epilog="""
Examples:
  kirocrew kas install                   # latest pinned engine + Node, from agent.kas_bundle_url
  kirocrew kas install --version 0.66.33
  kirocrew kas install --from ./bundle   # offline: manifest.json + artifacts in a directory
  kirocrew kas status
  kirocrew kas remove

Then: kirocrew config set agent.kas_bundle true && kirocrew restart
The engine signs in through Kiro Crew's own login (dashboard → Sign in).
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from kiro_crew.cli_kas import add_kas_parser

    add_kas_parser(kas_parser)


def register_logs_parser(sub: _Commands) -> None:
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


def register_token_parser(sub: _Commands) -> None:
    token_parser = cli_help.add_command(sub, "token")

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


def register_logout_parser(sub: _Commands) -> None:
    logout_parser = cli_help.add_command(sub, "logout")
    logout_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )


def register_status_parser(sub: _Commands) -> None:
    status_parser = cli_help.add_command(sub, "status")
    status_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCREW_PORT env or dashboard.url config)",
    )


def register_config_parser(sub: _Commands) -> None:
    cfg_parser = cli_help.add_command(
        sub,
        "config",
        epilog="""
Examples:
  kirocrew config get                   # Show all config
  kirocrew config get agent.provider    # Get a specific value
  kirocrew config set dashboard.url http://localhost:5476
  kirocrew config set skills.extra_paths --value-base64=WyIvb3B0L3NraWxscyJd  # ["/opt/skills"]
  kirocrew config edit                  # Open in $EDITOR
  kirocrew config defaults              # Stored values holding a superseded default
  kirocrew config defaults --adopt      # Take the current defaults for all of them
  kirocrew config defaults --keep session.autocompact_pct   # Affirm one as intentional

The dashboard port comes from the port in dashboard.url; the KIROCREW_PORT
env var overrides it.
""",
        formatter_class=_fmt,
    )
    cfg_sub = cfg_parser.add_subparsers(dest="config_action")
    cfg_get = cfg_sub.add_parser("get", help="Get a config value (or all if no key)")
    cfg_get.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set = cfg_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set.add_argument("value", nargs="?", help="Value to set")
    cfg_set.add_argument(
        "--value-base64",
        dest="value_base64",
        default=None,
        metavar="TOKEN",
        help=(
            "Value as standard Base64 of its UTF-8 text, attached with '=' "
            "(--value-base64=TOKEN) so no shell can alter or drop it; "
            "an empty token sets empty text"
        ),
    )
    cfg_set.add_argument("--file", "-f", dest="file", help="Load full config from a JSON file")
    cfg_set.add_argument(
        "--local",
        action="store_true",
        help="Save to config.local.json, the overlay whose values win over config.json",
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
