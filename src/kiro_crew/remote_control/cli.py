"""``kirocrew remote-control [status|on|off]``: the terminal side of the setting.

``off`` writes an explicit ``false``, so Remote Control stays off across a restart.
A running gateway notices the write within a few seconds and stops the relay
(:mod:`kiro_crew.remote_control.guard`); no restart is needed for that. ``on`` is
refused, and nothing is written, while the Kiro sign-in is an API key or an
external identity provider (exit 1). ``status`` prints what the setting asks for
and why it is refused when it is.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_ACTIONS = (
    ("status", "Show whether Remote Control is on, and why it is refused if it is"),
    ("on", "Turn personal Remote Control on (refused for API-key or external IdP sign-ins)"),
    ("off", "Turn personal Remote Control off; it stays off across restarts"),
)


def register_parser(sub: Any) -> None:
    from kiro_crew import cli_help

    parser = cli_help.add_command(sub, "remote-control")
    actions = parser.add_subparsers(dest="remote_control_action")
    for name, help_text in _ACTIONS:
        action = actions.add_parser(name, help=help_text)
        if name == "status":
            action.add_argument("--json", action="store_true", help="Print JSON")


def _audit(outcome: str, resources: str) -> None:
    from kiro_crew.sel import sel

    sel().log_api_access(
        caller="cli",
        operation="remote_control",
        outcome=outcome,
        source="cli",
        resources=resources,
    )


def _status(as_json: bool) -> int:
    from kiro_crew.remote_control import setting

    view = setting.read()
    if as_json:
        print(
            json.dumps(
                {
                    "requested": view.requested,
                    "allowed": view.decision.ok,
                    "reason": view.decision.reason,
                    "notice": view.decision.notice,
                    "effective": view.effective,
                }
            )
        )
        return 0
    if view.effective:
        print("Remote Control: on")
    elif view.requested:
        print("Remote Control: on in settings, but refused")
    else:
        print("Remote Control: off")
    if view.decision.notice:
        print(view.decision.notice)
    return 0


def run(args: argparse.Namespace) -> None:
    from kiro_crew.remote_control import setting

    action = getattr(args, "remote_control_action", None) or "status"
    if action == "status":
        sys.exit(_status(bool(getattr(args, "json", False))))
    if action == "off":
        setting.turn_off()
        _audit("allowed", "enabled=false")
        print("✅ Remote Control is off. It stays off until you turn it back on.")
        print("   A running gateway stops the relay within a few seconds.")
        return
    try:
        setting.turn_on()
    except setting.RemoteControlRefused as refused:
        _audit("denied", f"enabled=true reason={refused.decision.reason}")
        print(f"❌ {refused.decision.notice}", file=sys.stderr)
        print("   Nothing was changed.", file=sys.stderr)
        sys.exit(1)
    _audit("allowed", "enabled=true")
    print("✅ Remote Control is on.")
    print("   If the gateway did not start with it on, restart it to connect.")
